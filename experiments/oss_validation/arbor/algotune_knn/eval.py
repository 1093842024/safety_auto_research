"""Evaluate solution.py: correctness gate + median-of-N timing vs the reference.

Prints exactly one machine-readable metric line that Arbor reads:

    score: <speedup>

where ``speedup = median(reference_time) / median(solution_time)`` measured on a
held-out set of problem instances. Higher is better (maximize). A solution that
fails the correctness check on *any* instance scores ``0.0``.

Dev and test use disjoint seed ranges, so the signal Arbor iterates on is never
the same data it is finally judged on:

    dev  seeds: 1000 .. 1000 + N - 1
    test seeds: 9000 .. 9000 + N - 1

Problem size (and therefore runtime) is tunable via environment variables —
the AlgoTune ``--target-time-ms`` analogue:

    KNN_N_DB, KNN_N_QUERY, KNN_DIM, KNN_INSTANCES, KNN_TRIALS
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

import solution
import task

DEV_SEED_BASE = 1000
TEST_SEED_BASE = 9000


def build_dataset(split: str, n_instances: int) -> list[dict]:
    base = DEV_SEED_BASE if split == "dev" else TEST_SEED_BASE
    n_db = int(os.environ.get("KNN_N_DB", 2000))
    n_query = int(os.environ.get("KNN_N_QUERY", 200))
    dim = int(os.environ.get("KNN_DIM", 16))
    return [
        task.generate_problem(base + i, n_db, n_query, dim)
        for i in range(n_instances)
    ]


def time_fn(fn, problems: list[dict], trials: int) -> float:
    """Median wall-clock seconds to process the whole instance set once."""
    # Warm up caches / JIT / BLAS handles so the first trial isn't an outlier.
    for p in problems:
        fn(p)
    times = []
    for _ in range(trials):
        t0 = time.perf_counter()
        for p in problems:
            fn(p)
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", choices=["dev", "test"], default="dev")
    ap.add_argument(
        "--instances",
        type=int,
        default=int(os.environ.get("KNN_INSTANCES", 3)),
    )
    ap.add_argument(
        "--trials",
        type=int,
        default=int(os.environ.get("KNN_TRIALS", 5)),
    )
    args = ap.parse_args()

    problems = build_dataset(args.split, args.instances)

    # Correctness gate — every instance must pass before timing matters.
    for i, p in enumerate(problems):
        out = solution.solve(p)
        if not task.is_solution(p, out):
            print(f"split: {args.split}")
            print(f"correctness: FAIL on instance {i}")
            print("score: 0.0")
            return

    print(f"split: {args.split}")
    print("correctness: PASS")

    ref_t = time_fn(task.reference_solver, problems, args.trials)
    sol_t = time_fn(solution.solve, problems, args.trials)
    speedup = ref_t / sol_t if sol_t > 0 else 0.0

    print(f"reference_median_s: {ref_t:.6f}")
    print(f"solution_median_s: {sol_t:.6f}")
    print(f"score: {speedup:.4f}")


if __name__ == "__main__":
    main()
