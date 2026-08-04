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

# Stages on which an operator must NEVER be invoked (they are outer-loop / meta-loop).
INNER_LOOP_FORBIDDEN_CALLERS = {
    "layer_11_external_audit",
    "layer_09_self_iterative_evolution",
}


class OperatorAuditViolation(RuntimeError):
    """Raised when an atomic operator is invoked from a forbidden (outer) stage."""


def assert_operator_inner_only(operator: str, caller_stage: str | None = None) -> None:
    """Enforce that ``operator`` runs only inside the inner loop.

    Raises :class:`OperatorAuditViolation` if ``caller_stage`` is an outer/meta-loop
    stage, and :class:`ValueError` for an unknown operator name.
    """

    if operator not in ATOMIC_OPERATORS:
        raise ValueError(f"unknown atomic operator: {operator!r} (expected one of {ATOMIC_OPERATORS})")
    if caller_stage in INNER_LOOP_FORBIDDEN_CALLERS:
        raise OperatorAuditViolation(
            f"atomic operator {operator!r} must not run on outer stage {caller_stage!r}; "
            "operators are inner-loop-only (isolation invariant #1)."
        )
