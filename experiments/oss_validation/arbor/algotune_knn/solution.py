"""EDITABLE solver — Arbor improves THIS file (and only this file).

Implement ``solve(problem)`` so it returns an ``(n_query, k)`` integer array
where row ``i`` holds the indices of the ``k`` database points nearest to query
``i`` (Euclidean distance). Output must satisfy ``task.is_solution``.

The starting implementation is deliberately naive — identical in spirit to the
reference (full pairwise distances + full sort), so the initial speedup is about
1.0x. Your job is to make it faster while staying correct. Ideas the search may
explore: partial selection (``np.argpartition``) instead of a full sort, the
``|x - y|^2 = |x|^2 - 2 x·y + |y|^2`` GEMM expansion, dtype/layout tweaks, or
blocking over queries. None of these change the output — only the runtime.
"""

from __future__ import annotations

import numpy as np


def solve(problem: dict) -> np.ndarray:
    database = problem["database"]
    queries = problem["queries"]
    k = problem["k"]
    diff = queries[:, None, :] - database[None, :, :]
    d2 = np.sum(diff * diff, axis=2)
    idx = np.argpartition(d2, k - 1, axis=1)[:, :k]
    top_d2 = np.take_along_axis(d2, idx, axis=1)
    order = np.argsort(top_d2, axis=1)
    return np.take_along_axis(idx, order, axis=1)
