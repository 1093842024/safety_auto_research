#!/usr/bin/env python3
"""Sandbox runner for task ``arbor.algotune_knn`` (Arbor / AlgoTune efficiency).

Runs INSIDE the disposable Docker sandbox (no network, read-only /data). It loads
the official, materialised task definition (``task.py`` — problem generator,
reference solver, independent correctness verifier) and the *reference solution*
(``solution.py``) from the read-only ``/data`` mount, checks the solution passes
the correctness gate on every instance, then times the solution against the
reference solver and reports the real **speedup** = median(reference_time) /
median(solution_time). Higher is better; the baseline is 1.0. A solution that
fails the correctness gate on *any* instance scores 0.0 (gate_passed=False).

This is the platform research methodology for an efficiency task: the dual loop
would substitute an agent that edits ``solution.py`` to search for a faster k-NN
implementation, and the harness scores it honestly under confinement.

Usage (driven by scripts/build_agent_sandbox.sh):
    python /repo/scripts/sandbox_examples/run_arbor_algotune_knn_sandbox.py --data-dir /data
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time

# The official task definition + reference solution live on the read-only /data mount.
_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/data")
if _DATA_DIR not in sys.path:
    sys.path.insert(0, _DATA_DIR)

import numpy as np  # noqa: E402

# Pin BLAS / OpenMP to one thread so the measured time reflects the *algorithm*,
# not the core count (matches eval.sh) — keeps speedups comparable run-to-run.
for _t in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_t, "1")
os.environ["PYTHONHASHSEED"] = "0"

import task  # type: ignore  # noqa: E402  (task.py lives on /data)
import solution  # type: ignore  # noqa: E402

# Speedup gate: must beat (or match) the 1.0 baseline AND pass correctness.
SPEEDUP_THRESHOLD = 1.0
DEV_SEED_BASE = 1000
TEST_SEED_BASE = 9000


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


def build_dataset(split: str, n_instances: int) -> list[dict]:
    base = DEV_SEED_BASE if split == "dev" else TEST_SEED_BASE
    n_db = int(os.environ.get("KNN_N_DB", 2000))
    n_query = int(os.environ.get("KNN_N_QUERY", 200))
    dim = int(os.environ.get("KNN_DIM", 16))
    return [task.generate_problem(base + i, n_db, n_query, dim) for i in range(n_instances)]


def time_fn(fn, problems: list[dict], trials: int) -> float:
    """Median wall-clock seconds to process the whole instance set once."""
    for p in problems:  # warm up caches / BLAS handles
        fn(p)
    times = []
    for _ in range(trials):
        t0 = time.perf_counter()
        for p in problems:
            fn(p)
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=None, help="override (default AGENT_DATA_DIR)")
    ap.add_argument("--split", choices=["dev", "test"], default="dev")
    ap.add_argument("--instances", type=int, default=3)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    data_dir = args.data_dir or _DATA_DIR
    # Runner-contract fallback: if the expected sentinel is missing, fall back to
    # the host-provided AGENT_DATA_DIR (soft/host mode remaps /data -> AGENT_DATA_DIR).
    _SENTINEL = "task.py"
    if not os.path.exists(os.path.join(data_dir, _SENTINEL)):
        data_dir = os.environ.get("AGENT_DATA_DIR", data_dir)

    isolation = {
        "data_readonly": probe_readonly(data_dir),
        "network_blocked": probe_network_blocked(),
    }

    problems = build_dataset(args.split, args.instances)

    # Correctness gate — every instance must pass before timing matters.
    correctness_pass = True
    bad_instance = -1
    for i, p in enumerate(problems):
        out = solution.solve(p)
        if not task.is_solution(p, out):
            correctness_pass = False
            bad_instance = i
            break

    if not correctness_pass:
        metrics = {
            "speedup": 0.0,
            "reference_median_s": 0.0,
            "solution_median_s": 0.0,
            "primary": 0.0,
        }
        result = {
            "task_id": "arbor.algotune_knn",
            "eval_metric": "speedup",
            "threshold": SPEEDUP_THRESHOLD,
            "op": "higher",
            "passed": False,
            "gate_passed": False,
            "metrics": metrics,
            "details": {"correctness": "FAIL", "bad_instance": bad_instance, "split": args.split},
            "isolation": isolation,
            "port_notes": ["no ports opened; CPU-only, offline"],
        }
        scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
        os.makedirs(scratch, exist_ok=True)
        with open(os.path.join(scratch, args.result_name), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(json.dumps(result, indent=2))
        return 0

    ref_t = time_fn(task.reference_solver, problems, args.trials)
    sol_t = time_fn(solution.solve, problems, args.trials)
    speedup = float(ref_t / sol_t) if sol_t > 0 else 0.0

    metrics = {
        "speedup": round(speedup, 4),
        "reference_median_s": round(ref_t, 6),
        "solution_median_s": round(sol_t, 6),
        "primary": round(speedup, 4),
    }
    passed = bool(speedup >= SPEEDUP_THRESHOLD) and correctness_pass

    result = {
        "task_id": "arbor.algotune_knn",
        "eval_metric": "speedup",
        "threshold": SPEEDUP_THRESHOLD,
        "op": "higher",
        "passed": passed,
        "gate_passed": correctness_pass,
        "metrics": metrics,
        "details": {"correctness": "PASS", "split": args.split},
        "isolation": isolation,
        "port_notes": ["no ports opened; CPU-only, offline"],
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
