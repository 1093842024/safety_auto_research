from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..platform_contracts.objects import ALL_CONTRACT_MODELS

# name -> model, used to reconstruct pydantic objects on load.
_MODEL_BY_NAME = {m.__name__: m for m in ALL_CONTRACT_MODELS}

# Collections that hold pydantic objects and must be reconstructed on load.
#   coll_name -> (model_name, key_field)
_PERSIST_COLLECTIONS: dict[str, tuple[str, str]] = {
    "workflow_runs": ("WorkflowRun", "run_id"),
    "stage_runs": ("StageRun", "stage_run_id"),
    "decisions": ("DecisionRecord", "decision_id"),
    "artifacts": ("Artifact", "artifact_id"),
    "lessons": ("LessonCard", "lesson_id"),
}


class Repository:
    """In-memory store for control-plane objects and the platform event log.

    This is intentionally a single-process skeleton. When ``store_path`` is provided
    the whole store is persisted to a JSON file and reloaded on the next startup, so
    research records (workflow runs, stages, decisions, events, …) survive a process
    restart. ``store_path=None`` keeps the original pure in-memory behaviour (used by
    tests).
    """

    def __init__(self, store_path: str | Path | None = None) -> None:
        self.workflow_runs: dict[str, Any] = {}
        self.stage_runs: dict[str, Any] = {}
        self.decisions: dict[str, Any] = {}
        self.artifacts: dict[str, Any] = {}
        self.lessons: dict[str, Any] = {}
        self.metrics: dict[str, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        # Autonomous research records (leaderboard): record_id -> dict.
        self.research_records: dict[str, dict[str, Any]] = {}
        # Open approvals: run_id -> approval_id (persisted so restart preserves pending approvals).
        self.open_approvals: dict[str, str] = {}
        self._store_path: Path | None = Path(store_path) if store_path else None
        self._lock = threading.Lock()
        if self._store_path is not None and self._store_path.exists():
            self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        assert self._store_path is not None
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Back up the corrupt file before starting empty so data can be recovered
            # manually. The original is moved, not deleted.
            bak = self._store_path.with_name(self._store_path.name + ".corrupt." + str(int(os.path.getmtime(str(self._store_path)))))
            try:
                shutil.move(str(self._store_path), str(bak))
                logging.error(
                    "Corrupt store at %s backed up to %s: %s. Starting empty.",
                    self._store_path, bak, exc,
                )
            except OSError as bak_exc:
                logging.error(
                    "Corrupt store at %s (backup failed: %s): %s. Starting empty.",
                    self._store_path, bak_exc, exc,
                )
            return
        for coll_name, (model_name, key) in _PERSIST_COLLECTIONS.items():
            coll = getattr(self, coll_name)
            success_count = 0
            total_count = 0
            for item in raw.get(coll_name, []) or []:
                total_count += 1
                try:
                    obj = _MODEL_BY_NAME[model_name](**item["data"])
                    coll[getattr(obj, key)] = obj
                    success_count += 1
                except Exception as exc:
                    logging.warning(
                        "Skipping corrupt record in collection '%s': %s",
                        coll_name, exc,
                    )
            if total_count > 0 and success_count < total_count:
                logging.warning(
                    "Loaded %d/%d records from collection '%s' (%d corrupt records skipped)",
                    success_count, total_count, coll_name, total_count - success_count,
                )
        self.metrics = raw.get("metrics", {}) or {}
        if not isinstance(self.metrics, dict):
            self.metrics = {}
        self.events = raw.get("events", []) or []
        if not isinstance(self.events, list):
            self.events = []
        self.research_records = raw.get("research_records", {}) or {}
        if not isinstance(self.research_records, dict):
            self.research_records = {}
        self.open_approvals = raw.get("open_approvals", {}) or {}
        if not isinstance(self.open_approvals, dict):
            self.open_approvals = {}

    def _persist(self) -> None:
        """Write the whole store to disk atomically.

        Caller must hold ``self._lock`` (intra-process serialization). The cross-process
        ``flock`` is acquired here so *every* writer (the ~10 mutators that call
        ``_persist`` directly, plus ``_save``) is covered — fixing the H1 gap where only
        ``_save``/``persist_now`` were guarded and concurrent multi-worker writes could
        still corrupt the JSON store.
        """
        if self._store_path is None:
            return
        with self._cross_process_lock():
            payload: dict[str, Any] = {}
            for coll_name, (model_name, _key) in _PERSIST_COLLECTIONS.items():
                coll = getattr(self, coll_name)
                payload[coll_name] = [
                    {"__type__": model_name, "data": obj.model_dump(mode="json")}
                    for obj in coll.values()
                ]
            payload["metrics"] = self.metrics
            payload["events"] = self.events
            payload["research_records"] = self.research_records
            payload["open_approvals"] = self.open_approvals
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_name(self._store_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._store_path)

    @contextlib.contextmanager
    def _cross_process_lock(self) -> Any:
        """Advisory cross-process mutex on a sidecar ``.lock`` file (H1 fix).

        ``threading.Lock`` only serializes threads *within one process*. With multiple
        worker processes (e.g. ``uvicorn --workers N``) two processes could rewrite the
        JSON store concurrently, causing lost updates or (mid-write) corruption. A
        ``flock`` on a sidecar lock file gives the cross-process exclusion the JSON
        store needs. The lock is released automatically on process exit.
        """

        if self._store_path is None:
            yield
            return
        lock_path = self._store_path.with_name(self._store_path.name + ".lock")
        with open(lock_path, "w", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _save(self) -> None:
        # ``_persist`` already acquires the cross-process flock, so we only need the
        # intra-process thread lock here.
        with self._lock:
            self._persist()

    def persist_now(self) -> None:
        """Public persist — call after modifying non-model-backed state (e.g. open_approvals)."""
        self._save()

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:12]}"

    # ----- WorkflowRun -----
    def put_workflow_run(self, run: Any) -> None:
        with self._lock:
            self.workflow_runs[run.run_id] = run
            self._persist()

    def get_workflow_run(self, run_id: str) -> Any | None:
        with self._lock:
            return self.workflow_runs.get(run_id)

    def list_workflow_runs(self) -> list[Any]:
        with self._lock:
            return list(self.workflow_runs.values())

    # ----- StageRun -----
    def put_stage_run(self, stage: Any) -> None:
        with self._lock:
            self.stage_runs[stage.stage_run_id] = stage
            self._persist()

    def get_stage_run(self, stage_run_id: str) -> Any | None:
        with self._lock:
            return self.stage_runs.get(stage_run_id)

    def list_stage_runs(self, run_id: str) -> list[Any]:
        with self._lock:
            return [s for s in self.stage_runs.values() if s.run_id == run_id]

    # ----- DecisionRecord -----
    def put_decision(self, decision: Any) -> None:
        with self._lock:
            self.decisions[decision.decision_id] = decision
            self._persist()

    def get_decision(self, decision_id: str) -> Any | None:
        with self._lock:
            return self.decisions.get(decision_id)

    def list_decisions(self, run_id: str) -> list[Any]:
        with self._lock:
            return [d for d in self.decisions.values() if d.run_id == run_id]

    # ----- Artifact (spec §9.4 publish_artifact) -----
    def put_artifact(self, artifact: Any) -> None:
        with self._lock:
            self.artifacts[artifact.artifact_id] = artifact
            self._persist()

    def get_artifact(self, artifact_id: str) -> Any | None:
        with self._lock:
            return self.artifacts.get(artifact_id)

    def list_artifacts(self, run_id: str | None = None) -> list[Any]:
        with self._lock:
            if run_id is None:
                return list(self.artifacts.values())
            # P2-9 fix: exact-match against this run's own stage_run_ids (plus the
            # workflow run_id itself) instead of a *substring* scan. A substring match
            # leaks artifacts from unrelated runs whose stage_run_id merely contains
            # `run_id` as a substring (e.g. "run_1" vs "run_10_stage_2"). Inlining the
            # stage-id collection avoids re-acquiring the non-reentrant lock.
            stage_ids = {
                s.stage_run_id
                for s in self.stage_runs.values()
                if getattr(s, "run_id", None) == run_id
            }
            allowed = stage_ids | {run_id}
            return [a for a in self.artifacts.values() if str(a.producer_ref) in allowed]

    # ----- LessonCard (spec §9.4 register_lesson -> reinjection) -----
    def put_lesson(self, lesson: Any) -> None:
        with self._lock:
            self.lessons[lesson.lesson_id] = lesson
            self._persist()

    def get_lesson(self, lesson_id: str) -> Any | None:
        with self._lock:
            return self.lessons.get(lesson_id)

    def list_lessons(self, run_id: str | None = None) -> list[Any]:
        with self._lock:
            if run_id is None:
                return list(self.lessons.values())
            return [l for l in self.lessons.values() if l.source_run_id == run_id]

    # ----- Metrics (spec §9.4 record_metric) -----
    def record_metric(self, run_id: str, name: str, value: float, tags: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.metrics.setdefault(run_id, []).append(
                {"name": name, "value": value, "tags": tags or {}}
            )
            self._persist()

    def list_metrics(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.metrics.get(run_id, []))

    # ----- Event log -----
    def append_event(self, event: Any) -> None:
        with self._lock:
            self.events.append(event.model_dump(mode="json"))
            self._persist()

    def list_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if run_id is None:
                return list(self.events)
            return [e for e in self.events if e.get("run_id") == run_id]

    # ----- Research records (leaderboard) -----
    def put_research_record(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self.research_records[rec["record_id"]] = rec
            self._persist()

    def get_research_record(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.research_records.get(record_id)

    def list_research_records(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rs = list(self.research_records.values())
            if task_id:
                rs = [r for r in rs if r.get("task_id") == task_id]
            return rs

    def update_research_record(self, record_id: str, **fields: Any) -> None:
        with self._lock:
            rec = self.research_records.get(record_id)
            if rec is None:
                return
            rec.update(fields)
            self._persist()

    def delete_research_record(self, record_id: str) -> bool:
        with self._lock:
            if record_id in self.research_records:
                del self.research_records[record_id]
                self._persist()
                return True
            return False
