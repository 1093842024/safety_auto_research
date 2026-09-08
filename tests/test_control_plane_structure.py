"""Structural regression tests for the control-plane refactor (backlog A1 + A3).

A1 split the 1975-line ``control_plane/api.py`` monolith into per-domain routers under
``control_plane/routers/``; A3 introduced ``control_plane/deps.ControlPlaneDeps`` as the
dependency-injection boundary. These tests pin the *contract* of that refactor:

* the HTTP surface (path + method table) is exactly what the monolith exposed;
* every router actually contributes routes and no two routers collide;
* ``api.py`` stays a thin assembler (no route declarations, small);
* routers depend on ``deps``, never back on ``api`` (no import cycle);
* the DI container is reachable and honours an injected service.

If someone later adds an endpoint, EXPECTED_ROUTES must be updated deliberately — that
is the point: the HTTP surface should never drift silently.
"""

from __future__ import annotations

import os
import re
import unittest

from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.deps import ControlPlaneDeps
from safety_auto_research.control_plane.deps import build_deps
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.control_plane.routers import build_benchmarks_router
from safety_auto_research.control_plane.routers import build_evolution_router
from safety_auto_research.control_plane.routers import build_experiments_router
from safety_auto_research.control_plane.routers import build_flywheel_router
from safety_auto_research.control_plane.routers import build_integrity_router
from safety_auto_research.control_plane.routers import build_llm_router
from safety_auto_research.control_plane.routers import build_loops_router
from safety_auto_research.control_plane.routers import build_observability_router
from safety_auto_research.control_plane.routers import build_research_records_router
from safety_auto_research.control_plane.routers import build_workflow_runs_router
from safety_auto_research.control_plane.service import ControlPlaneService

_CP_DIR = os.path.dirname(
    os.path.abspath(
        __import__("safety_auto_research.control_plane.api", fromlist=["api"]).__file__
    )
)

ROUTER_BUILDERS = (
    build_workflow_runs_router,
    build_loops_router,
    build_evolution_router,
    build_observability_router,
    build_benchmarks_router,
    build_research_records_router,
    build_experiments_router,
    build_flywheel_router,
    build_llm_router,
    build_integrity_router,
)

# Frozen HTTP surface, captured from the pre-refactor monolith.
#
# Deliberate additions since that capture (this is the sanctioned way to grow the
# surface — see the module docstring):
#   * /integrity/gates + /workflow-runs/{run_id}/integrity-check — the opt-in
#     integrity-gate suite. Registered unconditionally and gated at request time
#     by the INTEGRITY_GATES env switch, precisely so that this frozen surface
#     stays deterministic instead of depending on the launching shell.
EXPECTED_ROUTES: dict[str, tuple[str, ...]] = {
    "/agent/protocol": ("GET",),
    "/agent/llm/chat": ("POST",),
    "/agent/llm/models": ("GET",),
    "/agent/llm/provider": ("GET",),
    "/agent/llm/test-label": ("POST",),
    "/benchmark-suites": ("GET",),
    "/benchmark-suites/{suite_id}": ("GET",),
    "/benchmark-suites/{suite_id}/baselines": ("GET",),
    "/benchmark-suites/{suite_id}/tasks": ("GET",),
    "/benchmark-tasks": ("GET",),
    "/benchmark-tasks/register": ("POST",),
    "/benchmark-tasks/task-types": ("GET",),
    "/benchmark-tasks/upload-dataset": ("POST",),
    "/benchmark-tasks/validate": ("POST",),
    "/benchmark-tasks/{task_id}": ("DELETE",),
    "/benchmark-tasks/{task_id}/launch": ("POST",),
    "/decisions/{decision_id}": ("GET",),
    "/events": ("GET",),
    "/experiences": ("GET",),
    "/integrity/gates": ("GET",),
    "/playbook": ("GET",),
    "/research-records": ("GET",),
    "/research-records/compare": ("GET",),
    "/research-records/leaderboard": ("GET",),
    "/research-records/{record_id}/reproduce": ("POST",),
    "/stages/{stage_run_id}": ("GET",),
    "/stages/{stage_run_id}/status": ("POST",),
    "/strategies": ("GET",),
    "/workflow-runs": ("GET", "POST"),
    "/workflow-runs/{run_id}": ("GET",),
    "/workflow-runs/{run_id}/agent-trace": ("GET",),
    "/workflow-runs/{run_id}/audit": ("GET",),
    "/workflow-runs/{run_id}/audits/{audit_id}/followup": ("POST",),
    "/workflow-runs/{run_id}/audits/{audit_id}/followups": ("GET",),
    "/workflow-runs/{run_id}/cancel": ("POST",),
    "/workflow-runs/{run_id}/capabilities/{capability_id}/run": ("POST",),
    "/workflow-runs/{run_id}/closed-loop": ("POST",),
    "/workflow-runs/{run_id}/debug": ("POST",),
    "/workflow-runs/{run_id}/decisions": ("GET", "POST"),
    "/workflow-runs/{run_id}/dispatch": ("POST",),
    "/workflow-runs/{run_id}/dual-loop": ("POST",),
    "/workflow-runs/{run_id}/evaluate": ("POST",),
    "/workflow-runs/{run_id}/evolution": ("GET", "POST"),
    "/workflow-runs/{run_id}/flywheel": ("GET", "POST"),
    "/workflow-runs/{run_id}/flywheel/schedule": ("DELETE", "GET", "POST"),
    "/workflow-runs/{run_id}/hypo-tree": ("GET",),
    "/workflow-runs/{run_id}/improvements": ("GET",),
    "/workflow-runs/{run_id}/integrity-check": ("POST",),
    "/workflow-runs/{run_id}/lessons": ("GET",),
    "/workflow-runs/{run_id}/mea": ("POST",),
    "/workflow-runs/{run_id}/mea/state": ("GET",),
    "/workflow-runs/{run_id}/program-evolution": ("GET", "POST"),
    "/workflow-runs/{run_id}/report-metric": ("POST",),
    "/workflow-runs/{run_id}/request-approval": ("POST",),
    "/workflow-runs/{run_id}/resolve-approval": ("POST",),
    "/workflow-runs/{run_id}/resolve-collaboration": ("POST",),
    "/workflow-runs/{run_id}/run-experiment": ("POST",),
    "/workflow-runs/{run_id}/stages": ("GET", "POST"),
    "/workflow-runs/{run_id}/start": ("POST",),
    "/workflow-runs/{run_id}/stream": ("GET",),
}


