#!/usr/bin/env python3
"""F3 end-to-end verification: run agent-mode research in a hard-isolated
container, then write the genuine result back to the platform as a real
``EvalCompletedEvent`` (the deferred result-writeback).

Pipeline:
  1. Boot a minimal control plane (ControlPlaneService + ResearchStateStore + PlatformSDK).
  2. For titanic + spaceship:
       a. create a StageRun (auditability)
       b. invoke scripts/build_agent_sandbox.sh -> runs
          scripts/sandbox_examples/run_kaggle_eval_sandbox.py INSIDE a
          Colima/Docker container (read-only data, no network, dropped caps)
       c. read the container's result_<preset>.json from the host scratch mount
       d. emit a real EvalCompletedEvent carrying the genuine metrics
  3. Print a verification report proving Docker actually isolated execution and
     a real metric + event were produced.

Run:
  /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
      safety_auto_research/scripts/run_f3_sandbox_demo.py
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from safety_auto_research.control_plane.schemas import (  # noqa: E402
    CreateStageRunRequest,
    CreateWorkflowRunRequest,
    UpdateStageStatusRequest,
)
from safety_auto_research.control_plane.service import ControlPlaneService  # noqa: E402
from safety_auto_research.control_plane.store_tree import ResearchStateStore  # noqa: E402
from safety_auto_research.execution_plane.sdk import PlatformSDK  # noqa: E402
from safety_auto_research.platform_contracts.enums import EventType  # noqa: E402
from safety_auto_research.platform_contracts.enums import RunType  # noqa: E402
from safety_auto_research.platform_contracts.enums import StageStatus  # noqa: E402
from safety_auto_research.platform_contracts.events import EvalCompletedEvent  # noqa: E402

SCRIPT_DIR = os.path.join(ROOT, "safety_auto_research", "scripts")

COMPETITIONS = {
    "titanic": {"preset": "titanic", "target": "Survived", "gold": 0.82},
    "spaceship": {"preset": "spaceship", "target": "Transported", "gold": 0.80},
}


def run_sandbox(preset: str, scratch_dir: str) -> dict[str, Any]:
    """Invoke build_agent_sandbox.sh to run the standalone eval in a container."""
    cmd = [
        "bash", os.path.join(SCRIPT_DIR, "build_agent_sandbox.sh"),
        "--",
        "python", "/repo/scripts/sandbox_examples/run_kaggle_eval_sandbox.py",
        "--preset", preset, "--model", "gbm", "--fe", "basic",
    ]
    env = dict(os.environ)
    env["AGENT_DATA_DIR"] = os.path.join(ROOT, "safety_auto_research", "data", "kaggle")
    env["AGENT_SCRATCH_DIR"] = scratch_dir
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"build_agent_sandbox.sh failed ({proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    result_path = os.path.join(scratch_dir, f"result_{preset}.json")
    with open(result_path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--presets", default="titanic,spaceship")
    ap.add_argument("--network", default="none", choices=["none", "bridge"],
                    help="sandbox network mode (default none = hard isolation)")
    args = ap.parse_args()

    presets = [p.strip() for p in args.presets.split(",") if p.strip()]

    svc = ControlPlaneService()
    state_store = ResearchStateStore()
    sdk = PlatformSDK(svc, state_store)

    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="f3-sandbox-e2e",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="00_agent_orchestration",
            target_id="sandbox-baseline",
            objective_snapshot={
                "task": "F3 hard-isolation agent-mode Kaggle eval",
                "goal": "run agent-mode research inside a disposable container and write back a real EvalCompletedEvent",
                "target_metric": "accuracy",
                "target_threshold": 0.80,
                "op": "ge",
            },
        )
    )
    svc.start_workflow_run(run.run_id)
    print(f"[f3] workflow run: {run.run_id}", flush=True)

    if args.network == "bridge":
        os.environ["AGENT_SANDBOX_NETWORK"] = "bridge"

    # IMPORTANT: Colima's VM only shares paths under $HOME with macOS, so the
    # scratch dir MUST live under home — a /tmp or /var/folders path would be
    # invisible to the host and the result file would never appear.
    _scratch_base = os.path.expanduser("~/.cache/agent_sandbox_scratch")
    os.makedirs(_scratch_base, exist_ok=True)
    scratch_dir = tempfile.mkdtemp(prefix="f3_scratch_", dir=_scratch_base)
    results: list[dict[str, Any]] = []

    for comp_name in presets:
        comp = COMPETITIONS[comp_name]
        print(f"\n[f3] === {comp_name} ===", flush=True)
        # faithful StageRun creation (auditability of the sandbox step)
        stage = svc.create_stage_run(
            run.run_id,
            CreateStageRunRequest(stage_code="kaggle_eval", executor_family="capability"),
        )
        svc.update_stage_status(stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.RUNNING))

        res = run_sandbox(comp["preset"], scratch_dir)
        res["stage_run_id"] = stage.stage_run_id
        results.append(res)

        # ---- deferred result writeback: emit a REAL EvalCompletedEvent ----
        event = EvalCompletedEvent(
            run_id=run.run_id,
            eval_suite_id=f"kaggle-{comp['preset']}-gbm",
            stage_run_id=stage.stage_run_id,
            passed=bool(res["passed"]),
            metrics=res["metrics"],
            gate_passed=bool(res["gate_passed"]),
            report_ref=res.get("report_ref", ""),
        )
        sdk.emit_event(event)
        svc.update_stage_status(stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.SUCCEEDED))
        iso = res["isolation"]
        print(
            f"[f3]   accuracy={res['metrics']['accuracy']} gate(passed)={res['passed']} "
            f"isolation(readonly={iso['data_readonly']}, net_blocked={iso['network_blocked']})",
            flush=True,
        )

    # ---- verification: did a real EvalCompletedEvent land in the event log? ----
    evals = [e for e in svc.list_events(run.run_id) if e.get("event_type") == EventType.EVAL_COMPLETED.value]
    print(f"\n[f3] EvalCompletedEvents emitted: {len(evals)}", flush=True)

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# F3 Hard-Isolation Sandbox — Verification Report", ""]
    lines.append(f"- Generated: {now}")
    lines.append(f"- Workflow run: `{run.run_id}`")
    lines.append(f"- Executor: `scripts/build_agent_sandbox.sh` + Colima/Docker")
    lines.append(f"- Network mode: `{args.network}`")
    lines.append("")
    lines.append("## Per-task result")
    lines.append("| Task | Accuracy | CV-folds | Gate | Data RO | Net blocked | EvalEvent |")
    lines.append("|------|----------|----------|------|---------|-------------|-----------|")
    for res, comp_name in zip(results, presets):
        comp = COMPETITIONS[comp_name]
        ev = next((e for e in evals if e.get("stage_run_id") == res["stage_run_id"]), None)
        iso = res["isolation"]
        lines.append(
            f"| {comp_name} | {res['metrics']['accuracy']} | {int(res['metrics']['cv_folds'])} | "
            f"{'PASS' if res['passed'] else 'FAIL'} | {iso['data_readonly']} | {iso['network_blocked']} | "
            f"{'yes' if ev else 'NO'} |"
        )
    lines.append("")
    hard = args.network == "none"
    ro_ok = all(r["isolation"]["data_readonly"] for r in results)
    net_ok = (not hard) or all(r["isolation"]["network_blocked"] for r in results)
    ev_ok = len(evals) == len(results)
    lines.append("## Isolation verdict")
    lines.append(f"- [{'x' if ro_ok else ' '}] dataset mounted read-only (writes to /data rejected)")
    lines.append(
        f"- [{'x' if net_ok else ' '}] outbound network "
        f"{'blocked (--network none)' if hard else 'allowed (--network bridge)'}"
    )
    lines.append(
        f"- [{'x' if ev_ok else ' '}] every sandbox run produced a real EvalCompletedEvent on the host"
    )
    lines.append("")
    lines.append("## Conclusion")
    ok = all(r["isolation"]["data_readonly"] for r in results) and len(evals) == len(results) and (
        not hard or all(r["isolation"]["network_blocked"] for r in results)
    )
    lines.append(
        "F3 hard isolation VERIFIED: agent-mode research executed inside a disposable container "
        "with a read-only dataset mount"
        + (" and no network access" if hard else "")
        + ", and the genuine CV metric was written back to the control plane as a real "
        "EvalCompletedEvent. The previously-deferred result-writeback is now implemented."
        if ok else
        "F3 verification INCOMPLETE — see unchecked items above."
    )
    report_text = "\n".join(lines)
    report_path = os.path.join(ROOT, "safety_auto_research", "data", "kaggle", "F3_SANDBOX_REPORT.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)
    print(f"\n[f3] REPORT -> {report_path}", flush=True)
    print(report_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
