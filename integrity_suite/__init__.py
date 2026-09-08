"""Integrity suite — deterministic, fail-closed integrity checks (additive, opt-in).

Ported/adapted from ``spark-to-paper-skills`` (see
``doc/analysis_spark_to_paper_vs_safety_2026-08-14.md``, items C1/C2/C3/C7). The
suite exists to make *integrity* checkable by code rather than by narrative:

    svg_audit.py    -- geometric SVG defect audit, stdlib-only, no renderer (C2)
    gate_runner.py  -- fail-closed gate scheduler over EXISTING validators (C1)

Design contract (do not break):

* **Additive only.** Nothing here is imported by the default ``run_dual_loop`` /
  ``run_evolutionary_loop`` hot paths. The platform behaves identically whether
  this package is present or not.
* **Zero new dependencies.** Standard library only. ``svg_audit`` needs no
  renderer; ``gate_runner`` needs no web framework.
* **No logic duplication.** ``gate_runner`` *calls* the platform's existing
  validators (``novelty_filter``, ``_select_objective``, ``evaluate_constraint``)
  and only consumes their verdicts. Thresholds live in the gate layer; the
  measurement logic stays in its original home.
* **No side effects on import.** Heavy modules (``control_plane`` and friends,
  which pull pydantic) are imported lazily *inside* the gate functions so this
  package stays importable in a bare stdlib environment.
"""

from __future__ import annotations

__all__ = ["gate_runner", "svg_audit"]
