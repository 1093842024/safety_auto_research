"""Task-specific **executable scoring rubric** induction and standard review.

This package implements the platform's rubric stage — the step that answers, *before*
any research result exists, the question "按什么标准判定这次研究成功":

* a task that **did not** declare a usable evaluation standard gets one
  synthesized (correct, effective, scientific — and machine-checkable);
* a task that **did** declare one gets it audited along three dimensions
  (accuracy / completeness / scientificity) and then normalized into the same
  executable contract.

Method lineage: AutoSciRub (*Learning to Evaluate Before Improving: Automatic Rubric
Induction for Automatic Research Agents*) — goal skeleton -> feasibility profile ->
criterion synthesis -> criterion-level verification. Two adaptations were made for this
platform:

1. criteria carry a programmatic ``check`` spec, so verification is genuinely
   *executable* against measured metrics instead of relying on an LLM judge;
2. the rubric is produced by the CONTROL PLANE and frozen, so the optimizing inner
   loop can read its grading contract but can never regenerate or relax it.

Zero heavy dependencies: this package imports only ``platform_contracts`` (plus a lazy,
optional LLM client), so it can be imported from the control plane, the execution plane,
and the benchmark-task registry without creating an import cycle.
"""

from .checks import BLOCKED_SCORE
from .checks import STATUS_SCORE
from .checks import evaluate_criteria
from .checks import evaluate_criterion
from .checks import summarize
from .engine import RubricEngine
from .engine import induce_rubric
from .engine import llm_enabled
from .engine import review_task_standard
from .review import review_standard
from .spec import TaskSpec
from .spec import normalize_metric
from .synthesize import DEFAULT_GAP_TOLERANCE
from .synthesize import DEFAULT_IMPROVEMENT_MARGIN
from .synthesize import MIN_FOLDS
from .synthesize import synthesize_rubric

__all__ = [
    "BLOCKED_SCORE",
    "DEFAULT_GAP_TOLERANCE",
    "DEFAULT_IMPROVEMENT_MARGIN",
    "MIN_FOLDS",
    "RubricEngine",
    "STATUS_SCORE",
    "TaskSpec",
    "evaluate_criteria",
    "evaluate_criterion",
    "induce_rubric",
    "llm_enabled",
    "normalize_metric",
    "review_standard",
    "review_task_standard",
    "summarize",
    "synthesize_rubric",
]
