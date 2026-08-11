"""Sandbox-backed research capability executor (F3, agent-mode generalization).

This is the *promotion* of the single-task F3 sandbox path into
``run_capability``'s agent mode: any agent-mode research capability that has a
real in-container research command is executed **inside a disposable Docker
container** (hard isolation) instead of on the host.

Two modes, both driven by ``params``:

* **kaggle / tabular** -- ``params`` carries ``preset`` (or ``custom`` + ``target``
  + ``data_subdir`` + ``threshold`` + ``eval_metric`` + ``op``). The executor
  builds a command that runs
  ``scripts/sandbox_examples/run_kaggle_eval_sandbox.py`` in-container, reads
  ``result_<preset>.json``, and emits a real ``EvalCompletedEvent``. This is the
  path that turns the formerly "empty-run" tabular benchmark tasks into real,
  isolated, measured runs.
* **generic ``research_cmd``** -- ``params["research_cmd"]`` is an argv list (or a
  shell string) that already uses in-container paths (``/repo/...``, ``/data/...``).
  The executor runs it in the same sandbox and reads back ``result.json``. This is
  the path the 16 non-tabular tracked-only tasks are wired to: when a task has a
  runnable script + dataset the same hard-isolation path runs it; when it has none
  the run *fails honestly* (no silent empty-run) with a clear "no in-repo executor"
  detail.

Isolation is delegated to ``scripts/build_agent_sandbox.sh`` (read-only data,
ephemeral scratch, ``--network none``, dropped caps, no-new-privileges, resource
limits). If Docker is unavailable it falls back to host execution (soft
isolation) so the capability contract still holds -- the agent-facing tool path
never breaks -- *unless* hard isolation is required, see below.

**Isolation integrity (R9).** Two rules govern how much the platform trusts a
sandboxed measurement:

1. *Fail closed on demand.* Set ``AGENT_SANDBOX_REQUIRE_HARD=1`` (env) or pass
   ``params["require_hard"]=True`` and a run that cannot obtain a real container
   FAILS instead of quietly executing unconfined research code on the host.
2. *Unforgeable attestation.* The isolation level recorded on the artifact comes
   from a marker file the host-side launcher writes **outside** the scratch mount,
   never from ``result.json``. The measured program owns its result file and can
   write ``{"isolation": {"network_blocked": true}}`` into it regardless of how it
   was actually executed; that self-report is now kept only as an untrusted hint.

Only the *research command* runs in the container; the agent (codex/claude) and
the control plane stay on the host. The host executor reads ``result.json`` from
the scratch mount and reconstructs the event, so the closed loop / leaderboard see
a genuine measurement produced under confinement.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import EvalCompletedEvent
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK

_REPO_ROOT = Path(__file__).resolve().parents[2]  # .../safety_auto_research
_BUILD_SCRIPT = _REPO_ROOT / "scripts" / "build_agent_sandbox.sh"
_SANDBOX_SCRIPT = _REPO_ROOT / "scripts" / "sandbox_examples" / "run_kaggle_eval_sandbox.py"
_BASH = "/bin/bash"

# Capabilities that should route into the sandbox when AGENT_SANDBOX=1 is set.
SANDBOX_CAPABILITY_IDS = frozenset(
    {
        "kaggle_eval", "kaggle_eval_sandbox", "run_research_sandbox",
        "text_cls_sandbox", "image_cls_sandbox", "audio_cls_sandbox",
        "embedding_sandbox",
    }
)


# Exit code the launcher uses when hard isolation was required but unavailable
# (EX_CONFIG). Kept in sync with scripts/build_agent_sandbox.sh.
_EX_CONFIG = 78


def hard_isolation_required(params: dict[str, Any] | None) -> bool:
    """Whether this invocation refuses to fall back to host (soft) execution.

    Opt in per-call with ``params["require_hard"]`` or globally with
    ``AGENT_SANDBOX_REQUIRE_HARD=1``.
    """

    params = params or {}
    if params.get("require_hard") is not None:
        return bool(params["require_hard"])
    return os.environ.get("AGENT_SANDBOX_REQUIRE_HARD") == "1"


def sandbox_requested(capability_id: str, params: dict[str, Any] | None) -> bool:
    """Whether a capability invocation should execute inside the sandbox.

    True when the caller explicitly opts in (``params["sandbox"] is True``), or when
    the operator enables sandboxing globally (``AGENT_SANDBOX=1``) for a
    sandbox-eligible capability.
    """

    params = params or {}
    if params.get("sandbox") is True:
        return True
    if os.environ.get("AGENT_SANDBOX") == "1" and capability_id in SANDBOX_CAPABILITY_IDS:
        return True
    return False


class SandboxResearchExecutor(StageExecutor):
    """Runs a research command inside the Docker sandbox and emits a real event."""

    stage_codes = ("kaggle_eval_sandbox", "run_research_sandbox")

    # ------------------------------------------------------------------ planning
    def _plan(
        self, stage_run: Any, params: dict[str, Any]
    ) -> tuple[list[str], str, str, str, dict[str, Any]]:
        """Return (container_command, host_data_dir, scratch_dir, result_name, eval_meta).

        ``container_command`` uses in-container paths (/repo/..., /data/...);
        ``host_data_dir`` is the host path the sandbox mounts read-only to /data.
        """

        params = params or {}
        cap_id = params.get("_capability_id", "")
        home = os.environ.get("HOME", "/tmp")

        # ---- generic research_cmd path ------------------------------------------
        research_cmd = params.get("research_cmd")
        if research_cmd:
            if isinstance(research_cmd, str):
                cmd: list[str] = shlex.split(research_cmd)
            else:
                cmd = [str(c) for c in research_cmd]
            data_dir = params.get("data_dir") or str(_REPO_ROOT / "data" / "kaggle")
            result_name = params.get("result_name", "result.json")
            eval_meta = {
                "eval_suite_id": params.get("eval_suite_id", f"sandbox-{cap_id or 'research'}"),
                "report_ref": params.get("report_ref", f"sandbox://{cap_id or 'research'}"),
            }
            scratch = self._make_scratch(home, stage_run, cap_id or "research")
            return cmd, data_dir, scratch, result_name, eval_meta

        # ---- text classification path (F2: break the data bottleneck) ----------
        # text_classification is the most feasible non-kaggle modality: TF-IDF +
        # linear model runs in the sklearn-only sandbox image (no torch/GPU).
        if cap_id == "text_cls_sandbox" or params.get("task_type") == "text_classification" or str(
            params.get("preset") or ""
        ) == "text_cls":
            obj = self._objective(params, stage_run)
            text_col = str(params.get("text_col") or obj.get("text_col") or "text")
            label_col = str(params.get("label_col") or obj.get("label_col") or "label")
            data_subdir = str(params.get("data_subdir") or obj.get("data_subdir") or "text_cls_demo")
            metric = params.get("eval_metric") or obj.get("eval_metric") or "f1_macro"
            direction = str(obj.get("direction") or "higher").strip().lower()
            op = params.get("op") or ("le" if direction == "lower" else "ge")
            threshold = (
                params.get("threshold")
                if params.get("threshold") is not None
                else obj.get("target_threshold", obj.get("target_value", 0.5))
            )
            threshold = float(threshold) if threshold is not None else 0.5
            cmd = [
                "python",
                "/repo/scripts/sandbox_examples/run_text_cls_sandbox.py",
                "--data-dir", f"/data/{data_subdir}",
                "--text-col", text_col,
                "--label-col", label_col,
                "--eval-metric", str(metric),
                "--op", op,
                "--threshold", str(threshold),
                "--result-name", "result.json",
            ]
            data_dir = params.get("data_dir") or str(_REPO_ROOT / "data" / "benchmark")
            result_name = "result.json"
            eval_meta = {
                "eval_suite_id": params.get("eval_suite_id", f"text-cls-{data_subdir}"),
                "report_ref": params.get("report_ref", f"text-cls://sandbox-{data_subdir}"),
            }
            scratch = self._make_scratch(home, stage_run, "text_cls")
            return cmd, data_dir, scratch, result_name, eval_meta

        # ---- image classification path -----------------------------------------
        if cap_id == "image_cls_sandbox" or params.get("task_type") == "image_classification" or str(
            params.get("preset") or ""
        ) == "image_cls":
            obj = self._objective(params, stage_run)
            data_subdir = str(params.get("data_subdir") or obj.get("data_subdir") or "image_cls_demo")
            arch = str(params.get("arch") or obj.get("arch") or "tiny_cnn")
            epochs = int(params.get("epochs") if params.get("epochs") is not None else obj.get("epochs", 6))
            metric = params.get("eval_metric") or obj.get("eval_metric") or "accuracy"
            direction = str(obj.get("direction") or "higher").strip().lower()
            op = params.get("op") or ("le" if direction == "lower" else "ge")
            threshold = float(
                params.get("threshold")
                if params.get("threshold") is not None
                else obj.get("target_threshold", obj.get("target_value", 0.0))
            )
            cmd = [
                "python",
                "/repo/scripts/sandbox_examples/run_image_cls_sandbox.py",
                "--data-dir", f"/data/{data_subdir}",
                "--arch", arch,
                "--epochs", str(epochs),
                "--eval-metric", str(metric),
                "--op", op,
                "--threshold", str(threshold),
                "--result-name", "result.json",
            ]
            data_dir = params.get("data_dir") or str(_REPO_ROOT / "benchmark_tasks" / "sample_data")
            result_name = "result.json"
            eval_meta = {
                "eval_suite_id": params.get("eval_suite_id", f"image-cls-{data_subdir}"),
                "report_ref": params.get("report_ref", f"image-cls://sandbox-{data_subdir}"),
            }
            scratch = self._make_scratch(home, stage_run, "image_cls")
            return cmd, data_dir, scratch, result_name, eval_meta

        # ---- audio classification path ----------------------------------------
        if cap_id == "audio_cls_sandbox" or params.get("task_type") == "audio_classification" or str(
            params.get("preset") or ""
        ) == "audio_cls":
            obj = self._objective(params, stage_run)
            data_subdir = str(params.get("data_subdir") or obj.get("data_subdir") or "audio_cls_demo")
            feature = str(params.get("feature") or obj.get("feature") or "logmel")
            epochs = int(params.get("epochs") if params.get("epochs") is not None else obj.get("epochs", 8))
            metric = params.get("eval_metric") or obj.get("eval_metric") or "accuracy"
            direction = str(obj.get("direction") or "higher").strip().lower()
            op = params.get("op") or ("le" if direction == "lower" else "ge")
            threshold = float(
                params.get("threshold")
                if params.get("threshold") is not None
                else obj.get("target_threshold", obj.get("target_value", 0.0))
            )
            cmd = [
                "python",
                "/repo/scripts/sandbox_examples/run_audio_cls_sandbox.py",
                "--manifest", f"/data/{data_subdir}/manifest.csv",
                "--data-dir", f"/data/{data_subdir}",
                "--feature", feature,
                "--epochs", str(epochs),
                "--eval-metric", str(metric),
                "--op", op,
                "--threshold", str(threshold),
                "--result-name", "result.json",
            ]
            data_dir = params.get("data_dir") or str(_REPO_ROOT / "benchmark_tasks" / "sample_data")
            result_name = "result.json"
            eval_meta = {
                "eval_suite_id": params.get("eval_suite_id", f"audio-cls-{data_subdir}"),
                "report_ref": params.get("report_ref", f"audio-cls://sandbox-{data_subdir}"),
            }
            scratch = self._make_scratch(home, stage_run, "audio_cls")
            return cmd, data_dir, scratch, result_name, eval_meta

        # ---- embedding (contrastive) path -------------------------------------
        if cap_id == "embedding_sandbox" or params.get("task_type") == "embedding_contrastive" or str(
            params.get("preset") or ""
        ) == "embedding_cls":
            obj = self._objective(params, stage_run)
            data_subdir = str(params.get("data_subdir") or obj.get("data_subdir") or "embedding_demo")
            dim = int(params.get("dim") if params.get("dim") is not None else obj.get("dim", 64))
            epochs = int(params.get("epochs") if params.get("epochs") is not None else obj.get("epochs", 30))
            metric = params.get("eval_metric") or obj.get("eval_metric") or "recall_at_10"
            direction = str(obj.get("direction") or "higher").strip().lower()
            op = params.get("op") or ("le" if direction == "lower" else "ge")
            threshold = float(
                params.get("threshold")
                if params.get("threshold") is not None
                else obj.get("target_threshold", obj.get("target_value", 0.0))
            )
            cmd = [
                "python",
                "/repo/scripts/sandbox_examples/run_embedding_sandbox.py",
                "--data-dir", f"/data/{data_subdir}",
                "--dim", str(dim),
                "--epochs", str(epochs),
                "--eval-metric", str(metric),
                "--op", op,
                "--threshold", str(threshold),
                "--result-name", "result.json",
            ]
            data_dir = params.get("data_dir") or str(_REPO_ROOT / "benchmark_tasks" / "sample_data")
            result_name = "result.json"
            eval_meta = {
                "eval_suite_id": params.get("eval_suite_id", f"embedding-{data_subdir}"),
                "report_ref": params.get("report_ref", f"embedding://sandbox-{data_subdir}"),
            }
            scratch = self._make_scratch(home, stage_run, "embedding")
            return cmd, data_dir, scratch, result_name, eval_meta

        # ---- kaggle / tabular path ----------------------------------------------
        preset = str(params.get("preset") or "titanic")
        target = params.get("target")
        model = str(params.get("model") or "gbm")
        fe = str(params.get("fe") or "basic")

        # Fill threshold / metric / op from params, falling back to the run's
        # objective snapshot, then the preset defaults -- mirrors kaggle_eval_executor.
        obj = self._objective(params, stage_run)
        preset_cfg = _PRESETS.get(preset, {})
        threshold = (
            params.get("threshold")
            if params.get("threshold") is not None
            else obj.get("target_threshold", preset_cfg.get("threshold", 0.82))
        )
        metric = params.get("eval_metric") or obj.get("eval_metric") or "accuracy"
        direction = str(obj.get("direction") or "higher").strip().lower()
        op = params.get("op") or ("le" if direction == "lower" else "ge")

        cmd = [
            "python",
            "/repo/scripts/sandbox_examples/run_kaggle_eval_sandbox.py",
            "--preset", preset,
            "--model", model,
            "--fe", fe,
            "--threshold", str(threshold),
            "--eval-metric", str(metric),
            "--op", op,
        ]
        if target and preset not in _PRESETS:  # custom tabular task
            cmd += ["--target", str(target)]
        if params.get("data_subdir"):
            cmd += ["--data-dir", f"/data/{params['data_subdir']}"]

        data_dir = str(_REPO_ROOT / "data" / "kaggle")
        result_name = params.get("result_name") or f"result_{preset}.json"
        eval_meta = {
            "eval_suite_id": params.get("eval_suite_id", f"kaggle-{preset}-{target or ''}"),
            "report_ref": params.get("report_ref", f"kaggle-eval://sandbox-{preset}-{model}"),
        }
        scratch = self._make_scratch(home, stage_run, f"kaggle_{preset}")
        return cmd, data_dir, scratch, result_name, eval_meta

    @staticmethod
    def _objective(params: dict[str, Any], stage_run: Any) -> dict[str, Any]:
        obj = params.get("_objective_snapshot") or {}
        if obj:
            return obj
        try:
            run = stage_run._sdk.load_object(f"run:{stage_run.run_id}")
            return getattr(run, "objective_snapshot", None) or {}
        except Exception:
            return {}

    @staticmethod
    def _make_scratch(home: str, stage_run: Any, tag: str) -> str:
        # Colima's VM only shares paths under $HOME with the host; never /tmp.
        base = os.path.join(home, ".cache", "agent_sandbox_scratch")
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(prefix=f"run_{stage_run.run_id}_{tag}_", dir=base)
        return d

    @staticmethod
    def _make_marker_path(home: str) -> str:
        """Allocate the launcher's attestation path, deliberately OUTSIDE scratch.

        Scratch is mounted rw into the container and is where the payload writes;
        an attestation stored there would be forgeable by the program it attests to.
        """

        base = os.path.join(home, ".cache", "agent_sandbox_marker")
        os.makedirs(base, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="mode_", suffix=".json", dir=base)
        os.close(fd)
        return path

    @staticmethod
    def _read_marker(path: str) -> dict[str, Any]:
        """Read the launcher attestation. Missing/garbled == unknown (never 'hard')."""

        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ----------------------------------------------------------------- execution
    def execute(
        self,
        stage_run: Any,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        cmd, data_dir, scratch, result_name, eval_meta = self._plan(stage_run, params)

        # R16 fix: ``_make_scratch`` creates a throwaway dir via ``mkdtemp`` that was
        # never removed on ANY of the five return paths — every sandboxed eval leaked a
        # directory under ``~/.cache/agent_sandbox_scratch``. Clean it up unconditionally;
        # set ``AGENT_SANDBOX_KEEP_SCRATCH=1`` to keep it on failure for debugging.
        result: ExecResult | None = None
        try:
            result = self._execute_in_scratch(
                stage_run, sdk, params, cmd=cmd, data_dir=data_dir, scratch=scratch,
                result_name=result_name, eval_meta=eval_meta,
            )
            return result
        finally:
            failed = result is None or result.final_status == StageStatus.FAILED
            if not (failed and os.environ.get("AGENT_SANDBOX_KEEP_SCRATCH") == "1"):
                shutil.rmtree(scratch, ignore_errors=True)

    def _execute_in_scratch(
        self,
        stage_run: Any,
        sdk: PlatformSDK,
        params: dict[str, Any],
        *,
        cmd: list[str],
        data_dir: str,
        scratch: str,
        result_name: str,
        eval_meta: dict[str, Any],
    ) -> ExecResult:
        """Original execute() body; scratch lifetime is owned by execute()."""
        network = str(params.get("network") or "none")

        if not _BUILD_SCRIPT.exists():
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=f"sandbox runner missing: {_BUILD_SCRIPT}",
            )

        require_hard = hard_isolation_required(params)
        marker_path = self._make_marker_path(os.environ.get("HOME", "/tmp"))

        env = os.environ.copy()
        env["AGENT_DATA_DIR"] = data_dir
        env["AGENT_REPO_DIR"] = str(_REPO_ROOT)
        env["AGENT_SCRATCH_DIR"] = scratch
        env["AGENT_SANDBOX_NETWORK"] = network
        env["AGENT_SANDBOX_MARKER_PATH"] = marker_path
        env["AGENT_SANDBOX_REQUIRE_HARD"] = "1" if require_hard else "0"

        try:
            try:
                proc = subprocess.run(
                    [_BASH, str(_BUILD_SCRIPT), "--network", network, "--", *cmd],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=int(params.get("timeout", 600)),
                )
            except subprocess.TimeoutExpired:
                return ExecResult(
                    final_status=StageStatus.FAILED,
                    gate_result=GateResult.FAILED,
                    event=None,
                    output_refs=[],
                    detail=f"sandbox research command timed out after {params.get('timeout', 600)}s",
                )

            marker = self._read_marker(marker_path)
        finally:
            try:
                os.unlink(marker_path)
            except OSError:
                pass

        # R9: the isolation level is whatever the *launcher* recorded. An absent
        # marker means we cannot prove anything -> "unknown", which is treated as
        # not-hard by the gate below.
        isolation_mode = str(marker.get("mode") or "unknown")

        if require_hard and isolation_mode != "hard":
            tail = (proc.stdout + proc.stderr)[-1500:]
            hint = (
                "launcher refused to degrade to host execution"
                if proc.returncode == _EX_CONFIG
                else f"launcher reported isolation={isolation_mode}"
            )
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=(
                    "hard isolation required (AGENT_SANDBOX_REQUIRE_HARD=1 / "
                    f"require_hard=True) but not obtained: {hint} "
                    f"(reason: {marker.get('reason') or 'n/a'}).\n{tail}"
                ),
            )

        result_path = os.path.join(scratch, result_name)
        if proc.returncode != 0 or not os.path.exists(result_path):
            tail = (proc.stdout + proc.stderr)[-1500:]
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=(
                    f"sandbox research command exited {proc.returncode}; "
                    f"no result.json produced (no in-repo executor?).\n{tail}"
                ),
            )

        with open(result_path, encoding="utf-8") as fh:
            res = json.load(fh)

        passed = bool(res.get("passed", False))
        gate_passed = bool(res.get("gate_passed", passed))
        metrics = res.get("metrics", {})
        # Untrusted: written by the measured program itself. Kept for diagnostics
        # only -- never used to decide whether the run was actually confined.
        claimed_isolation = res.get("isolation", {})

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=eval_meta["eval_suite_id"],
            stage_run_id=stage_run.stage_run_id,
            passed=passed,
            metrics=metrics,
            gate_passed=gate_passed,
            report_ref=res.get("report_ref", eval_meta["report_ref"]),
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": "eval_report",
                "uri": event.report_ref,
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={
                "suite": eval_meta["eval_suite_id"],
                "gate_passed": gate_passed,
                "eval_metric": res.get("eval_metric", "accuracy"),
                "model": res.get("model", "gbm"),
                "preset": res.get("preset", "titanic"),
                # R9: derived from the host-side launcher attestation, not from
                # the payload's self-reported isolation block.
                "sandbox": {"hard": "docker", "soft": "soft"}.get(isolation_mode, "unknown"),
                "isolation_mode": isolation_mode,
                "isolation_network": marker.get("network"),
                "isolation_claimed_by_payload": bool(claimed_isolation.get("network_blocked")),
            },
        )
        primary_metric = res.get("eval_metric", "accuracy")
        if primary_metric in metrics:
            sdk.record_metric(
                stage_run.run_id, f"eval.{primary_metric}", float(metrics[primary_metric]),
                tags={"suite": eval_meta["eval_suite_id"], "sandbox": "1"},
            )

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if gate_passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"sandbox kaggle eval [{res.get('preset')}/{res.get('model')}]: "
                f"{primary_metric}={metrics.get('primary')} vs {res.get('op', 'ge')} "
                f"{res.get('threshold')} -> {'PASS' if passed else 'FAIL'}"
                f" | isolation={isolation_mode} (net={marker.get('network', 'n/a')})"
                f" | payload_claim: data_ro={claimed_isolation.get('data_readonly')}"
                f" net_blocked={claimed_isolation.get('network_blocked')}"
            ),
        )


# Mirror of kaggle_eval_executor.PRESETS so the plan step can resolve defaults
# without importing sklearn on the host.
_PRESETS: dict[str, dict[str, Any]] = {
    "titanic": {"target": "Survived", "threshold": 0.82},
    "spaceship": {"target": "Transported", "threshold": 0.80},
    "wine": {"target": "quality", "threshold": 0.78},
    "iris": {"target": "species", "threshold": 0.90},
    "breast_cancer": {"target": "diagnosis", "threshold": 0.92},
}


def build_sandbox_command_preview(params: dict[str, Any]) -> list[str]:
    """Return the in-container command an executor would run (for tool-protocol display)."""

    params = params or {}
    if params.get("research_cmd"):
        return [str(c) for c in params["research_cmd"]]
    preset = str(params.get("preset") or "titanic")
    return [
        "python",
        "/repo/scripts/sandbox_examples/run_kaggle_eval_sandbox.py",
        "--preset", preset,
        "--model", str(params.get("model") or "gbm"),
    ]
