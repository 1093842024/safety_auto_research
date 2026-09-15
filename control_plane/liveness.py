"""Run-liveness detection: 运行中 run 到底是「真在跑」还是「已死但状态未感知」。

Dual-loop drivers run as **in-process threads** in the control plane, and every
driver registers itself in the shared ``run_cancel_events`` slot map for the
duration of its life (``acquire_run_slot`` / ``release_run_slot``). Two zombie
modes follow:

1. **Orphaned after restart** — the run's ``running`` status was persisted to
   the JSON store, but the process (and its driver threads) died. On the next
   boot no driver exists for it: the run is dead with certainty.
   :func:`reconcile_orphaned_runs` runs once at app startup and marks such runs
   ``failed`` with a human-readable ``status_detail``.

2. **Stale while "running"** — the driver thread is registered but has emitted
   no event for a long time. It *may* still be legitimately busy (agent-mode
   inner loops are slow), so this is surfaced as a warning verdict rather than
   an auto-fail: :func:`liveness_snapshot` reports per-run
   ``last_activity_at`` / ``stale_minutes`` / ``verdict`` for the UI.

Verdicts (for non-terminal runs):
  * ``active``          — driver alive and events are recent (真在运行)
  * ``stale``           — driver alive but no event for > STALE_AFTER_MINUTES
  * ``zombie``          — no driver slot at all (异常终止，状态未更新)
  * ``waiting_approval` — HITL gate; liveness is not applicable
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any

from ..platform_contracts.enums import WorkflowStatus
from ..platform_contracts.events import utc_now

#: A "running" run whose driver emitted no event for longer than this is
#: reported as ``stale`` (疑似卡死). Generous by design: agent-mode inner loops
#: can legitimately be quiet for many minutes.
STALE_AFTER_MINUTES = 30.0

_TERMINAL = frozenset(
    {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.EXITED_BUDGET,
        WorkflowStatus.EXITED_CONVERGED,
        WorkflowStatus.CANCELLED,
    }
)

_ORPHAN_DETAIL = (
    "服务重启检测：该 run 在上次进程退出时仍处于运行中，"
    "执行线程已不存在（驱动为进程内线程，无法跨进程存活），"
    "启动对账时自动标记为异常终止。"
)


def _minutes_since(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 60.0)


def reconcile_orphaned_runs(service: Any) -> list[str]:
    """Mark persisted ``running`` runs as failed at startup (进程内驱动已随进程消亡).

    Returns the list of reconciled run_ids. Best-effort: a broken store must
    never prevent the app from booting.
    """
    reconciled: list[str] = []
    try:
        runs = service.list_workflow_runs()
    except Exception:
        return reconciled
    for run in runs:
        if run.status != WorkflowStatus.RUNNING:
            continue
        try:
            service.set_run_status(
                run.run_id, WorkflowStatus.FAILED.value, detail=_ORPHAN_DETAIL
            )
            reconciled.append(run.run_id)
        except Exception:
            # A single bad run must not block the rest of the reconciliation.
            continue
    return reconciled


def liveness_snapshot(service: Any, driver_slots: dict[str, Any]) -> dict[str, dict]:
    """Per-run liveness info for all non-terminal runs.

    Returns ``{run_id: {status, driver_alive, last_activity_at, stale_minutes,
    verdict, reason}}``. ``driver_slots`` is the shared run-cancel-event map:
    an entry exists exactly while an in-process driver thread is alive.
    """
    now = utc_now()
    out: dict[str, dict] = {}
    try:
        runs = service.list_workflow_runs()
        all_events = service.list_events()
    except Exception:
        return out

    # Latest event timestamp per run (events are the driver's activity trace).
    last_activity: dict[str, datetime] = {}
    for ev in all_events:
        rid = ev.get("run_id")
        ts = ev.get("occurred_at")
        if not rid or not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        prev = last_activity.get(rid)
        if prev is None or dt > prev:
            last_activity[rid] = dt

    for run in runs:
        if run.status in _TERMINAL:
            continue
        rid = run.run_id
        driver_alive = rid in driver_slots
        activity = last_activity.get(rid) or run.started_at
        stale_minutes = _minutes_since(activity, now)
        entry: dict[str, Any] = {
            "status": run.status.value
            if isinstance(run.status, WorkflowStatus)
            else str(run.status),
            "driver_alive": driver_alive,
            "last_activity_at": activity.isoformat() if activity else None,
            "stale_minutes": (
                round(stale_minutes, 1) if stale_minutes is not None else None
            ),
        }
        if run.status == WorkflowStatus.RUNNING:
            if not driver_alive:
                entry["verdict"] = "zombie"
                entry["reason"] = "无活跃执行线程（进程重启或驱动异常退出后遗留）"
            elif stale_minutes is not None and stale_minutes > STALE_AFTER_MINUTES:
                entry["verdict"] = "stale"
                entry["reason"] = (
                    f"执行线程仍在，但已 {stale_minutes:.0f} 分钟无任何事件"
                    "（疑似卡死，也可能是长时间 agent 内循环）"
                )
            else:
                entry["verdict"] = "active"
                entry["reason"] = "执行线程存活且事件在更新（真在运行）"
        elif run.status == WorkflowStatus.WAITING_APPROVAL:
            entry["verdict"] = "waiting_approval"
            entry["reason"] = "等待人工审批（HITL 门）"
        else:  # requested / other non-terminal
            entry["verdict"] = "idle"
            entry["reason"] = "已请求尚未开始执行"
        out[rid] = entry
    return out
