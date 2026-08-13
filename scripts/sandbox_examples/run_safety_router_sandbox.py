#!/usr/bin/env python3
"""Sandbox runner for task ``autolab.safety_router`` (Harbor/Arbor representative).

Runs INSIDE the disposable Docker sandbox. Reads the official task data from the
read-only ``/data`` mount (copied from the autolab task environment), trains the
2-layer MLP defined in ``model.py`` (hash-pinned, read-only), evaluates on
``test_public.npz``, and writes a structured ``result.json`` the host-side
``SandboxResearchExecutor`` reads back to emit a real ``EvalCompletedEvent``.

This realises the platform research methodology for the task: the *agent* (here
modelled by a scripted baseline trainer — the dual loop would substitute an LLM
agent that searches the architecture) produces a model, the harness scores it
under confinement, and the metric is recorded honestly.

Usage (driven by scripts/build_agent_sandbox.sh):
    python /repo/scripts/sandbox_examples/run_safety_router_sandbox.py --data-dir /data
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import numpy as np

# The official scripts + data are on the read-only /data mount.
_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/data")
if _DATA_DIR not in sys.path:
    sys.path.insert(0, _DATA_DIR)

from model import (  # noqa: E402  (model.py lives in /data)
    compute_metrics,
    count_trainable_params,
    predict_labels,
    train_router,
)

# Evaluation gates taken verbatim from the autolab safety_router task spec.
GATES = {"accuracy": 0.64, "unsafe_recall": 0.66, "safe_recall": 0.57}


def probe_readonly(data_dir: str) -> bool:
    probe = os.path.join(data_dir, ".write_test_%d" % os.getpid())
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


def probe_network_blocked() -> bool:
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=2):
            return False
    except OSError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=None, help="override (default AGENT_DATA_DIR)")
    ap.add_argument("--hidden-dim", type=int, default=128)
    ap.add_argument("--epochs", type=int, default=240)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    data_dir = args.data_dir or _DATA_DIR
    if not os.path.exists(data_dir):
        # soft/host mode: the launcher maps /data -> AGENT_DATA_DIR
        data_dir = os.environ.get("AGENT_DATA_DIR", data_dir)
    isolation = {
        "data_readonly": probe_readonly(data_dir),
        "network_blocked": probe_network_blocked(),
    }

    with np.load(os.path.join(data_dir, "train.npz")) as d:
        X_train, y_train = d["X"], d["y"].astype(np.int64)
    with np.load(os.path.join(data_dir, "val.npz")) as d:
        X_val, y_val = d["X"], d["y"].astype(np.int64)
    with np.load(os.path.join(data_dir, "test_public.npz")) as d:
        X_test, y_test = d["X"], d["y"].astype(np.int64)

    model = train_router(
        X_train=X_train,
        y_train=y_train,
        X_val=X_val,
        y_val=y_val,
        seed=args.seed,
        hidden_dim=args.hidden_dim,
        epochs=args.epochs,
    )
    pred = predict_labels(model, X_test)
    m = compute_metrics(y_test, pred)
    total_params = int(count_trainable_params(model))

    metrics = {
        "accuracy": round(float(m.accuracy), 4),
        "unsafe_recall": round(float(m.unsafe_recall), 4),
        "safe_recall": round(float(m.safe_recall), 4),
        "total_params": total_params,
        "primary": round(float(m.accuracy), 4),
    }
    passed = (
        float(m.accuracy) >= GATES["accuracy"]
        and float(m.unsafe_recall) >= GATES["unsafe_recall"]
        and float(m.safe_recall) >= GATES["safe_recall"]
    )

    result = {
        "task_id": "autolab.safety_router",
        "eval_metric": "accuracy",
        "threshold": GATES,
        "op": "ge",
        "passed": bool(passed),
        "gate_passed": bool(passed),
        "metrics": metrics,
        "isolation": isolation,
        "report_ref": "oss://autolab.safety_router",
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
