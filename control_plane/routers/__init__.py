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
from .flywheel import build_flywheel_router
from .llm import build_llm_router
from .settings import build_settings_router
from .datasets import build_datasets_router
from .research_skills import build_research_skills_router

__all__ = [
    "build_workflow_runs_router",
    "build_loops_router",
    "build_evolution_router",
    "build_observability_router",
    "build_benchmarks_router",
    "build_research_records_router",
    "build_experiments_router",
    "build_integrity_router",
    "build_flywheel_router",
    "build_llm_router",
    "build_settings_router",
    "build_datasets_router",
    "build_research_skills_router",
]
