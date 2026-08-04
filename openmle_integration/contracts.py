"""Local re-implementation of the OpenMLE/``dojo`` interface contract (Phase A).

These dataclasses / ABC mirror the signatures found in the vendored upstream kernel
(``vendor/openmle_dojo/dojo/...``) **exactly**, so that integration code written against
this module can later be pointed at the real ``dojo`` package with no signature changes.

Upstream references (for faithful porting later):
- ``core/tasks/base.py``      -> :class:`Task`
- ``core/tasks/constants.py`` -> outcome-key constants
- ``core/interpreters/base.py`` -> :class:`Interpreter`, :class:`ExecutionResult`
- ``core/solvers/utils/journal.py`` -> :class:`Node`, :class:`Journal`
- ``core/solvers/utils/metric.py`` -> :class:`MetricValue`

No third-party imports beyond the standard library + numpy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# Outcome-key constants (mirror dojo.core.tasks.constants)
# --------------------------------------------------------------------------
class TaskOutcome(str, Enum):
    EXECUTION_OUTPUT = "execution_output"
    TASK_DESCRIPTION = "task_description"
    TEST_FITNESS = "test_fitness"
    VALIDATION_FITNESS = "validation_fitness"
    VALID_SOLUTION = "valid_solution"
    VALID_SOLUTION_FEEDBACK = "valid_solution_feedback"
    AUX_EVAL_INFO = "aux_eval_info"
    SUCCESS_STATUS = "success_status"


# Convenience module-level names (match dojo's ``constants.py`` style).
EXECUTION_OUTPUT = TaskOutcome.EXECUTION_OUTPUT.value
TASK_DESCRIPTION = TaskOutcome.TASK_DESCRIPTION.value
TEST_FITNESS = TaskOutcome.TEST_FITNESS.value
VALIDATION_FITNESS = TaskOutcome.VALIDATION_FITNESS.value
VALID_SOLUTION = TaskOutcome.VALID_SOLUTION.value
VALID_SOLUTION_FEEDBACK = TaskOutcome.VALID_SOLUTION_FEEDBACK.value
AUX_EVAL_INFO = TaskOutcome.AUX_EVAL_INFO.value

FAILED_STATUSES = {"buggy", "error", "failed", "invalid", "sandbox_error", "timeout"}
SUCCESS_STATUSES = {"ok", "passed", "success", "valid"}


# --------------------------------------------------------------------------
# Interpreter (mirror dojo.core.interpreters.base)
# --------------------------------------------------------------------------
@dataclass
class ExecutionResult:
    """Result of running code in an interpreter (mirror dojo)."""

    term_out: str = ""
    exec_time: float = 0.0
    exit_code: int = 0
    eval_return: Any = None
    timed_out: bool = False


class Interpreter(ABC):
    """Abstract execution environment (mirror dojo.core.interpreters.base.Interpreter)."""

    @abstractmethod
    def run(self, code: str, file_name: str = "solution.py") -> ExecutionResult:
        ...


# --------------------------------------------------------------------------
# Metric value (mirror dojo.core.solvers.utils.metric.MetricValue)
# --------------------------------------------------------------------------
@dataclass
class MetricValue:
    """Comparison is by "better", not "bigger" (mirror dojo)."""

    value: float
    maximize: bool
    info: Dict[str, Any] = field(default_factory=dict)

    def better_than(self, other: Optional["MetricValue"]) -> bool:
        if other is None:
            return True
        if self.maximize:
            return self.value > other.value
        return self.value < other.value


@dataclass
class WorstMetricValue(MetricValue):
    """Sentinel used when a node is buggy/failed (always worse than any valid value)."""

    value: float = float("inf")
    maximize: bool = True
    info: Dict[str, Any] = field(default_factory=dict)

    def better_than(self, other: Optional["MetricValue"]) -> bool:
        return False


# --------------------------------------------------------------------------
# Search tree node / journal (mirror dojo.core.solvers.utils.journal)
# --------------------------------------------------------------------------
@dataclass
class Node:
    """A node in the program-evolution search tree (mirror dojo Node, trimmed)."""

    node_id: str
    code: str = ""
    plan: str = ""
    parents: List[str] = field(default_factory=list)
    children: List[str] = field(default_factory=list)
    operator: Optional[str] = None  # draft | improve | debug | crossover
    operators_metrics: Dict[str, Any] = field(default_factory=dict)
    term_out: str = ""
    exec_time: float = 0.0
    exit_code: int = 0
    analysis: str = ""
    metric: Optional[MetricValue] = None
    is_buggy: bool = False
    generation: int = 0


class Journal:
    """Maintains the whole search tree (mirror dojo Journal, in-memory)."""

    def __init__(self) -> None:
        self._nodes: Dict[str, Node] = {}

    def add_node(self, node: Node) -> None:
        self._nodes[node.node_id] = node
        for pid in node.parents:
            parent = self._nodes.get(pid)
            if parent is not None and node.node_id not in parent.children:
                parent.children.append(node.node_id)

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def all_nodes(self) -> List[Node]:
        return list(self._nodes.values())

    def snapshot(self) -> Dict[str, Any]:
        return {
            nid: {
                "operator": n.operator,
                "metric": n.metric.value if n.metric else None,
                "is_buggy": n.is_buggy,
                "parents": n.parents,
            }
            for nid, n in self._nodes.items()
        }


# --------------------------------------------------------------------------
# Task ABC (mirror dojo.core.tasks.base.Task) -- the core integration seam
# --------------------------------------------------------------------------
class Task(ABC):
    """Abstract MLE task with a verifiable execution environment.

    Mirrors ``dojo.core.tasks.base.Task``:
      - ``prepare``   -> (initial_state, task_info)
      - ``step_task`` -> (new_state, outcome_dict)
      - ``evaluate_fitness`` -> score dict
      - ``close``     -> cleanup
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    @abstractmethod
    def prepare(self, **task_args: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        ...

    @abstractmethod
    def step_task(self, state: Dict[str, Any], action: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        ...

    @abstractmethod
    def evaluate_fitness(
        self,
        solution: Optional[Any] = None,
        state: Optional[Dict[str, Any]] = None,
        interpreter: Optional[Interpreter] = None,
        aux_info: Optional[Dict[str, Any]] = None,
    ) -> Any:
        ...

    @abstractmethod
    def close(self, state: Dict[str, Any]) -> None:
        ...
