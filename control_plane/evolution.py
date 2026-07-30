"""Evolutionary search over the inner-loop configuration space (Phase 3).

Implements the population mechanics of the harness-evolution playbook (AlphaEvolve /
ShinkaEvolve / DGM, cf. the gap-analysis doc §第三期) against the platform's existing
dual-loop primitives:

  * **Population** — candidates are points on the *editable surface* (the same
    whitelist the meta-loop may patch: fe / model / cv_folds), each tracked with
    fitness, novelty, generation, parentage and offspring count.
  * **Fitness-proportional parent selection** — ShinkaEvolve-style balancing: weight
    grows with fitness rank and shrinks with how many offspring a candidate already
    produced (sample-efficient exploration, anti-elitism).
  * **Novelty rejection sampling** — a candidate whose hashed-embedding cosine
    similarity to anything already archived exceeds the threshold is rejected before
    spending an evaluation (diversity-collapse guard, Weng bottleneck #4).
  * **Run-scoped archive** — ``EvolutionArchive`` is the fifth cumulative store
    (SQLite table ``evolution_candidates``); every entry carries ``run_id``
    (isolation invariant #3).

The heavy lifting (parallel fitness evaluation, audit of generation champions) lives
in ``ClosedLoopOrchestrator.run_evolutionary_loop``; this module stays free of
control-plane side effects so it is unit-testable in isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# Default editable surface (mirrors the meta-loop whitelist; overridable by caller).
DEFAULT_SURFACE: dict[str, tuple[Any, ...]] = {
    "fe": ("basic", "rich"),
    "model": ("gbm", "gbm-strong", "rf", "logreg"),
    "cv_folds": (2, 3, 5, 8),
}


@dataclass
class Candidate:
    candidate_id: str
    run_id: str
    params: dict[str, Any]
    generation: int
    parent_id: str | None = None
    branch: str = "main"
    fitness: float | None = None
    novelty: float = 1.0  # 1 = maximally novel, 0 = exact duplicate
    status: str = "proposed"  # proposed | evaluated | rejected_novelty | champion
    metrics: dict[str, Any] = field(default_factory=dict)
    offspring_count: int = 0


# ---------------------------------------------------------------------- embedding
def _stable_hash(tok: str) -> int:
    """M4 fix: process-stable hash (builtin ``hash()`` is randomized per process via
    PYTHONHASHSEED, which broke the seed-based reproducibility promise)."""

    return int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "little")


def _embed(text: str, dim: int = 256) -> list[float]:
    """Deterministic dependency-free embedding: hashed bag-of-tokens, L2-normalized."""

    vec = [0.0] * dim
    for tok in re.findall(r"[a-z0-9一-鿿]+", text.lower()):
        vec[_stable_hash(tok) % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def candidate_text(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


def max_similarity(params: dict[str, Any], archived: list[dict[str, Any]]) -> float:
    """Cosine similarity of this config to the most similar archived config."""

    if not archived:
        return 0.0
    v = _embed(candidate_text(params))
    return max(_cosine(v, _embed(candidate_text(a))) for a in archived)


# ---------------------------------------------------------------------- operators
def seed_population(
    base_params: dict[str, Any],
    *,
    surface: dict[str, tuple[Any, ...]],
    size: int,
    run_id: str,
    rng: Any,
) -> list[Candidate]:
    """Generation 0: the base configuration plus distinct random surface points."""

    pop = [
        Candidate(
            candidate_id=_new_id("cand"),
            run_id=run_id,
            params=dict(base_params),
            generation=0,
            branch="gen0",
        )
    ]
    seen = {candidate_text(base_params)}
    guard = 0
    while len(pop) < size and guard < size * 20:
        guard += 1
        params = dict(base_params)
        for key, allowed in surface.items():
            params[key] = rng.choice(list(allowed))
        key = candidate_text(params)
        if key in seen:
            continue
        seen.add(key)
        pop.append(
            Candidate(
                candidate_id=_new_id("cand"),
                run_id=run_id,
                params=params,
                generation=0,
                branch="gen0",
            )
        )
    return pop


def select_parent(
    evaluated: list[Candidate],
    *,
    rng: Any,
) -> Candidate | None:
    """ShinkaEvolve-style parent sampling: fitness rank ↑ / offspring count ↓."""

    pool = [c for c in evaluated if c.fitness is not None]
    if not pool:
        return None
    pool.sort(key=lambda c: c.fitness or 0.0, reverse=True)
    n = len(pool)
    weights = [
        ((n - rank) / n) / (1.0 + c.offspring_count) for rank, c in enumerate(pool)
    ]
    return rng.choices(pool, weights=weights, k=1)[0]


def mutate(
    parent: Candidate,
    *,
    surface: dict[str, tuple[Any, ...]],
    run_id: str,
    generation: int,
    rng: Any,
) -> Candidate:
    """Single-point mutation on the editable surface (bounded edit, Self-Harness)."""

    params = dict(parent.params)
    editable = [k for k in surface if k in params or True]
    key = rng.choice(editable)
    allowed = [v for v in surface[key] if v != params.get(key)]
    if allowed:
        params[key] = rng.choice(allowed)
    return Candidate(
        candidate_id=_new_id("cand"),
        run_id=run_id,
        params=params,
        generation=generation,
        parent_id=parent.candidate_id,
        branch=f"gen{generation}",
    )


def novelty_filter(
    candidates: list[Candidate],
    *,
    archived_params: list[dict[str, Any]],
    threshold: float = 0.92,
) -> list[Candidate]:
    """Reject near-duplicates before evaluation (diversity-collapse guard)."""

    kept: list[Candidate] = []
    seen = list(archived_params)
    for c in candidates:
        sim = max_similarity(c.params, seen)
        c.novelty = round(1.0 - sim, 4)
        if sim >= threshold:
            c.status = "rejected_novelty"
            continue
        kept.append(c)
        seen.append(c.params)
    return kept


# ---------------------------------------------------------------------- archive
class EvolutionArchive:
    """Run-scoped candidate history (SQLite or in-memory)."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._cands: dict[str, Candidate] = {}
        if db_path:
            self._init_db()

    def _init_db(self) -> None:
        assert self.db_path is not None
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS evolution_candidates ("
                "candidate_id TEXT PRIMARY KEY, run_id TEXT, data TEXT)"
            )
            conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            for cid, _rid, data in conn.execute(
                "SELECT candidate_id, run_id, data FROM evolution_candidates"
            ):
                self._cands[cid] = Candidate(**json.loads(data))

    def _persist(self, c: Candidate) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evolution_candidates VALUES (?,?,?)",
                (c.candidate_id, c.run_id, json.dumps(asdict(c), ensure_ascii=False)),
            )
            conn.commit()

    def add(self, c: Candidate) -> Candidate:
        self._cands[c.candidate_id] = c
        self._persist(c)
        return c

    def update(self, candidate_id: str, **fields: Any) -> Candidate | None:
        c = self._cands.get(candidate_id)
        if c is None:
            return None
        for k, v in fields.items():
            setattr(c, k, v)
        self._persist(c)
        return c

    def list(self, run_id: str | None = None) -> list[Candidate]:
        out = list(self._cands.values())
        if run_id is not None:
            out = [c for c in out if c.run_id == run_id]
        return out

    def best(self, run_id: str) -> Candidate | None:
        evaluated = [c for c in self.list(run_id) if c.fitness is not None]
        if not evaluated:
            return None
        return max(evaluated, key=lambda c: c.fitness or 0.0)
