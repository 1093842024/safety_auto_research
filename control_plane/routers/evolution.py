"""Config-level and program-level evolutionary-search routes.

Extracted verbatim from ``control_plane/api.py`` (backlog item A1: split the
1975-line monolith into per-domain routers). Handler bodies are unchanged; the
shared dependencies they used to close over are now bound from
:class:`~..deps.ControlPlaneDeps` as local aliases.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status

from ...platform_contracts.enums import RunType
from ...platform_contracts.enums import WorkflowStatus
from ...platform_contracts.events import utc_now
from ...platform_contracts.objects import WorkflowRun
from ..schemas import EvolutionRequest
from ..schemas import ProgramEvolutionRequest
from ..service import ConflictError
from ..service import NotFoundError
from ..progress_bus import progress_bus as _progress_bus
from ..deps import ControlPlaneDeps
from ..deps import _map_summary_status
from ..deps import _spawn_bg_thread
from ..deps import acquire_run_slot
from ..deps import release_run_slot
from ..deps import translate_exc as _translate


def build_evolution_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the evolution router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    state_store = deps.state_store
    orchestrator = deps.orchestrator
    _run_cancel_events = deps.run_cancel_events
    _shutdown_event = deps.shutdown_event

    @router.post(
        "/workflow-runs/{run_id}/evolution",
        summary="Run parallel evolutionary search over the inner-loop config space",
    )
    def run_evolution_endpoint(run_id: str, req: EvolutionRequest) -> dict:
        # R1 fix: pre-register the cancel event synchronously so ``POST .../cancel``
        # reaches the running loop instead of being dropped on the floor.
        cancel_ev = acquire_run_slot(_run_cancel_events, run_id)
        if cancel_ev is None:
            # R8 fix: single-flight. A second POST for the same run used to spawn a
            # second driver thread racing the first one on the same StageRuns/metrics.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run {run_id} 已有正在执行的循环；请先取消（POST /workflow-runs/{run_id}/cancel）",
            )

        # 缺陷2 fix: background the loop; wire cancel_event; finalize status + capture record.
        def _drive() -> None:
            if _shutdown_event.is_set():
                release_run_slot(_run_cancel_events, run_id)
                return
            try:
                try:
                    svc.start_workflow_run(run_id)
                except (ConflictError, ValueError):
                    pass  # already running / terminal — proceed
                inner_params = dict(req.inner_params) or {
                    "preset": "titanic",
                    "model": "gbm",
                    "data_dir": os.path.join(
                        os.path.dirname(__file__), "..", "data", "kaggle", "titanic"
                    ),
                    "cv_folds": 5,
                    "threshold": 0.82,
                }
                summary = orchestrator.run_evolutionary_loop(
                    run_id,
                    inner_capability=req.inner_capability,
                    inner_params=inner_params,
                    audit_params=req.audit_params or {"threshold": 0.8},
                    population_size=req.population_size,
                    generations=req.generations,
                    max_workers=req.max_workers,
                    novelty_threshold=req.novelty_threshold,
                    budget=req.budget or None,
                    seed=req.seed,
                    cancel_event=cancel_ev,
                    progress_callback=_progress_bus.emit,
                )
                svc.set_run_status(run_id, _map_summary_status(summary.get("status", "exited_budget")))
                try:
                    svc.capture_run_record(run_id)
                except Exception:
                    logging.exception("capture_run_record failed for run=%s", run_id)
            except Exception:
                logging.exception("evolution loop failed for run=%s", run_id)
                try:
                    svc.set_run_status(run_id, "failed")
                except Exception:
                    logging.exception("set_run_status('failed') failed for run=%s", run_id)
            finally:
                # R3 fix: unconditional cleanup (used to be nested in ``except``).
                try:
                    orchestrator.expire_pending_strategies(run_id)
                except Exception:
                    logging.exception("expire_pending_strategies failed for run=%s", run_id)
                finally:
                    release_run_slot(_run_cancel_events, run_id)  # 缺陷10: always release the cancel event once the run ends

        _spawn_bg_thread(_drive, name=f"evolution-{run_id}")
        return {"run_id": run_id, "status": "running", "accepted": True}

    # ----- Evolution observability: population / candidates -----
    @router.get(
        "/workflow-runs/{run_id}/evolution",
        summary="Evolution candidates (fitness / novelty / lineage) for a run",
    )
    def list_evolution(run_id: str) -> list[dict]:
        from dataclasses import asdict

        try:
            svc.get_workflow_run(run_id)
            return [asdict(c) for c in state_store.evolution.list(run_id)]
        except Exception as exc:
            raise _translate(exc)

    # ----- Program-level evolutionary search (OpenRSI / Phase B–C): island model ----
    # 缺陷7 fix: previously `run_program_evolutionary_loop` was an ORPHAN path — only
    # reachable via orchestrator-internal calls + test_openmle_phase_*. It is now wired
    # to REST so the frontend EvolutionPanel island view can actually drive it.
    @router.post(
        "/workflow-runs/{run_id}/program-evolution",
        summary="Run program-level evolutionary search (OpenRSI island model) over the code space",
    )
    def run_program_evolution_endpoint(run_id: str, req: ProgramEvolutionRequest) -> dict:
        # R1 fix: pre-register the cancel event synchronously (see the config-level
        # evolution endpoint above).
        cancel_ev = acquire_run_slot(_run_cancel_events, run_id)
        if cancel_ev is None:
            # R8 fix: single-flight. A second POST for the same run used to spawn a
            # second driver thread racing the first one on the same StageRuns/metrics.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run {run_id} 已有正在执行的循环；请先取消（POST /workflow-runs/{run_id}/cancel）",
            )

        # 缺陷2 fix (applied to this endpoint too): background the loop; wire the
        # cancel_event; finalize status + capture record; always release the cancel event.
        def _drive() -> None:
            if _shutdown_event.is_set():
                release_run_slot(_run_cancel_events, run_id)
                return
            try:
                # Materialize the run if missing (mirrors the MEA endpoint) so the
                # loop's objective_snapshot drives goal/op resolution.
                try:
                    svc.get_workflow_run(run_id)
                except NotFoundError:
                    svc._repo.put_workflow_run(
                        WorkflowRun(
                            run_id=run_id,
                            program_id="program-evolution",
                            run_type=RunType.STANDARD_RESEARCH,
                            entry_stage="00_agent_orchestration",
                            target_id=run_id,
                            objective_snapshot={"goal": (req.task_config or {}).get("name", run_id), "op": "ge"},
                            status=WorkflowStatus.REQUESTED,
                            started_at=utc_now(),
                        )
                    )
                try:
                    svc.start_workflow_run(run_id)
                except (ConflictError, ValueError):
                    pass  # already running / terminal — proceed

                # ---- build the verifiable task config ----
                from ...openmle_integration.adapter import OpenMLETaskConfig

                tc = dict(req.task_config or {})
                preset = tc.get("preset")
                if not tc.get("data_dir") and preset in (None, "titanic"):
                    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    tc["data_dir"] = os.path.join(pkg_root, "data", "kaggle", "titanic")
                if "target" not in tc:
                    tc["target"] = "Survived"
                if "id_col" not in tc:
                    tc["id_col"] = "PassengerId"
                task_config = OpenMLETaskConfig(
                    **{k: v for k, v in tc.items() if k in OpenMLETaskConfig.__dataclass_fields__}
                )

                # ---- build the operator backend ----
                if req.backend_type == "llm":
                    from ...openmle_integration.operators import make_api_backend

                    backend = make_api_backend(
                        **{k: v for k, v in (req.backend_config or {}).items()
                           if k in ("api_key", "base_url", "model", "tme_open")}
                    )
                else:
                    from ...openmle_integration.operators import TemplateOperatorBackend

                    backend = TemplateOperatorBackend()

                summary = orchestrator.run_program_evolutionary_loop(
                    run_id,
                    task_config,
                    backend,
                    islands=req.islands,
                    pop_per_island=req.pop_per_island,
                    generations=req.generations,
                    max_workers=req.max_workers,
                    novelty_threshold=req.novelty_threshold,
                    audit=req.audit,
                    audit_params=req.audit_params or None,
                    budget=req.budget or None,
                    seed=req.seed,
                    cancel_event=cancel_ev,
                    progress_callback=_progress_bus.emit,
                )
                svc.set_run_status(run_id, _map_summary_status(summary.get("status", "exited_budget")))
                try:
                    svc.capture_run_record(run_id)
                except Exception:
                    logging.exception("capture_run_record failed for run=%s", run_id)
            except Exception:
                logging.exception("program evolution loop failed for run=%s", run_id)
                try:
                    svc.set_run_status(run_id, "failed")
                except Exception:
                    logging.exception("set_run_status('failed') failed for run=%s", run_id)
            finally:
                # R3 fix: unconditional cleanup (used to be nested in ``except``).
                try:
                    orchestrator.expire_pending_strategies(run_id)
                except Exception:
                    logging.exception("expire_pending_strategies failed for run=%s", run_id)
                finally:
                    release_run_slot(_run_cancel_events, run_id)  # 缺陷10: always release once the run ends

        _spawn_bg_thread(_drive, name=f"program-evolution-{run_id}")
        return {"run_id": run_id, "status": "running", "accepted": True}

    # ----- Program evolution observability: code-node candidates / island lineage -----
    @router.get(
        "/workflow-runs/{run_id}/program-evolution",
        summary="Program-level evolution candidates (code nodes + island lineage) for a run",
    )
    def list_program_evolution(run_id: str) -> list[dict]:
        from dataclasses import asdict

        try:
            svc.get_workflow_run(run_id)
            # node_kind="program" only — the two evolution grains coexist in one archive
            # (config + program); the island view needs just the code-operator nodes.
            return [asdict(c) for c in state_store.evolution.list_by_kind(run_id, "program")]
        except Exception as exc:
            raise _translate(exc)

    # ----- MEA loop (LongHorizon-Harness Phase P1): Manage-Execute-Audit -----

    return router
