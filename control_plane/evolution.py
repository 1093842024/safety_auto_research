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

  * **Phase A — dual evolution grain** — each ``Candidate`` now carries ``node_kind``
    (``"config"`` for the legacy hyperparameter surface, ``"program"`` for code candidates
    produced by an atomic operator). ``code`` / ``operator`` / ``parent_ids`` support the
    program-level grain introduced by OpenMLE-Evo; all fields are defaulted so existing
    config nodes and persisted JSON remain valid. ``list_by_kind`` filters by grain.

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
import threading
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
    # --- Phase A: program-level evolution extension (OpenMLE-Evo alignment) ---
    # node_kind distinguishes the two evolution grains that now coexist in one archive:
    #   "config"  -> hyperparameter-config candidate (legacy, deterministic)
    #   "program" -> code candidate produced by an atomic operator (Draft/Improve/Debug/Crossover)
    # All new fields are defaulted so existing config nodes and persisted JSON remain valid.
    node_kind: str = "config"
    code: str | None = None
    operator: str | None = None  # draft | improve | debug | crossover (program nodes only)
    parent_ids: list[str] | None = None  # multi-parent support for Crossover


# ---------------------------------------------------------------------- embedding
def _stable_hash(tok: str) -> int:
    """M4 fix: process-stable hash (builtin ``hash()`` is randomized per process via
    PYTHONHASHSEED, which broke the seed-based reproducibility promise)."""

    return int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "little")


