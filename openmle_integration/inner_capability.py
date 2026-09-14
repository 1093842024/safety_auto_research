"""Inner-loop-only enforcement seam for the atomic operators (Phase B).

The four atomic operators (Draft / Improve / Debug / Crossover) are *inner-loop*
capabilities. They MUST NEVER run on the outer-audit path (``layer_11_external_audit``)
or the meta-loop (``layer_09``) — that is isolation invariant #1 from the project's
design (operators only ever see curated inner-loop context, never the audit input).

This module codifies that invariant as a single checked call so it cannot be
violated by accident when the operators are wired into the orchestrator.
"""

from __future__ import annotations

# The four atomic MLE program-evolution operators (OpenMLE-Evo alignment).
ATOMIC_OPERATORS = ("draft", "improve", "debug", "crossover")

# Stages on which an operator must NEVER be invoked (they are outer-loop / meta-loop /
# evaluation-standard stages).
INNER_LOOP_FORBIDDEN_CALLERS = {
    "layer_11_external_audit",
    "layer_09_self_iterative_evolution",
    # layer_12 builds the rubric the run is graded against; it must stay free of any
    # program-generation side effects, and no operator may run on that path.
    "layer_12_rubric_induction",
}
# Kept for documentation / defense-in-depth; the allowlist below is the
# authoritative gate (P2-2 fix: a whitelist instead of a blacklist so that a
# missing or unrecognized caller_stage can never silently pass).

# The ONLY caller stages from which an atomic operator may run. Anything else
# (None, an outer/meta-loop stage, or an unrecognized string) is rejected.
INNER_LOOP_ALLOWED_CALLERS = {"inner", "inner_program_evolution"}


class OperatorAuditViolation(RuntimeError):
    """Raised when an atomic operator is invoked from a non-inner-loop stage."""


def assert_operator_inner_only(operator: str, caller_stage: str | None = None) -> None:
    """Enforce that ``operator`` runs only inside the inner loop.

    Raises :class:`OperatorAuditViolation` if ``caller_stage`` is not an explicitly
    allowed inner-loop stage (this covers ``None``, unrecognized stages, and the
    outer/meta-loop stages), and :class:`ValueError` for an unknown operator name.

    P2-2 fix: previously a blacklist was used, so a missing / unrecognized
    ``caller_stage`` (e.g. a caller that forgot to pass it) passed silently and
    could have routed an operator onto the outer-audit / meta-loop path. The
    allowlist makes the guard fail-closed.
    """

    if operator not in ATOMIC_OPERATORS:
        raise ValueError(f"unknown atomic operator: {operator!r} (expected one of {ATOMIC_OPERATORS})")
    if caller_stage not in INNER_LOOP_ALLOWED_CALLERS:
        raise OperatorAuditViolation(
            f"atomic operator {operator!r} may only run on an inner-loop stage "
            f"(one of {sorted(INNER_LOOP_ALLOWED_CALLERS)}), got caller_stage={caller_stage!r}; "
            "operators are inner-loop-only (isolation invariant #1)."
        )
