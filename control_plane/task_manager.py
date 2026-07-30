"""File-backed parallel task manager (Phase 3, sub-agent/background-jobs pattern).

Weng's harness pattern #3: parallel work must be *explicit and inspectable* — every
task's result is written to a JSON file on disk, so a crash mid-generation never loses
completed evaluations (recovery = files that already exist are not re-run), and the
parent can merge results by reading files instead of holding everything in memory.

Used by the evolutionary driver to evaluate a population's candidates concurrently;
deliberately generic (any ``task_id -> callable`` map) so future sub-agent workloads
(background literature search, parallel red-team sweeps) can reuse it.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from typing import Callable


class BackgroundTaskManager:
    """Run callables in a thread pool; persist each result as a JSON file."""

    def __init__(self, work_dir: str) -> None:
        self.work_dir = work_dir
        os.makedirs(self.work_dir, exist_ok=True)

    def result_path(self, task_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
        return os.path.join(self.work_dir, f"{safe}.json")

    def is_done(self, task_id: str) -> bool:
        path = self.result_path(task_id)
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh).get("status") == "ok"
        except (json.JSONDecodeError, OSError):
            return False

    def load_result(self, task_id: str) -> dict[str, Any] | None:
        path = self.result_path(task_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, task_id: str, payload: dict[str, Any]) -> None:
        tmp = self.result_path(task_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
        os.replace(tmp, self.result_path(task_id))  # atomic publish

    def run_all(
        self,
        tasks: dict[str, Callable[[], Any]],
        *,
        max_workers: int = 4,
    ) -> dict[str, dict[str, Any]]:
        """Execute pending tasks in parallel; return {task_id: result-payload}.

        Tasks whose result file already exists with ``status == "ok"`` are skipped
        (crash recovery) and their stored payload is returned as-is.
        """

        out: dict[str, dict[str, Any]] = {}
        pending: dict[str, Callable[[], Any]] = {}
        for task_id, fn in tasks.items():
            if self.is_done(task_id):
                out[task_id] = self.load_result(task_id) or {}
            else:
                pending[task_id] = fn

        def _run(task_id: str, fn: Callable[[], Any]) -> tuple[str, dict[str, Any]]:
            try:
                result = fn()
                payload = {
                    "task_id": task_id,
                    "status": "ok",
                    "result": result,
                    "ts": time.time(),
                }
            except Exception as exc:  # a failed candidate must not kill the generation
                payload = {
                    "task_id": task_id,
                    "status": "error",
                    "error": str(exc),
                    "ts": time.time(),
                }
            self._write(task_id, payload)
            return task_id, payload

        if pending:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_run, tid, fn) for tid, fn in pending.items()]
                for fut in futures:
                    task_id, payload = fut.result()
                    out[task_id] = payload
        return out

    def list_tasks(self) -> list[dict[str, Any]]:
        """Inspectable state: every task file on disk (parent's process manager view)."""

        tasks = []
        for name in sorted(os.listdir(self.work_dir)):
            if name.endswith(".json"):
                data = self.load_result(name[:-5])
                if data:
                    tasks.append(data)
        return tasks
