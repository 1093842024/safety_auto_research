"""FastAPI application assembler for the unified control plane.

Backlog A1 + A3: this module used to be a 1975-line monolith that declared ~57 route
handlers inside a single ``create_app`` closure. It is now a thin assembler:

* **A3 (DI boundary)** — shared runtime dependencies (``ControlPlaneService``,
  ``ResearchStateStore``, ``ClosedLoopOrchestrator``, cancellation registry, shutdown
  event, progress bus) plus the helper closures they backed (``build_run_ctx``,
  ``spawn_full_experiment``, ``make_agent_orchestrator``, ``resolve_eval_params``) live
  in :mod:`control_plane.deps` as :class:`~control_plane.deps.ControlPlaneDeps`.
* **A1 (router split)** — route handlers were moved verbatim into per-domain routers
  under :mod:`control_plane.routers`; each ``build_*_router(deps)`` binds the shared
  dependencies as local aliases so handler bodies are unchanged.

``create_app(service=None)`` keeps its original signature and route table (same paths,
methods, response models, status codes, summaries and tags), so callers and tests are
unaffected.
"""

from __future__ import annotations

from fastapi import FastAPI

from . import agent_settings
from .deps import ControlPlaneDeps
from .deps import _graceful_shutdown
from .deps import build_deps

# Backend processes get started by several operators (scripts / IDE watchers /
# manual runs). Apply the agent-CLI + Docker-sandbox env defaults BEFORE any
# router/deps construction, so AGENT_COMMAND / AGENT_SANDBOX are always correct
# regardless of how this module was launched (operator-set values win).
agent_settings.apply_env_defaults()
from .routers import build_benchmarks_router
from .routers import build_datasets_router
from .routers import build_evolution_router
from .routers import build_experiments_router
from .routers import build_flywheel_router
from .routers import build_integrity_router
from .routers import build_llm_router
from .routers import build_loops_router
from .routers import build_observability_router
from .routers import build_research_records_router
from .routers import build_research_skills_router
from .routers import build_settings_router
from .routers import build_workflow_runs_router
from .service import ControlPlaneService

# Registration order defines the FastAPI route-matching order. It mirrors the original
# top-to-bottom declaration order of the monolithic api.py; all paths use distinct
# literal suffixes, so grouping by domain does not change matching behaviour.
_ROUTER_BUILDERS = (
    build_workflow_runs_router,
    build_loops_router,
    build_evolution_router,
    build_observability_router,
    build_benchmarks_router,
    build_research_records_router,
    build_experiments_router,
    build_flywheel_router,
    # LLM debug surface (auto-label prompts + provider config) — registered before
    # the integrity suite so the LLM paths are visible at their natural position.
    build_llm_router,
    # System settings (agent-CLI model selection etc.) — stateless w.r.t. deps.
    build_settings_router,
    # Dataset management (数据集管理): register/inspect local train/eval datasets.
    build_datasets_router,
    # Research-skill knowledge base (研究 Skill): AREX-Skill 索引/检索/详情.
    build_research_skills_router,
    # Opt-in integrity gates (spark-to-paper integration). Registered last and
    # always: the ``INTEGRITY_GATES`` env switch gates the endpoints' *behaviour*,
    # not their registration, so the OpenAPI surface never depends on the
    # environment. Disabled by default -> the default control plane is unchanged.
    build_integrity_router,
)


def create_app(service: ControlPlaneService | None = None) -> FastAPI:
    """Build the FastAPI application for the unified control plane."""

    app = FastAPI(
        title="Safety R&D Unified Control Plane",
        version="0.1.0",
        description=(
            "Minimal control-plane API wiring WorkflowRun / StageRun / DecisionRecord "
            "into a validated state machine. See platform_contracts for the canonical schemas."
        ),
    )

    @app.on_event("shutdown")
    def _on_shutdown():
        _graceful_shutdown()

    deps: ControlPlaneDeps = build_deps(service)
    # Exposed for tests / introspection: the app's dependency container.
    app.state.deps = deps

    # Startup reconciliation: dual-loop drivers are in-process threads, so any
    # run persisted as "running" from a previous process life is dead with
    # certainty — mark it failed instead of showing an eternal "运行中" zombie.
    try:
        from . import liveness

        _orphans = liveness.reconcile_orphaned_runs(deps.svc)
        if _orphans:
            import logging

            logging.getLogger(__name__).warning(
                "启动对账：%d 个遗留的运行中 run 已自动标记为异常终止: %s",
                len(_orphans),
                ", ".join(_orphans[:10]),
            )
    except Exception:
        pass

    for build_router in _ROUTER_BUILDERS:
        app.include_router(build_router(deps))

    return app
