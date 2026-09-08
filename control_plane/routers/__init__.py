"""Per-domain FastAPI routers for the control plane (backlog item A1)."""

from __future__ import annotations

from .workflow_runs import build_workflow_runs_router
from .loops import build_loops_router
from .evolution import build_evolution_router
from .observability import build_observability_router
from .benchmarks import build_benchmarks_router
from .research_records import build_research_records_router
from .experiments import build_experiments_router
from .integrity import build_integrity_router

__all__ = [
    "build_workflow_runs_router",
    "build_loops_router",
    "build_evolution_router",
    "build_observability_router",
    "build_benchmarks_router",
    "build_research_records_router",
    "build_experiments_router",
    "build_integrity_router",
]