def _make_app():
    return create_app(ControlPlaneService(Repository()))


class RouteSurfaceTest(unittest.TestCase):
    """A1 must not change a single path or method of the HTTP surface."""

    def test_openapi_paths_match_frozen_surface(self) -> None:
        spec = _make_app().openapi()
        actual = {
            path: tuple(sorted(m.upper() for m in ops))
            for path, ops in spec["paths"].items()
        }
        self.assertEqual(set(EXPECTED_ROUTES), set(actual), "path set drifted")
        for path, methods in EXPECTED_ROUTES.items():
            self.assertEqual(methods, actual[path], f"methods drifted for {path}")

    def test_every_router_contributes_routes(self) -> None:
        deps = build_deps(ControlPlaneService(Repository()))
        for build in ROUTER_BUILDERS:
            with self.subTest(router=build.__name__):
                router = build(deps)
                self.assertGreater(len(router.routes), 0)

    def test_routers_do_not_collide(self) -> None:
        """No (path, method) pair may be registered by two different routers."""
        deps = build_deps(ControlPlaneService(Repository()))
        seen: dict[tuple[str, str], str] = {}
        for build in ROUTER_BUILDERS:
            for route in build(deps).routes:
                for method in sorted(getattr(route, "methods", []) or []):
                    key = (route.path, method)
                    self.assertNotIn(
                        key,
                        seen,
                        f"{key} registered by both {seen.get(key)} and {build.__name__}",
                    )
                    seen[key] = build.__name__
        self.assertEqual(
            sum(len(m) for m in EXPECTED_ROUTES.values()),
            len(seen),
            "router route count differs from the frozen surface",
        )


class ThinAssemblerTest(unittest.TestCase):
    """api.py must stay an assembler — route decorators belong in routers/."""

    def _api_source(self) -> str:
        with open(os.path.join(_CP_DIR, "api.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_api_declares_no_http_routes(self) -> None:
        src = self._api_source()
        offenders = re.findall(r"@app\.(get|post|put|patch|delete)\b", src)
        self.assertEqual([], offenders, "route declarations leaked back into api.py")

    def test_api_module_stays_small(self) -> None:
        src = self._api_source()
        self.assertLess(
            len(src.splitlines()),
            160,
            "api.py grew back towards a monolith — put new endpoints in routers/",
        )

    def test_routers_never_import_api(self) -> None:
        """Routers depend on deps.py, never back on api.py (no import cycle)."""
        rdir = os.path.join(_CP_DIR, "routers")
        for name in sorted(os.listdir(rdir)):
            if not name.endswith(".py"):
                continue
            with self.subTest(module=name), open(os.path.join(rdir, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("from ..api import", src)
            self.assertNotIn("from safety_auto_research.control_plane.api", src)


class DepsContainerTest(unittest.TestCase):
    """A3: the DI container is the single place shared runtime state is wired."""

    def test_app_exposes_deps_container(self) -> None:
        app = _make_app()
        deps = app.state.deps
        self.assertIsInstance(deps, ControlPlaneDeps)
        for attr in (
            "svc",
            "state_store",
            "orchestrator",
            "run_cancel_events",
            "shutdown_event",
            "progress_bus",
        ):
            self.assertIsNotNone(getattr(deps, attr), attr)

    def test_injected_service_is_honoured(self) -> None:
        svc = ControlPlaneService(Repository())
        app = create_app(svc)
        self.assertIs(app.state.deps.svc, svc)
        self.assertIs(app.state.deps.orchestrator.svc, svc)

    def test_helper_surface_is_available(self) -> None:
        deps = build_deps(ControlPlaneService(Repository()))
        for helper in ("make_agent_orchestrator", "build_run_ctx", "spawn_full_experiment"):
            self.assertTrue(callable(getattr(deps, helper)), helper)


if __name__ == "__main__":
    unittest.main()
