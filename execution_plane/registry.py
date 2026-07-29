"""Adapter registry — resolves a stage code to its executor (spec §11.2)."""

from __future__ import annotations

from .base import StageExecutor
from .executors import AttackExecutor
from .executors import EvalExecutor
from .executors import LessonExecutor


class AdapterRegistry:
    """Holds the registered stage executors and resolves by stage code."""

    def __init__(self) -> None:
        self._executors: list[StageExecutor] = []

    def register(self, executor: StageExecutor) -> None:
        self._executors.append(executor)

    def resolve(self, stage_code: str) -> StageExecutor | None:
        for ex in self._executors:
            if ex.can_handle(stage_code):
                return ex
        return None


def default_registry() -> AdapterRegistry:
    """Registry wired with the three loop-closing executors."""

    registry = AdapterRegistry()
    registry.register(EvalExecutor())
    registry.register(AttackExecutor())
    registry.register(LessonExecutor())
    return registry
