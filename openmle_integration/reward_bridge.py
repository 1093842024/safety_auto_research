"""Phase D1 -- Local bridge for the OpenMLE-RL *external* training pipeline.

OpenMLE-RL lives OUTSIDE this repo (CC BY-NC 4.0 license + no GPU/docker in this
environment), so we deliberately do **not** vendor its training code. Instead we
re-implement the *semantic contract* of its ``reward_func_utils.py`` /
``program_database.py`` reward shaping: turn a program-evolution candidate's fitness
and auxiliary signals into an RL-style scalar reward with interpretable components.

This decouples safety_auto_research from aira-evo / slime (whose relative imports would
fail if copied verbatim) while keeping the same reward decomposition, so:
  * the program-evolution loop (Phase C) can emit reward-shaped signals, and
  * an external RL trainer (or the local trainer from Phase D2) can consume them with
    no import coupling.

The decomposition mirrors OpenMLE-RL:
    total = w_validity * validity_bonus * valid
          + w_improve * improvement_scale * max(0, fitness - prev_fitness)
          + w_diverse * diversity_scale * novelty
          - w_parsimony * complexity_penalty_per_kloc * kloc
          + baseline
clamped to [min_reward, max_reward].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .contracts import FAILED_STATUSES

# Reward component keys (mirror OpenMLE-RL's reward decomposition).
VALIDITY = "validity"
IMPROVEMENT = "improvement"
DIVERSITY = "diversity"
PARSIMONY = "parsimony"
BASELINE = "baseline"


DEFAULT_WEIGHTS: dict[str, float] = {
    VALIDITY: 1.0,
    IMPROVEMENT: 2.0,
    DIVERSITY: 0.5,
    PARSIMONY: 0.2,
}


@dataclass
class RewardComponents:
    """One candidate's decomposed reward (all sub-scores + the clamped total)."""

    validity: float = 0.0
    improvement: float = 0.0
    diversity: float = 0.0
    parsimony: float = 0.0
    baseline: float = 0.0
    total: float = 0.0


@dataclass
class RewardConfig:
    """Tunable weights / scales for the reward decomposition."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    validity_bonus: float = 1.0
    improvement_scale: float = 1.0  # multiply the raw fitness delta
    diversity_scale: float = 1.0  # multiply the raw novelty in [0, 1]
    complexity_penalty_per_kloc: float = 0.1  # penalty per 1000 lines of code
    baseline: float = 0.0
    min_reward: float = -1.0
    max_reward: float = 5.0


def _code_complexity(program: Optional[str]) -> float:
    """Rough program complexity in kloc (non-blank lines / 1000)."""

    if not program:
        return 0.0
    n_lines = sum(1 for ln in program.splitlines() if ln.strip())
    return n_lines / 1000.0


def reward_func(
    fitness: Optional[float],
    *,
    valid: bool = True,
    prev_fitness: Optional[float] = None,
    novelty: float = 1.0,
    program: Optional[str] = None,
    config: Optional[RewardConfig] = None,
) -> RewardComponents:
    """Compute an RL-style reward for a single program-evolution candidate.

    Args:
        fitness: candidate fitness (e.g. cv_accuracy). ``None`` => failed candidate.
        valid: whether the program produced a VALID_SOLUTION.
        prev_fitness: best fitness from the previous generation (for improvement).
        novelty: novelty score in [0, 1] (1.0 = brand new program).
        program: source code, used for the parsimony penalty.
        config: optional :class:`RewardConfig` override.

    Returns:
        :class:`RewardComponents` with each sub-score and the clamped ``total``.
    """

    cfg = config or RewardConfig()
    w = cfg.weights

    validity = cfg.validity_bonus if valid else 0.0
    fit = fitness if fitness is not None else 0.0
    improvement = 0.0
    if prev_fitness is not None:
        improvement = max(0.0, (fit - prev_fitness) * cfg.improvement_scale)
    diversity = max(0.0, min(1.0, novelty)) * cfg.diversity_scale
    parsimony = _code_complexity(program) * cfg.complexity_penalty_per_kloc

    total = (
        w.get(VALIDITY, 1.0) * validity
        + w.get(IMPROVEMENT, 2.0) * improvement
        + w.get(DIVERSITY, 0.5) * diversity
        - w.get(PARSIMONY, 0.2) * parsimony
        + cfg.baseline
    )
    total = max(cfg.min_reward, min(cfg.max_reward, total))

    return RewardComponents(
        validity=round(validity, 6),
        improvement=round(improvement, 6),
        diversity=round(diversity, 6),
        parsimony=round(parsimony, 6),
        baseline=cfg.baseline,
        total=round(total, 6),
    )


def reward_population(
    candidates: Sequence,
    *,
    prev_best_fitness: Optional[float] = None,
    config: Optional[RewardConfig] = None,
) -> list[RewardComponents]:
    """Score a whole population the way an RL trainer would batch-reward it.

    Each ``candidate`` is duck-typed for ``.fitness`` / ``.novelty`` / ``.code`` /
    ``.status`` (matches :class:`~safety_auto_research.control_plane.evolution.Candidate`).
    A candidate with ``status`` starting with ``"rejected"`` or ``valid=False`` is
    treated as an invalid solution.
    """

    out: list[RewardComponents] = []
    for c in candidates:
        if prev_best_fitness is None:
            prev = None
        else:
            prev = prev_best_fitness
        valid = not (getattr(c, "status", "") or "").startswith("rejected")
        status = getattr(c, "status", "")
        valid = status not in FAILED_STATUSES and not (status or "").startswith("rejected")
        out.append(
            reward_func(
                getattr(c, "fitness", None),
                valid=valid,
                prev_fitness=prev,
                novelty=getattr(c, "novelty", 1.0) or 1.0,
                program=getattr(c, "code", None),
                config=config,
            )
        )
    return out
