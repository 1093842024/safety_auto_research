"""EDITABLE solver — Arbor improves THIS file (and only this file).

Materialised into ``data/oss/arbor.algotune_knn/`` as the *reference solution*
the platform runs. It returns an ``(n_query, k)`` integer array of the k nearest
database neighbours (Euclidean) per query, satisfying ``task.is_solution``.

The shipped baseline is the naive full-pairwise + full-sort implementation
(``speedup ~ 1.0``). This version keeps the *output identical* (same k nearest
neighbours) but is genuinely faster via two standard, output-preserving tricks:

  * the ``|x-y|^2 = |x|^2 - 2 x·y + |y|^2`` GEMM distance expansion, which
    avoids the ``(n_query, n_db, dim)`` broadcast intermediate that dominates the
    reference's runtime, and
  * ``np.argpartition`` partial selection instead of a full per-row sort.

Both only change *how fast* the same neighbours are computed; the correctness
verifier (``task.is_solution``) recomputes ground-truth distances independently
and confirms the chosen indices are the true k nearest (within rtol/atol 1e-9).
"""

from __future__ import annotations

import numpy as np


def solve(problem: dict) -> np.ndarray:
    database = np.asarray(problem["database"], dtype=np.float64)
    queries = np.asarray(problem["queries"], dtype=np.float64)
    k = int(problem["k"])

    # Squared Euclidean distances via the GEMM expansion:
    #   ||q - d||^2 = ||q||^2 + ||d||^2 - 2 q·d
    # (n_query, n_db). Avoids the O(n_query*n_db*dim) broadcast of the reference.
    db_sq = np.einsum("ij,ij->i", database, database)          # (n_db,)
    q_sq = np.einsum("ij,ij->i", queries, queries)             # (n_query,)
    d2 = q_sq[:, None] + db_sq[None, :] - 2.0 * (queries @ database.T)

    # Partial top-k selection (no full sort): pick the k smallest, then sort the
    # chosen k for a stable, ascending-distance output row.
    topk = np.argpartition(d2, k, axis=1)[:, :k]
    order = np.argsort(np.take_along_axis(d2, topk, axis=1), axis=1)
    return np.take_along_axis(topk, order, axis=1).astype(np.int64)
