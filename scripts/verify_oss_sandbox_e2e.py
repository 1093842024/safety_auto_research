"""L1 end-to-end sandbox proof for the three OSS representative tasks.

This drives ``SandboxResearchExecutor`` — the exact capability the
``POST /benchmark-tasks/{id}/launch`` handler routes harness tasks to — for each
representative wired into the platform, and asserts the L1 contract:

    * the run launches (research_cmd path through build_agent_sandbox.sh),
    * a real, measured ``result.json`` is produced,
    * a real ``EvalCompletedEvent`` is emitted back to the control plane.

Representatives (样板优先):
    * autolab.safety_router     -> Harbor/Arbor training class  (offline, hard isolation)
    * sab.h_importances_92      -> Agent-eval class            (offline, hard isolation)
    * claudini.injection_tmeoa  -> Adversarial class (tmeoa)   (needs network -> soft/host)

Usage:
    python scripts/verify_oss_sandbox_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(REPO)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)


class _AnyAttr:
    def __getattr__(self, name):
        return None


class FakeSDK:
    """Minimal control-plane SDK surface the executor touches."""

    def __init__(self):
        self.events: list = []
        self.metrics: list = []
        self.artifacts: list = []

    def emit_event(self, event):
        self.events.append(event)

    def record_metric(self, *a, **k):
        self.metrics.append((a, k))

    def publish_artifact(self, *a, **k):
        art = _AnyAttr()
        art.artifact_id = f"art-{len(self.artifacts)}"
        self.artifacts.append(art)
        return art

    def load_object(self, *a, **k):
        return None


def _rep(name, cap_id, research_cmd, data_dir, network, expect_isolation):
    return dict(
        name=name, cap_id=cap_id, research_cmd=research_cmd,
        data_dir=data_dir, network=network, expect_isolation=expect_isolation,
    )


def main() -> int:
    import argparse

    from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
        SandboxResearchExecutor,
    )
    from safety_auto_research.platform_contracts.enums import StageStatus

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--task", default=None,
        help="Run only one representative (e.g. autolab.safety_router). "
             "Default: run all three.",
    )
    args = ap.parse_args()

    # Absolute host data dirs (mounted read-only into the sandbox as /data).
    oss = os.path.join(REPO, "data", "oss")
    reps = [
        _rep(
            "autolab.safety_router",
            "run_research_sandbox",
            ["python", "/repo/scripts/sandbox_examples/run_safety_router_sandbox.py",
             "--data-dir", "/data/", "--hidden-dim", "128", "--epochs", "240"],
            os.path.join(oss, "autolab.safety_router"),
            "none", "hard",
        ),
        _rep(
            "sab.h_importances_92",
            "run_research_sandbox",
            ["python", "/repo/scripts/sandbox_examples/run_sab_sandbox.py",
             "--data-dir", "/data/"],
            os.path.join(oss, "sab.h_importances_92"),
            "none", "hard",
        ),
        _rep(
            "claudini.injection_tmeoa",
            "run_research_sandbox",
            ["python", "/repo/scripts/sandbox_examples/run_claudini_tmeoa_sandbox.py",
             "--data-dir", "/data/", "--model", "qwen3.6-35b-a3b", "--objective",
             "logprob", "--budget", "40", "--samples", "0,1,2"],
            os.path.join(oss, "claudini_tmeoa"),
            "bridge", "soft",  # needs outbound network to tmeoa
        ),
    ]

    if args.task:
        reps = [r for r in reps if r["name"] == args.task]
        if not reps:
            print(f"unknown task '{args.task}'; choices: "
                  f"{', '.join(r['name'] for r in reps)}")
            return 2

    overall_ok = True
    summary = []
    for r in reps:
        print(f"\n===== {r['name']} (cap={r['cap_id']}, net={r['network']}) =====")
        # Isolation mode for this rep: claudini must reach tmeoa -> soft/host; the
        # offline reps should get hard (docker) isolation.
        if r["expect_isolation"] == "soft":
            os.environ["AGENT_SANDBOX_DISABLE"] = "1"
            os.environ.pop("AGENT_SANDBOX", None)
        else:
            os.environ["AGENT_SANDBOX"] = "1"
            os.environ.pop("AGENT_SANDBOX_DISABLE", None)

        params = {
            "_capability_id": r["cap_id"],
            "research_cmd": r["research_cmd"],
            "data_dir": r["data_dir"],
            "result_name": "result.json",
            "network": r["network"],
            "_objective_snapshot": {
                "eval_metric": "accuracy", "direction": "higher", "target_value": 0.8,
            },
        }
        ex = SandboxResearchExecutor()
        sdk = FakeSDK()
        stage_run = SimpleNamespace(
            run_id=f"e2e-{r['name']}", stage_run_id="s0", _sdk=sdk, input_refs=[],
        )
        res = ex.execute(stage_run, sdk, params)
        print(f"  final_status = {res.final_status}")
        print(f"  detail       = {res.detail}")

        ok = res.final_status == StageStatus.SUCCEEDED
        ev = sdk.events[0] if sdk.events else None
        iso = (res.detail.split("isolation=")[1].split()[0]
               if "isolation=" in res.detail else "unknown")
        if ev is None:
            print("  [FAIL] no EvalCompletedEvent emitted")
            ok = False
        else:
            primary = ev.metrics.get("primary")
            print(f"  event.passed = {ev.passed}")
            print(f"  event.metrics= {ev.metrics}")
            print(f"  isolation    = {iso} (expect {r['expect_isolation']})")
            if not (isinstance(primary, (int, float))):
                print(f"  [FAIL] no real primary metric in event: {primary}")
                ok = False
        if iso != r["expect_isolation"]:
            print(f"  [WARN] isolation {iso} != expected {r['expect_isolation']} "
                  f"(env limitation, not a contract failure)")
        print(f"  => {'PASS' if ok else 'FAIL'}")
        overall_ok = overall_ok and ok
        summary.append({"task": r["name"], "ok": ok, "isolation": iso,
                        "status": str(res.final_status)})

    print("\n===== L1 OSS SANDBOX SUMMARY =====")
    print(json.dumps(summary, indent=2))
    print("=== L1 OSS SANDBOX E2E:", "PASS ===" if overall_ok else "FAIL ===")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
