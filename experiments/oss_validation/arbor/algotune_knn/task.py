"""Task definition for the brute-force k-NN AlgoTune-style example.

PROTECTED FILE — do not edit during a research run.

It defines the three pieces every AlgoTune-style task needs:

  * ``generate_problem`` — deterministic problem instances from a seed.
  * ``reference_solver`` — the baseline implementation (the timing denominator
    AND the thing your solution must match output-for-output).
  * ``is_solution``      — an *independent* correctness verifier that recomputes
    the ground truth, so neither the reference nor a candidate can cheat.

Only ``solution.py`` may be edited to make the computation faster. The metric
is the speedup of ``solution.solve`` over ``reference_solver`` (higher = better),
gated on ``is_solution`` passing on every instance.
"""

from __future__ import annotations

import numpy as np

# Number of nearest neighbours to return per query.
K = 10


def generate_problem(
    seed: int,
    n_db: int = 2000,
    n_query: int = 200,
    dim: int = 16,
) -> dict:
    """Return one deterministic k-NN problem instance.

    A problem is a database of ``n_db`` points, a batch of ``n_query`` query
    points (both in ``dim`` dimensions), and the number of neighbours ``k``.
    """
    rng = np.random.default_rng(seed)
    database = rng.standard_normal((n_db, dim)).astype(np.float64)
    queries = rng.standard_normal((n_query, dim)).astype(np.float64)
    return {"database": database, "queries": queries, "k": K}


def reference_solver(problem: dict) -> np.ndarray:
    """Baseline: full pairwise distances + full sort, take the k smallest.

    Correct but intentionally unoptimised — this is the speedup baseline.
    Returns an ``(n_query, k)`` int array of database indices, where each row
    lists a query's k nearest neighbours (Euclidean distance).
    """
    database = problem["database"]
    queries = problem["queries"]
    k = problem["k"]
    # Full broadcast distances -> (n_query, n_db). O(n_query * n_db * dim) memory.
    diff = queries[:, None, :] - database[None, :, :]
    d2 = np.sum(diff * diff, axis=2)
    # Full sort of every row, then slice the first k. O(n_db log n_db) per query.
    idx = np.argsort(d2, axis=1)[:, :k]
    return idx


def is_solution(problem: dict, solution) -> bool:
    """Independently verify the returned indices are the true k nearest.

    Tie-robust: instead of comparing indices (ambiguous under ties), it compares
    the *sorted distances* of the candidate's chosen neighbours against the true
    k smallest distances. This validates the reference too, so a solution cannot
    pass by returning fast-but-wrong output.
    """
    database = problem["database"]
    queries = problem["queries"]
    k = problem["k"]

    sol = np.asarray(solution)
    if sol.shape != (queries.shape[0], k):
        return False

    # Accept integer indices (or float arrays that hold integral values).
    if not np.issubdtype(sol.dtype, np.integer):
        if not np.all(np.isfinite(sol)) or not np.all(sol == np.floor(sol)):
            return False
        sol = sol.astype(np.int64)

    if sol.size == 0 or sol.min() < 0 or sol.max() >= database.shape[0]:
        return False

    # Indices within a row must be distinct (no neighbour counted twice).
    row_sorted = np.sort(sol, axis=1)
    if np.any(row_sorted[:, 1:] == row_sorted[:, :-1]):
        return False

    # Ground-truth k smallest squared distances per query.
    diff = queries[:, None, :] - database[None, :, :]
    d2 = np.sum(diff * diff, axis=2)
    true_topk = np.sort(np.partition(d2, k - 1, axis=1)[:, :k], axis=1)

    # Candidate's chosen distances, sorted, must match within tolerance.
    cand = np.take_along_axis(d2, sol, axis=1)
    cand_sorted = np.sort(cand, axis=1)
    return bool(np.allclose(cand_sorted, true_topk, rtol=1e-9, atol=1e-9))