def _embed_tokens(tokens: list[str], dim: int = 256) -> list[float]:
    """Hashed bag-of-tokens over an *explicit* token list, L2-normalized."""

    vec = [0.0] * dim
    for tok in tokens:
        vec[_stable_hash(tok) % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _embed(text: str, dim: int = 256) -> list[float]:
    """Deterministic dependency-free embedding: hashed bag-of-tokens, L2-normalized."""

    return _embed_tokens(re.findall(r"[a-z0-9一-鿿]+", text.lower()), dim=dim)


# R6 fix: keys that are *run context*, not search dimensions. They are byte-identical
# for every candidate in a population, so including them in the novelty fingerprint
# only dilutes the signal — and ``data_dir`` is catastrophic: an absolute path
# tokenizes into a dozen shared tokens (users/glennge/work/github/...), pushing the
# cosine between two genuinely different models (gbm vs rf) up to 0.972, above the
# 0.92 threshold. The whole population then gets rejected as "not novel" and the
# evolutionary search silently degenerates to a single candidate.
NON_SEARCH_KEYS: frozenset[str] = frozenset({
    "data_dir", "preset", "threshold", "target", "id_col", "task_id", "run_id",
    "output_dir", "work_dir", "seed", "_capability_id",
})


def novelty_tokens(params: dict[str, Any]) -> list[str]:
    """Fingerprint tokens for novelty scoring: one ``key=value`` token per search dim.

    Two changes vs. the old "tokenize the whole JSON blob" approach:
      * run-context keys (:data:`NON_SEARCH_KEYS`) are dropped entirely;
      * each surviving entry contributes exactly **one** token, so a long value
        (a path, a list of dropped columns) cannot outweigh the model choice.

    With the default surface (``fe`` / ``model`` / ``cv_folds``) a single-gene
    difference now yields cosine ≈ 0.67, comfortably below the 0.92 threshold, while
    an exact duplicate still scores 1.0.
    """

    toks: list[str] = []
    for k in sorted(params):
        if k in NON_SEARCH_KEYS:
            continue
        toks.append(f"{k}={json.dumps(params[k], sort_keys=True, ensure_ascii=False)}")
    return toks


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    # L4 fix: an empty/zero embedding (e.g. an empty config) has zero norm, so its
    # cosine with any vector is 0.0 — meaning two *identical* empty configs were
    # never flagged as duplicates. Treat zero-vs-zero as identical, zero-vs-nonzero
    # as dissimilar.
    if dot == 0.0:
        a_zero = all(v == 0.0 for v in a)
        b_zero = all(v == 0.0 for v in b)
        return 1.0 if (a_zero and b_zero) else 0.0
    return dot


def candidate_text(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


def parse_island_index(branch: str) -> int:
    """Extract the island index from a branch tag.

    Program-level (OpenMLE-Evo) branches are ``gen{g}.isl{i}`` -> returns ``i``.
    Config-level branches are ``gen{g}`` (no island) -> returns ``0``.

    Used by the frontend "island view" to colour nodes by island of origin.
    """

    if not branch:
        return 0
    m = re.search(r"isl(\d+)", branch)
    return int(m.group(1)) if m else 0


def max_similarity(params: dict[str, Any], archived: list[dict[str, Any]]) -> float:
    """Cosine similarity of this config to the most similar archived config.

    R6 fix: scores over :func:`novelty_tokens` (search dimensions only) instead of the
    raw ``candidate_text`` JSON blob. ``candidate_text`` is still the *identity* key
    used for exact dedup elsewhere and is intentionally left untouched.
    """

    if not archived:
        return 0.0
    v = _embed_tokens(novelty_tokens(params))
    return max(_cosine(v, _embed_tokens(novelty_tokens(a))) for a in archived)


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
    idx = rng.choices(range(n), weights=weights, k=1)[0]
    chosen = pool[idx]
    chosen.offspring_count += 1
    return chosen


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
    editable = [k for k in surface if k in params]
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


def candidate_signature(c: Candidate) -> str:
    """The comparison key for novelty: program code for program nodes, else config."""

    if c.node_kind == "program" and c.code:
        return c.code
    return candidate_text(c.params)


def novelty_filter(
    candidates: list[Candidate],
    *,
    archived_params: list[dict[str, Any]] | None = None,
    archived_codes: list[str] | None = None,
    threshold: float = 0.92,
) -> list[Candidate]:
    """Reject near-duplicates before evaluation (diversity-collapse guard).

    Two grains, two rules:
      * **config** nodes -- cosine similarity over the param dict (legacy rule); a
        candidate within ``threshold`` of any archived config is rejected.
      * **program** nodes -- EXACT source-code dedup. Our operator templates share
        ~95% boilerplate, so a cosine-over-bag-of-tokens metric cannot tell a RandomForest
        draft from a GradientBoosting draft (sim ~1.0); exact-code membership correctly
        rejects only truly identical programs while keeping different models/operators.
    """

    kept: list[Candidate] = []
    seen_params = list(archived_params or [])
    seen_code_set = set(archived_codes or [])
    for c in candidates:
        if c.node_kind == "program":
            sig = candidate_signature(c)
            is_dup = sig in seen_code_set
            c.novelty = 0.0 if is_dup else 1.0
        else:
            sim = max_similarity(c.params, seen_params) if seen_params else 0.0
            is_dup = sim >= threshold
            c.novelty = round(1.0 - sim, 4)
        if is_dup:
            c.status = "rejected_novelty"
            continue
        kept.append(c)
        seen_params.append(c.params)
        seen_code_set.add(candidate_signature(c))
    return kept


# ---------------------------------------------------------------------- program-level operators (Phase B/C)
# The SAME EvolutionArchive now holds two grains: "config" (hyperparameter surface,
# legacy) and "program" (code produced by the atomic operators Draft/Improve/Debug/
# Crossover). These helpers are pure (no execution); the caller runs the code via the
# OpenMLETaskAdapter and fills in ``fitness``. All generated candidates carry
# node_kind="program" + code + operator + parent_ids so the tree/archive stay coherent.
def seed_program_population(
    backend: Any,
    *,
    size: int,
    run_id: str,
    rng: Any,
    target: str = "Survived",
    id_col: str = "PassengerId",
    feature_cols: tuple[str, ...] = (),
    task_description: str = "",
    generation: int = 0,
) -> list[Candidate]:
    """Generation 0 of program nodes: Draft produces ``size`` distinct solution programs."""

    from ..openmle_integration.operators import draft_program, draft_template_count

    pop: list[Candidate] = []
    seen: set[str] = set()
    guard = 0
    n_templates = max(1, draft_template_count())
    while len(pop) < size and guard < size * 20:
        guard += 1
        # Explore the model space (sklearn + any importable open-domain framework) for a
        # diverse seed pop; the count is read from the backend so the two never drift.
        variant = rng.randrange(n_templates)
        code = draft_program(
            backend, target=target, id_col=id_col, task_description=task_description,
            variant=variant, caller_stage="inner_program_evolution",
        )
        if code in seen:
            continue
        seen.add(code)
        pop.append(Candidate(
            candidate_id=_new_id("prog"),
            run_id=run_id,
            params={"operator": "draft"},
            generation=generation,
            branch=f"gen{generation}",
            node_kind="program",
            code=code,
            operator="draft",
        ))
    return pop


def mutate_program(
    parent: Candidate,
    operator: str,
    backend: Any,
    *,
    run_id: str,
    generation: int,
    rng: Any,
    target: str = "Survived",
    id_col: str = "PassengerId",
    feature_cols: tuple[str, ...] = (),
    task_description: str = "",
    feedback: str | None = None,
) -> Candidate:
    """Improve or Debug a parent program node into a child (single-parent transform)."""

    if operator not in ("improve", "debug"):
        raise ValueError(f"mutate_program operator must be improve/debug, got {operator!r}")
    from ..openmle_integration.operators import debug_program, improve_program

    base = parent.code or ""
    if operator == "improve":
        code = improve_program(
            backend, target=target, id_col=id_col, task_description=task_description,
            current_program=base, feedback=feedback, caller_stage="inner_program_evolution",
        )
    else:
        code = debug_program(
            backend, target=target, id_col=id_col, task_description=task_description,
            current_program=base, feedback=feedback, caller_stage="inner_program_evolution",
        )
    return Candidate(
        candidate_id=_new_id("prog"),
        run_id=run_id,
        params={"operator": operator, "parent": parent.candidate_id},
        generation=generation,
        parent_id=parent.candidate_id,
        branch=f"gen{generation}",
        node_kind="program",
        code=code,
        operator=operator,
        parent_ids=[parent.candidate_id],
    )


def crossover_programs(
    a: Candidate,
    b: Candidate,
    backend: Any,
    *,
    run_id: str,
    generation: int,
    target: str = "Survived",
    id_col: str = "PassengerId",
    feature_cols: tuple[str, ...] = (),
    task_description: str = "",
) -> Candidate:
    """Combine two program nodes into a child (multi-parent transform, OpenMLE Crossover)."""

    from ..openmle_integration.operators import crossover_program

    code = crossover_program(
        backend, target=target, id_col=id_col, task_description=task_description,
        parent_programs=(a.code or "", b.code or ""), caller_stage="inner_program_evolution",
    )
    return Candidate(
        candidate_id=_new_id("prog"),
        run_id=run_id,
        params={"operator": "crossover", "parents": [a.candidate_id, b.candidate_id]},
        generation=generation,
        parent_id=a.candidate_id,
        branch=f"gen{generation}",
        node_kind="program",
        code=code,
        operator="crossover",
        parent_ids=[a.candidate_id, b.candidate_id],
    )


class IslandModel:
    """OpenMLE-Evo island model over program populations (Phase C).

    Each island evolves its own program population; ``migrate`` injects the global
    top-k program candidates into every *other* island, providing the cross-island
    gene flow that prevents premature convergence. Island origin is encoded in the
    candidate branch tag (``gen{g}.isl{i}``) so ``migrate`` can exclude it.
    """

    def __init__(self, n_islands: int, run_id: str) -> None:
        self.n_islands = n_islands
        self.run_id = run_id
        self.islands: list[list[Candidate]] = [[] for _ in range(n_islands)]

    @staticmethod
    def _island_of(branch: str) -> int | None:
        if ".isl" in branch:
            try:
                return int(branch.rsplit(".isl", 1)[1])
            except ValueError:
                return None
        return None

    def seed(self, seeder: Any, *, size: int, generation: int = 0) -> None:
        for idx, island in enumerate(self.islands):
            for c in seeder(size=size, generation=generation):
                c.branch = f"gen{generation}.isl{idx}"
                island.append(c)

    def all_candidates(self) -> list[Candidate]:
        out: list[Candidate] = []
        for isl in self.islands:
            out.extend(isl)
        return out

    def best(self) -> Candidate | None:
        evaluated = [c for c in self.all_candidates() if c.fitness is not None]
        return max(evaluated, key=lambda c: c.fitness or 0.0) if evaluated else None

    def migrate(self, top_k: int = 1, current_generation: int | None = None, archive: Any = None) -> None:
        ranked = sorted(
            (c for c in self.all_candidates() if c.fitness is not None),
            key=lambda c: c.fitness or 0.0, reverse=True,
        )[:top_k]
        for champ in ranked:
            origin = self._island_of(champ.branch)
            for idx, isl in enumerate(self.islands):
                if idx == origin:
                    continue
                dest_gen = current_generation if current_generation is not None else champ.generation
                clone = Candidate(
                    candidate_id=_new_id("mig"),
                    run_id=self.run_id,
                    params=dict(champ.params),
                    generation=dest_gen,  # use destination island's current generation
                    parent_id=champ.candidate_id,
                    branch=f"gen{dest_gen}.isl{idx}",  # relabel to destination island
                    node_kind=champ.node_kind,
                    code=champ.code,
                    operator=champ.operator,
                    parent_ids=list(champ.parent_ids or [champ.candidate_id]),
                    # P2-6 fix: carry the champion's known fitness/metrics and mark
                    # the clone evaluated so it is imported as a known-good parent
                    # on the destination island (not silently re-evaluated).
                    fitness=champ.fitness,
                    metrics=dict(champ.metrics or {}),
                    status="evaluated",
                )
                isl.append(clone)
                # P2-6 fix: register the clone in the archive so the exact-code
                # novelty filter (archive.list_by_kind(..., "program")) can de-dup
                # against it; without this, migrated champions could seed duplicate
                # programs across islands.
                if archive is not None:
                    archive.add(clone)


# ---------------------------------------------------------------------- archive
class EvolutionArchive:
    """Run-scoped candidate history (SQLite or in-memory)."""

    def __init__(self, db_path: str | None = None) -> None:
        self._lock = threading.RLock()
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
        with self._lock:
            self._cands[c.candidate_id] = c
            self._persist(c)
            return c

    def update(self, candidate_id: str, **fields: Any) -> Candidate | None:
        with self._lock:
            c = self._cands.get(candidate_id)
            if c is None:
                return None
            for k, v in fields.items():
                setattr(c, k, v)
            self._persist(c)
            return c

    def list(self, run_id: str | None = None) -> list[Candidate]:
        with self._lock:
            out = list(self._cands.values())
            if run_id is not None:
                out = [c for c in out if c.run_id == run_id]
            return out

    def list_by_kind(self, run_id: str, kind: str) -> list[Candidate]:
        """Return only candidates of a given ``node_kind`` (config | program) for a run.

        Enables the frontend/EvolutionPanel to show the two evolution grains separately
        without mixing hyperparameter-config nodes with code-operator nodes.
        """
        with self._lock:
            return [c for c in self.list(run_id) if c.node_kind == kind]

    def best(self, run_id: str) -> Candidate | None:
        with self._lock:
            evaluated = [c for c in self.list(run_id) if c.fitness is not None]
            if not evaluated:
                return None
            return max(evaluated, key=lambda c: c.fitness or 0.0)
