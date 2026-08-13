"""End-to-end sandbox-chain verification for a classification modality.

This reproduces, in-process, the exact path the ``POST /benchmark-tasks/{id}/launch``
endpoint takes once a real agent (codex/claude) is wired in AND the operator has
sandboxing enabled (``AGENT_SANDBOX=1``):

  1. The launch handler resolves a custom ``text_classification`` task to
     ``sandbox_capability = "text_cls_sandbox"`` and folds its registration
     values into ``inner_agent_config`` (task_type / text_col / label_col /
     data_subdir / threshold / eval_metric) — see control_plane/routers/benchmarks.py.
  2. The dual loop drives ``run_capability("text_cls_sandbox", ...)`` -> the
     ``SandboxResearchExecutor`` routes the capability to
     ``scripts/sandbox_examples/run_text_cls_sandbox.py`` and (with AGENT_SANDBOX=1)
     executes it *inside* the disposable Docker container
     (``safety-research-sandbox:latest``, read-only data, --network none).
  3. The container writes ``result.json``; the host-side launcher writes an
     *unforgeable* isolation attestation (R9), and the executor emits a real
     ``EvalCompletedEvent`` back to the control plane.

We skip the (heavy, network/API-dependent) codex research agent itself and drive
the executor directly with the very ``params`` the launch handler would produce,
so the sandbox chain — the part under test — is exercised for real, in Docker.

Usage:
    AGENT_SANDBOX=1 python scripts/verify_classification_sandbox_e2e.py
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


# ---- fake control-plane SDK (the executor only needs these surface calls) ----
class _AnyAttr:
    def __getattr__(self, name):
        return None


class FakeSDK:
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


def main() -> int:
    # 0) operator has sandboxing enabled (the precondition for hard isolation)
    if os.environ.get("AGENT_SANDBOX") != "1":
        print("[setup] AGENT_SANDBOX=1 required -> exporting it for this run")
        os.environ["AGENT_SANDBOX"] = "1"

    from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
        SandboxResearchExecutor,
    )
    from safety_auto_research.platform_contracts.enums import StageStatus

    # 1) params exactly as benchmarks.py launch handler folds them for a
    #    custom text_classification task (mirrors lines ~364-373 of benchmarks.py).
    params = {
        "_capability_id": "text_cls_sandbox",
        "task_type": "text_classification",
        "text_col": "text",
        "label_col": "label",
        "data_subdir": "text_cls_demo",
        "threshold": 0.8,
        "eval_metric": "f1_macro",
        "op": "ge",
        "epochs": 3,
        "_objective_snapshot": {
            "eval_metric": "f1_macro",
            "direction": "higher",
            "target_value": 0.8,
        },
    }

    ex = SandboxResearchExecutor()
    sdk = FakeSDK()
    stage_run = SimpleNamespace(run_id="e2e-text-cls", stage_run_id="s0", _sdk=sdk,
                                input_refs=[])

    print("== executing text_cls_sandbox capability inside Docker sandbox ==")
    res = ex.execute(stage_run, sdk, params)

    print(f"  final_status      = {res.final_status}")
    print(f"  gate_result       = {res.gate_result}")
    print(f"  detail            = {res.detail}")

    # 2) assertions — the real chain must hold
    ok = True
    if res.final_status != StageStatus.SUCCEEDED:
        print("  [FAIL] executor did not succeed"); ok = False

    # isolation must be HARD (real docker), proven by the launcher attestation
    isolation_mode = (res.detail.split("isolation=")[1].split()[0]
                      if "isolation=" in res.detail else "unknown")
    print(f"  isolation_mode    = {isolation_mode}  (expect 'hard')")
    if isolation_mode != "hard":
        print("  [FAIL] not executed under hard (docker) isolation"); ok = False

    # a real EvalCompletedEvent must have been emitted
    if not sdk.events:
        print("  [FAIL] no EvalCompletedEvent emitted"); ok = False
    else:
        ev = sdk.events[0]
        print(f"  event.passed      = {ev.passed}")
        print(f"  event.metrics     = {ev.metrics}")
        primary = ev.metrics.get("primary")
        if not (isinstance(primary, (int, float)) and 0.0 <= primary <= 1.0):
            print(f"  [FAIL] event metric out of range: {primary}"); ok = False
        else:
            print(f"  [OK] real metric produced: primary={primary:.4f}")

    print("\n" + ("=== CLASSIFICATION SANDBOX E2E: PASS ===" if ok
                    else "=== CLASSIFICATION SANDBOX E2E: FAIL ==="))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
