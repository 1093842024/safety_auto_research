"""Dependency-injection container for the control plane (backlog item A3).

Centralises construction of the shared runtime dependencies — ``ControlPlaneService``,
``ResearchStateStore`` and ``ClosedLoopOrchestrator`` — together with the helper
closures that HTTP route handlers need (run-context reconstruction, agent-orchestrator
factory, background-thread spawning, exception translation, summary-status mapping).

Routers receive a :class:`ControlPlaneDeps` instance and bind the names they need as
local aliases, so the route-handler logic is byte-for-byte identical to the previous
single-file ``api.py``. This keeps the refactor behavior-preserving while giving the
control plane a real DI boundary (and making ``create_app`` a thin assembler).
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import threading
import time as _time
from typing import Any, Callable

from fastapi import HTTPException
from fastapi import status

from ..benchmark_tasks import get_task
from .progress_bus import progress_bus as _progress_bus
from .schemas import InnerLoopConfig
from .service import ConflictError
from .service import ControlPlaneService
from .service import NotFoundError
from .store_tree import ResearchStateStore

# --------------------------------------------------------------------------- #
# Process-wide background-worker bookkeeping.                                  #
# These stay module-level (as they were in api.py) so that shutdown semantics   #
# are unchanged: one registry per process, joined once via atexit / FastAPI     #
# shutdown hook.                                                                #
# --------------------------------------------------------------------------- #
_shutdown_event = threading.Event()
_bg_threads: set[threading.Thread] = set()
_bg_lock = threading.Lock()
# Per-run cancellation events — set by the cancel-endpoint, checked by dual-loop workers.
_run_cancel_events: dict[str, threading.Event] = {}


def acquire_run_slot(
    events: dict[str, threading.Event], run_id: str
) -> threading.Event | None:
    """R8 fix: single-flight guard for loop drivers, plus R1's cancel-event registry.

    Two POSTs for the same ``run_id`` used to spawn two independent driver threads on
    the *same* run: both created StageRuns, both wrote metrics, both raced on the
    terminal ``set_run_status`` — producing interleaved iterations and a corrupted
    research record with no error anywhere. There was no mutual exclusion at all.

    Returns a freshly registered cancel event, or ``None`` when a driver is already
    live for this run (caller should answer 409). The event doubles as the
    "a driver owns this run" token: it is registered here, before the thread is
    spawned, and released by :func:`release_run_slot` in the driver's ``finally``.
    """

    with _bg_lock:
        if run_id in events:
            return None
        ev = threading.Event()
        events[run_id] = ev
        return ev


def release_run_slot(events: dict[str, threading.Event], run_id: str) -> None:
    """Release the slot acquired by :func:`acquire_run_slot` (idempotent).

    Also closes the run's SSE progress stream (R15): every background driver ends
    by releasing its slot, so this is the single choke point where "this run will
    emit no further progress events" becomes true. Without the sentinel the SSE
    generator stayed parked on ``await q.get()`` forever and the browser kept a
    dead EventSource open.
    """

    with _bg_lock:
        events.pop(run_id, None)
    try:
        _progress_bus.close(run_id)
    except Exception:  # pragma: no cover - never let cleanup break a driver
        logging.exception("failed to close progress stream for run=%s", run_id)


def _register_bg_thread(t: threading.Thread) -> None:
    """Track a background worker so shutdown can join it gracefully."""
    with _bg_lock:
        _bg_threads.add(t)


def _deregister_bg_thread(t: threading.Thread) -> None:
    with _bg_lock:
        _bg_threads.discard(t)


def _spawn_bg_thread(target: Callable[[], None], name: str = "") -> threading.Thread:
    """Spawn a non-daemon background thread that will be joined on shutdown.

    Non-daemon threads are NOT force-killed on process exit — they finish their
    current work unit (e.g. a ``_persist`` call) before the process stops. This
    prevents store-JSON corruption from a mid-write SIGTERM.
    """
    t = threading.Thread(target=target, daemon=False, name=name or f"bg-{len(_bg_threads)}")
    _register_bg_thread(t)
    t.start()
    return t


def _graceful_shutdown(timeout: float = 8.0) -> None:
    """Signal background workers and wait for them to finish (at most *timeout* seconds)."""
    _shutdown_event.set()
    threads: list[threading.Thread] = []
    with _bg_lock:
        threads = list(_bg_threads)
    deadline = _time.monotonic() + timeout
    for t in threads:
        remaining = deadline - _time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)
    with _bg_lock:
        _bg_threads.clear()


atexit.register(_graceful_shutdown)


def _map_summary_status(status: str) -> str:
    """Map dual/evolution-loop summary statuses onto valid WorkflowStatus names (M6).

    The orchestrator reports a few loop-local statuses that are not WorkflowStatus
    enum members; writing them verbatim would raise and (worse) skip the run's
    record capture.
    """

    _MAP = {
        "collaboration_aborted": "cancelled",
        "stopped_after_eval": "exited_converged",
    }
    return _MAP.get(status, status)


def translate_exc(exc: Exception) -> HTTPException:
    """Map service-layer errors onto HTTP errors (was ``_translate`` inside create_app)."""
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise exc


def resolve_eval_params(task, inner, type_cfg, pkg_root):
    """Single source of truth for the kaggle_eval launch parameters shared by
    ``launch_benchmark_task`` (fresh launch) and ``_build_run_ctx`` (replay from a
    run's objective_snapshot). A2: this ~40-line block used to be duplicated in both
    places and had to be kept in sync by hand — every preset/threshold tweak risked a
    silent divergence between a fresh launch and a debug replay.
    """
    task_id = task.task_id
    is_custom_exec = (
        task_id.startswith("custom.")
        and task.harness == "kaggle_eval"
        and task.supported_by_platform
    )
    if is_custom_exec:
        preset = "custom"
    elif inner.preset:
        preset = inner.preset
    elif task_id == "platform.spaceship":
        preset = "spaceship"
    elif task_id == "platform.titanic":
        preset = "titanic"
    else:
        preset = "titanic"
    # kaggle_eval expects fe as "basic"/"rich" (string), not a bool.
    fe_value = (inner.fe or "basic").lower()
    if fe_value not in ("basic", "rich"):
        fe_value = "basic"
    if is_custom_exec:
        data_dir = inner.data_dir or str(type_cfg.get("data_dir") or "")
    else:
        data_dir = inner.data_dir or os.path.join(pkg_root, "data", "kaggle", preset)
    # Threshold gate: explicit inner.threshold wins, else the task's own registered
    # threshold (custom tasks), else the competition default.
    if inner.threshold is not None:
        threshold = float(inner.threshold)
    elif is_custom_exec and type_cfg.get("threshold") not in (None, ""):
        threshold = float(type_cfg["threshold"])
    elif preset == "spaceship":
        threshold = 0.80
    else:
        threshold = 0.82
    return preset, fe_value, data_dir, threshold, is_custom_exec


class ControlPlaneDeps:
    """Bundle of shared runtime dependencies + helper closures for the control plane.

    Constructed once by ``create_app`` (or by tests that inject a pre-built service).
    Routers receive the instance and bind the names they need as local aliases so
    handler code stays verbatim.
    """

    def __init__(
        self,
        svc: ControlPlaneService,
        state_store: ResearchStateStore,
        orchestrator,
        *,
        run_cancel_events: dict[str, threading.Event] | None = None,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self.svc = svc
        self.state_store = state_store
        self.orchestrator = orchestrator
        # Default to the process-wide registries so semantics match the old module-level
        # globals in api.py; tests may inject isolated ones.
        self.run_cancel_events: dict[str, threading.Event] = (
            _run_cancel_events if run_cancel_events is None else run_cancel_events
        )
        self.shutdown_event: threading.Event = (
            _shutdown_event if shutdown_event is None else shutdown_event
        )
        self.progress_bus = _progress_bus

    # ----------------------------------------------------------------- helpers
    def make_agent_orchestrator(self, agent_cli: str):
        """Build a per-run orchestrator wired to the chosen agent CLI.

        * ``"codex"``      -> ``RemoteAgentHarness(CodexTransport(...))``
        * ``"claude_code"``-> ``RemoteAgentHarness(ClaudeCodeTransport(...))``
        * ``"auto"`` / None-> the global ``orchestrator`` only if it already has a real
          remote agent (i.e. ``AGENT_COMMAND`` was set); otherwise raise so the caller can
          fail the run loudly instead of silently running scripted.
        """
        from ..execution_plane.orchestrator import ClosedLoopOrchestrator

        svc = self.svc
        state_store = self.state_store
        orchestrator = self.orchestrator

        if agent_cli == "codex":
            from ..execution_plane.agent.harness import RemoteAgentHarness
            from ..execution_plane.agent.transport import CodexTransport

            transport = CodexTransport(capability_runner=orchestrator.run_capability)
            return ClosedLoopOrchestrator(
                svc, state_store=state_store, mode="agent", harness=RemoteAgentHarness(transport=transport)
            )
        if agent_cli == "claude_code":
            from ..execution_plane.agent.harness import RemoteAgentHarness
            from ..execution_plane.agent.transport import ClaudeCodeTransport

            transport = ClaudeCodeTransport(capability_runner=orchestrator.run_capability)
            return ClosedLoopOrchestrator(
                svc, state_store=state_store, mode="agent", harness=RemoteAgentHarness(transport=transport)
            )
        # auto / None: reuse a globally-wired real agent if present.
        if orchestrator.has_real_agent():
            return orchestrator
        raise RuntimeError("no real remote agent configured (set AGENT_COMMAND)")

    def build_run_ctx(self, run) -> dict[str, Any]:
        """Reconstruct the full execution context (orchestrator, params, config)
        from a run's ``objective_snapshot`` so debug / run-experiment can replay.
        """
        orchestrator = self.orchestrator
        _make_agent_orchestrator = self.make_agent_orchestrator

        obj = run.objective_snapshot or {}
        cfg0 = obj.get("config", {}) or {}
        inner_dump = cfg0.get("inner_loop") or {}
        inner = InnerLoopConfig(**inner_dump) if inner_dump else InnerLoopConfig()
        task_id = obj.get("benchmark_task_id", "")
        task = get_task(task_id)
        if task is None:
            raise ValueError(f"cannot rebuild context: task {task_id} not found")
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        type_cfg = dict(task.type_config or {})
        # A2: same single source of truth as a fresh launch — a debug replay now
        # resolves preset / fe / data_dir / threshold identically to the original launch.
        preset, fe_value, data_dir, threshold, is_custom_exec = resolve_eval_params(
            task, inner, type_cfg, pkg_root
        )
        agent_mode = obj.get("execution_mode") == "agent"
        agent_cli = cfg0.get("agent_cli") or obj.get("agent_cli")
        inner_params = {
            "preset": preset,
            "model": inner.model,
            "fe": fe_value,
            "data_dir": data_dir,
            "cv_folds": inner.cv_folds,
            "threshold": threshold,
        }
        if inner.drop_cols:
            inner_params["drop_cols"] = list(inner.drop_cols)
        if is_custom_exec:
            if type_cfg.get("target_col"):
                inner_params["target"] = str(type_cfg["target_col"])
            extra_drops = list(type_cfg.get("drop_cols") or [])
            if type_cfg.get("id_col"):
                extra_drops.append(str(type_cfg["id_col"]))
            merged = list(inner_params.get("drop_cols", [])) + extra_drops
            if merged:
                inner_params["drop_cols"] = sorted(set(merged))
            if type_cfg.get("model") and inner.model == "gbm":
                inner_params["model"] = str(type_cfg["model"])
            if type_cfg.get("cv_folds") and inner.cv_folds == 5:
                inner_params["cv_folds"] = int(type_cfg["cv_folds"])
        inner_agent_config = (
            {**inner.model_dump(), "task_spec": obj.get("task_spec", {}), "goal": obj.get("goal", "")}
            if agent_mode
            else None
        )
        per_run_orch = None
        if agent_mode and agent_cli in ("codex", "claude_code"):
            cli_bin = "codex" if agent_cli == "codex" else os.environ.get("CLAUDE_CMD", "claude")
            if shutil.which(cli_bin):
                per_run_orch = _make_agent_orchestrator(agent_cli)
        elif agent_mode and orchestrator.has_real_agent():
            per_run_orch = orchestrator
        active_orchestrator = per_run_orch or orchestrator
        collab_mode = getattr(inner, "collaboration_mode", None) or "autonomous"
        return {
            "active_orchestrator": active_orchestrator,
            "inner_params": inner_params,
            "inner_agent_config": inner_agent_config,
            "agent_mode": agent_mode,
            "audit_threshold": cfg0.get("audit_threshold", 0.8),
            "max_outer_iters": cfg0.get("max_outer_iters", 3),
            "collaboration_mode": collab_mode,
            "execution_mode": obj.get("execution_mode", "platform"),
        }

    def spawn_full_experiment(self, run, ctx: dict[str, Any]) -> None:
        """Start a full autonomous experiment (dual loop) in a background daemon thread."""
        svc = self.svc
        _run_cancel_events_local = self.run_cancel_events
        _shutdown_event_local = self.shutdown_event
        _progress_bus_local = self.progress_bus

        active_orchestrator = ctx["active_orchestrator"]

        # R1 + R8 fix: pre-register the cancel event synchronously (so ``POST .../cancel``
        # is honored — ``.get(run_id)`` inside the thread always returned None because
        # nothing ever pre-registered the key) and treat it as a single-flight token so
        # two launches for the same run cannot drive it concurrently.
        cancel_ev = acquire_run_slot(_run_cancel_events_local, run.run_id)
        if cancel_ev is None:
            raise ConflictError(f"run {run.run_id} 已有正在执行的循环；请先取消后再启动")

        try:
            svc.start_workflow_run(run.run_id)
        except (ConflictError, ValueError):
            pass  # already running / terminal

        def _drive() -> None:
            if _shutdown_event_local.is_set():
                release_run_slot(_run_cancel_events_local, run.run_id)
                return
            try:
                summary = active_orchestrator.run_dual_loop(
                    run.run_id,
                    inner_capability="kaggle_eval",
                    inner_params=ctx["inner_params"],
                    audit_params={"threshold": ctx["audit_threshold"]},
                    max_outer_iters=ctx["max_outer_iters"],
                    agent_inner=ctx["agent_mode"],
                    inner_agent_config=ctx.get("inner_agent_config"),
                    collaboration_mode=ctx.get("collaboration_mode", "autonomous"),
                    cancel_event=cancel_ev,
                    progress_callback=_progress_bus_local.emit,
                )
                svc.set_run_status(run.run_id, _map_summary_status(summary.get("status", "exited_budget")))
                try:
                    svc.capture_run_record(run.run_id)
                except Exception:
                    logging.exception("capture_run_record failed for run=%s", run.run_id)
            except Exception:
                logging.exception("experiment failed for run=%s", run.run_id)
                try:
                    svc.set_run_status(run.run_id, "failed")
                except Exception:
                    logging.exception("set_run_status('failed') failed for run=%s", run.run_id)
            finally:
                # I3 fix: no pending meta-loop proposal survives an abnormal exit.
                # R24 fix: the pop() used to sit *after* expire_pending_strategies inside
                # the same try, so a raising expire leaked the cancel event forever.
                try:
                    active_orchestrator.expire_pending_strategies(run.run_id)
                except Exception:
                    logging.exception("expire_pending_strategies failed for run=%s", run.run_id)
                finally:
                    release_run_slot(_run_cancel_events_local, run.run_id)  # 缺陷10 + R8

        _spawn_bg_thread(_drive, name=f"experiment-{run.run_id}")


def build_deps(service: ControlPlaneService | None = None) -> ControlPlaneDeps:
    """Construct the dependency container (mirrors the previous ``create_app`` wiring).

    When *service* is ``None`` the app builds its own on-disk-persistent service +
    state store (real launch), so research records survive restarts: the Repository is
    backed by a JSON file and the dual-loop ResearchStateStore by a SQLite db. Paths are
    overridable via env vars and default under ``data/``. When a service is injected
    (tests) an in-memory state store is used.
    """
    from ..execution_plane.orchestrator import ClosedLoopOrchestrator

    # R30 fix: the real-launch path used to build a throwaway service, state store
    # and orchestrator first and then immediately rebuild all three with the on-disk
    # paths — the discarded ResearchStateStore still opened a default SQLite handle,
    # and an AGENT_COMMAND launch constructed three orchestrators to keep one.
    # Resolve the configuration first, then construct each collaborator exactly once.
    if service is None:
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(pkg_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        repo_path = os.environ.get(
            "CONTROL_PLANE_STORE", os.path.join(data_dir, "control_plane_store.json")
        )
        state_path = os.environ.get(
            "RESEARCH_STATE_DB", os.path.join(data_dir, "research_state.db")
        )
        svc = ControlPlaneService(store_path=repo_path)
        # Shared cumulative-state store (dual-loop): the orchestrator threads it into
        # the SDK so hypothesis-tree / experience-bank / strategy-archive persist.
        state_store = ResearchStateStore(db_path=state_path)
    else:
        svc = service
        state_store = ResearchStateStore()  # in-memory (tests)

    # Remote-agent wiring (Tasks 1 & 4): if AGENT_COMMAND is set, plug a real external
    # agent so agent-mode inner loops can actually execute. Without it, agent mode fails
    # loudly instead of silently falling back to the scripted executor.
    agent_command = os.environ.get("AGENT_COMMAND")
    if agent_command:
        from ..execution_plane.agent.harness import RemoteAgentHarness

        orchestrator = ClosedLoopOrchestrator(
            svc,
            state_store=state_store,
            mode="agent",
            harness=RemoteAgentHarness(agent_command=agent_command),
        )
    else:
        orchestrator = ClosedLoopOrchestrator(svc, state_store=state_store)

    return ControlPlaneDeps(svc, state_store, orchestrator)
