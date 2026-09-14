"""The normalized *task specification* the rubric engine reads.

The rubric engine must work for three different callers that all describe a research
task slightly differently:

1. the curated benchmark catalog (``benchmark_tasks.BenchmarkTask``),
2. a half-filled custom-task registration form (``registry.TASK_TYPE_SPECS`` values),
3. a live workflow run (``WorkflowRun.objective_snapshot``).

:class:`TaskSpec` is the single normalized view over all three, so the engine has
exactly one input shape and stays trivially unit-testable. It carries only *task
definition* data — never inner-loop results — because the rubric must be built
BEFORE any result exists (no post-hoc standard fitting).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any


def _f(v: Any) -> float | None:
    """Coerce to float, returning ``None`` for empty / unparseable values."""

    if v is None or v == "":
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    # NaN / ±inf are not usable thresholds (they silently win every comparison).
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def normalize_metric(m: str) -> str:
    """Canonical metric token: lowercase, ``@K`` -> ``_at_K`` (mirrors registry.py)."""

    return str(m or "").strip().lower().replace("@", "_at_")


@dataclass
class TaskSpec:
    """Normalized research-task definition (the rubric engine's only input)."""

    task_id: str = ""
    name: str = ""
    task_type: str = ""       # tabular_classification | llm_sft | ... ("" for curated)
    modality: str = ""
    objective: str = ""       # free-text research goal / instruction
    dataset_desc: str = ""
    eval_metric: str = ""
    direction: str = "higher"  # higher | lower
    baseline: float | None = None
    reference: float | None = None
    gates: dict[str, Any] = field(default_factory=dict)
    eval_method: str = ""      # free-text description of how it is scored
    harness: str = ""
    executable: bool = False   # can the platform actually run + measure it?
    # Raw registration form / type_config (extra knobs: cv_folds, threshold, eval_protocol…).
    type_config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ derived
    @property
    def metric(self) -> str:
        return normalize_metric(self.eval_metric)

    @property
    def lower_is_better(self) -> bool:
        return str(self.direction).strip().lower() == "lower"

    @property
    def target_value(self) -> float | None:
        """The pass gate, wherever it was declared.

        Precedence mirrors what the executors actually read: an explicit
        ``gates.threshold`` / ``gates.target`` first, then the registration form's
        ``threshold`` / ``target_value``, then any ``metric>=`` style gate key.
        """

        for key in ("threshold", "target", "target_value"):
            v = _f(self.gates.get(key))
            if v is not None:
                return v
        # ``target_threshold`` is the legacy objective-snapshot spelling used by the
        # closed-loop driver; honouring it here keeps the primary-metric criterion
        # *evaluable* for runs that declare their gate that way (otherwise the rubric
        # silently degrades to a blocked criterion and cannot check the gate at all).
        for key in ("threshold", "target_value", "target_threshold"):
            v = _f(self.type_config.get(key))
            if v is not None:
                return v
        for key, raw in self.gates.items():
            if any(op in str(key) for op in (">=", "<=", ">", "<")):
                v = _f(raw)
                if v is not None:
                    return v
        return None

    @property
    def comparison_gates(self) -> dict[str, float]:
        """``{"accuracy>=": 0.64, ...}`` style gates parsed into metric -> value."""

        out: dict[str, float] = {}
        for key, raw in self.gates.items():
            k = str(key)
            if not any(op in k for op in (">=", "<=", ">", "<")):
                continue
            v = _f(raw)
            if v is not None:
                out[k] = v
        return out

    @property
    def cv_folds(self) -> int | None:
        v = _f(self.type_config.get("cv_folds"))
        return int(v) if v is not None else None

    def has_declared_standard(self) -> bool:
        """True when the task itself declares something usable to grade against.

        A bare metric name is NOT a standard: without a direction *and* at least one
        of {gate, baseline, reference} there is nothing to check a result against, so
        the engine must SYNTHESIZE rather than review.
        """

        if not self.metric:
            return False
        if str(self.direction).strip().lower() not in ("higher", "lower"):
            return False
        return bool(self.gates) or self.baseline is not None or self.reference is not None

    def provided_standard(self) -> dict[str, Any]:
        """The declared standard, as a serializable dict (goes into the rubric)."""

        return {
            "eval_metric": self.eval_metric,
            "direction": self.direction,
            "baseline": self.baseline,
            "reference": self.reference,
            "gates": dict(self.gates),
            "eval_method": self.eval_method,
            "cv_folds": self.cv_folds,
        }

    # ------------------------------------------------------------------ builders
    @classmethod
    def from_benchmark_dict(cls, d: dict[str, Any]) -> "TaskSpec":
        """Build from ``benchmark_tasks.to_dict(task)`` output."""

        return cls(
            task_id=str(d.get("task_id") or ""),
            name=str(d.get("name") or ""),
            task_type=str(d.get("task_type") or ""),
            modality=str(d.get("modality") or ""),
            objective=str(d.get("goal") or d.get("name") or ""),
            dataset_desc=str(d.get("dataset_desc") or ""),
            eval_metric=str(d.get("eval_metric") or ""),
            direction=str(d.get("direction") or "higher"),
            baseline=_f(d.get("baseline")),
            reference=_f(d.get("reference")),
            gates=dict(d.get("gates") or {}),
            eval_method=str(d.get("eval_method") or ""),
            harness=str(d.get("harness") or ""),
            executable=bool(d.get("supported_by_platform")),
            type_config=dict(d.get("type_config") or {}),
            tags=[str(t) for t in (d.get("tags") or [])],
        )

    @classmethod
    def from_registration(cls, task_type: str, values: dict[str, Any]) -> "TaskSpec":
        """Build from a (possibly incomplete) custom-task registration form."""

        gates: dict[str, Any] = {}
        tv = _f(values.get("target_value"))
        if tv is not None:
            gates["target"] = tv
        th = _f(values.get("threshold"))
        if th is not None:
            gates["threshold"] = th
        return cls(
            task_id=str(values.get("task_id") or f"custom.{values.get('name') or 'draft'}"),
            name=str(values.get("name") or ""),
            task_type=str(task_type or ""),
            modality="",
            objective=str(values.get("description") or values.get("name") or ""),
            dataset_desc=str(values.get("description") or ""),
            eval_metric=str(values.get("eval_metric") or ""),
            direction=str(values.get("direction") or "higher"),
            baseline=_f(values.get("baseline")),
            reference=None,
            gates=gates,
            eval_method="",
            harness="",
            executable=False,
            type_config=dict(values),
            tags=[str(t) for t in (values.get("tags") or [])],
        )

    @classmethod
    def from_objective_snapshot(cls, snap: dict[str, Any]) -> "TaskSpec":
        """Build from a live run's ``objective_snapshot``."""

        cfg = dict(snap.get("config") or {})
        type_config = dict(snap.get("type_config") or {})
        if cfg.get("inner_loop"):
            # Surface the inner-loop knobs the engine cares about (cv_folds/threshold)
            # without letting the whole config leak into the rubric.
            il = dict(cfg["inner_loop"])
            for k in ("cv_folds", "threshold"):
                if il.get(k) is not None:
                    type_config.setdefault(k, il[k])
        for k in ("cv_folds", "threshold"):
            if cfg.get(k) is not None:
                type_config.setdefault(k, cfg[k])
        # Legacy top-level gate spelling used by the closed-loop driver.
        if snap.get("target_threshold") is not None:
            type_config.setdefault("target_threshold", snap["target_threshold"])
        return cls(
            task_id=str(snap.get("benchmark_task_id") or snap.get("task_id") or ""),
            name=str(snap.get("name") or ""),
            task_type=str(snap.get("task_type") or ""),
            modality=str(snap.get("modality") or ""),
            objective=str(snap.get("goal") or snap.get("objective") or snap.get("name") or ""),
            dataset_desc=str(snap.get("dataset_desc") or ""),
            eval_metric=str(snap.get("eval_metric") or ""),
            direction=str(snap.get("direction") or "higher"),
            baseline=_f(snap.get("baseline")),
            reference=_f(snap.get("reference")),
            gates=dict(snap.get("gates") or {}),
            eval_method=str(snap.get("eval_method") or ""),
            harness=str(snap.get("harness") or ""),
            executable=bool(snap.get("supported_by_platform")),
            type_config=type_config,
            tags=[str(t) for t in (snap.get("tags") or [])],
        )
