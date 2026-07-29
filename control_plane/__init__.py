from __future__ import annotations

"""Unified control plane skeleton for the safety algorithm R&D platform.

Wires the canonical contract models (WorkflowRun, StageRun, DecisionRecord)
into a minimal API and state-machine service. This is the Phase 1 control-plane
MVP described in `doc/unified_safety_rd_platform_architecture_spec.md`.
"""

from .service import ControlPlaneService

try:
    # The FastAPI server is an optional extra; the core service + schemas must stay
    # importable without it (e.g. agent-driven runs, scripts, tests).
    from .api import create_app

    __all__ = ["ControlPlaneService", "create_app"]
except ImportError:  # pragma: no cover - fastapi not installed
    __all__ = ["ControlPlaneService"]
