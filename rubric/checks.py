"""Programmatic evaluation of rubric criteria — what makes the rubric *executable*.

The outer audit (``layer_11_external_audit``) calls :func:`evaluate_criteria` with the
inner loop's **curated** result only (metrics dict + report reference + config
snapshot). That keeps the existing isolation invariant intact: the checker never sees
the inner loop's experimental narrative, only its measurements.

Every criterion resolves to one of the platform's four constraint statuses so it drops
straight into the existing ``AuditReport.constraints`` shape:

* ``verified`` — the check passed on real data;
* ``partial``  — the check passed weakly, or the criterion is knowingly blocked
  (a retained-but-unmeasurable requirement is *not* a pass and *not* a conflict);
* ``conflict`` — the check ran and FAILED (this is the strong negative signal);
* ``missing``  — the data needed to run the check was not reported at all.

Two design rules, both load-bearing:

1. **A check that cannot be executed must never silently return ``verified``.**
2. **A check that cannot be executed must not lower the research's score either.**
   Each verdict therefore carries ``evaluable``: ``False`` when the criterion was
   blocked (the task definition, not the research, lacks the input) or when the needed
   evidence was never curated to the auditor. Such criteria are still REPORTED as unmet
   (rule 1) but are given zero weight in the aggregate confidence, because charging the
   research for a task-definition gap is a misattribution — that gap is already reported
   by the rubric *review* (准确性/完整性/科学性) and by layer_12's own gate.
   Consequence: rubric criteria can only make a verdict stricter when a real check
   actually FAILS, never merely because data is absent.
"""

from __future__ import annotations

import math
from typing import Any
from typing import Callable
from typing import Sequence

from ..platform_contracts.objects import RubricCriterion

# Score attached to each status (feeds the audit's weighted confidence aggregation).
STATUS_SCORE = {"verified": 1.0, "partial": 0.5, "conflict": 0.1, "missing": 0.0}
# A knowingly-blocked criterion scores slightly above a plain miss: the requirement was
# preserved and the blocker is documented, but it is still NOT satisfied.
BLOCKED_SCORE = 0.3


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _lookup(metrics: dict[str, Any], keys: Sequence[str] | str) -> tuple[str | None, float | None]:
    """First finite metric among *keys*. Returns ``(key_used, value)``."""

    if isinstance(keys, str):
        keys = [keys]
    for k in keys:
        if k in metrics:
            v = _finite(metrics[k])
            if v is not None:
                return k, v
    # Case-insensitive / ``@``-normalized retry so "Recall@10" matches "recall_at_10".
    norm = {str(k).strip().lower().replace("@", "_at_"): k for k in metrics}
    for k in keys:
        nk = str(k).strip().lower().replace("@", "_at_")
        if nk in norm:
            v = _finite(metrics[norm[nk]])
            if v is not None:
                return norm[nk], v
    return None, None


def _cmp(op: str, lhs: float, rhs: float) -> bool:
    if op == "ge":
        return lhs >= rhs
    if op == "gt":
        return lhs > rhs
    if op == "le":
        return lhs <= rhs
    if op == "lt":
        return lhs < rhs
    raise ValueError(f"unsupported comparison op: {op!r}")


# --------------------------------------------------------------------------- #
# Individual check kinds                                                      #
# --------------------------------------------------------------------------- #
def _check_metric_present(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    key, val = _lookup(ctx["metrics"], check.get("metric", []))
    if val is None:
        return "missing", "运行未上报该指标（或上报值非有限数值）"
    return "verified", f"{key}={val}"


def _check_metric_threshold(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    key, val = _lookup(ctx["metrics"], check.get("metric", []))
    target = _finite(check.get("value"))
    if target is None:
        return "partial", "门限值缺失或非法，无法程序化判定"
    if val is None:
        return "missing", "运行未上报该指标，无法与门限比较"
    op = str(check.get("op", "ge"))
    ok = _cmp(op, val, target)
    sym = {"ge": ">=", "gt": ">", "le": "<=", "lt": "<"}[op]
    return ("verified" if ok else "conflict"), f"{key}={val} {sym} {target} -> {ok}"


def _check_metric_improves(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    key, val = _lookup(ctx["metrics"], check.get("metric", []))
    base = _finite(check.get("value"))
    margin = _finite(check.get("margin")) or 0.0
    if base is None:
        return "partial", "基线值缺失，无法判定改进幅度"
    if val is None:
        return "missing", "运行未上报该指标，无法与基线比较"
    op = str(check.get("op", "ge"))
    delta = (base - val) if op in ("le", "lt") else (val - base)
    ok = delta >= margin
    return ("verified" if ok else "conflict"), (
        f"{key}={val} vs baseline={base}, delta={round(delta, 6)} "
        f"(需 >= {margin}) -> {ok}"
    )


def _check_metric_gap(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    key, val = _lookup(ctx["metrics"], check.get("metric", []))
    ref_key, ref = _lookup(ctx["metrics"], check.get("baseline_metric", []))
    tol = _finite(check.get("value"))
    if tol is None:
        return "partial", "容差缺失，无法判定差距"
    if val is None:
        return "missing", "缺少调优侧指标，无法计算差距"
    if ref is None:
        return "missing", "缺少留出集 / 测试集指标，无法核验泛化性（未做留出复核）"
    gap = abs(val - ref)
    ok = gap <= tol
    if ok:
        status = "verified"
    elif gap <= tol * 1.6:
        status = "partial"  # over tolerance but not egregious
    else:
        status = "conflict"
    return status, f"{key}={val} vs {ref_key}={ref}, gap={round(gap, 6)} (容差 {tol})"


def _check_real_eval(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    if ctx.get("real_eval"):
        return "verified", f"真实评测引用: {ctx.get('report_ref')}"
    if ctx.get("report_ref"):
        return "partial", f"有报告引用但未标记为真实评测: {ctx.get('report_ref')}"
    return "missing", "无评测报告引用，无法确认指标来自真实执行"


def _check_artifact_exists(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    want = str(check.get("artifact_type") or "")
    have = {str(a) for a in (ctx.get("artifact_types") or [])}
    if not have:
        return "missing", "本次运行未上报任何产出类型"
    if want and want not in have:
        return "conflict", f"缺少要求的产出类型 {want}（已有 {sorted(have)}）"
    return "verified", f"产出类型 {want or sorted(have)} 已发布"


def _check_config_min(check: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    field = str(check.get("field") or "")
    need = _finite(check.get("value"))
    cfg = ctx.get("config") or {}
    got = _finite(cfg.get(field))
    if need is None:
        return "partial", "阈值缺失，无法判定"
    if got is None:
        return "missing", f"运行配置未记录 {field}，无法核验重复测量次数"
    ok = got >= need
    return ("verified" if ok else "conflict"), f"{field}={got} (需 >= {need}) -> {ok}"


_CHECKERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[str, str]]] = {
    "metric_present": _check_metric_present,
    "metric_threshold": _check_metric_threshold,
    "metric_improves": _check_metric_improves,
    "metric_gap": _check_metric_gap,
    "real_eval": _check_real_eval,
    "artifact_exists": _check_artifact_exists,
    "config_min": _check_config_min,
}


# --------------------------------------------------------------------------- #
# Public entry                                                                #
# --------------------------------------------------------------------------- #
def evaluate_criterion(
    criterion: RubricCriterion,
    ctx: dict[str, Any],
    judge: Callable[[str, str, str], float] | None = None,
    answer: str = "",
) -> dict[str, Any]:
    """Evaluate one criterion against the curated result context.

    ``ctx`` keys: ``metrics`` (dict), ``report_ref`` (str|None), ``real_eval`` (bool),
    ``artifact_types`` (list[str]), ``config`` (dict).

    Returns a constraint dict in the platform's existing ``AuditReport.constraints``
    shape, extended with rubric provenance (``criterion_id`` / ``dimension`` /
    ``check_kind``) so the frontend can render the rubric verdict per criterion.
    """

    kind = str((criterion.check or {}).get("kind") or "judge")
    checker = _CHECKERS.get(kind)

    if checker is not None:
        status, note = checker(criterion.check or {}, ctx)
        score = STATUS_SCORE[status]
        evaluated_by = f"programmatic:{kind}"
    else:
        # ``judge`` kind (or an unknown kind): fall back to the claim judge, explicitly.
        if judge is None:
            status, note, score = "partial", "无程序化判定方式且未配置 judge", 0.5
        else:
            raw = float(judge(answer, criterion.requirement, str(ctx.get("metrics") or {})))
            score = max(0.0, min(1.0, raw))
            status = "verified" if score >= 0.7 else "partial" if score >= 0.4 else "missing"
            note = f"judge(score)={round(score, 3)}"
        evaluated_by = "judge" if kind == "judge" else f"judge(unknown_kind:{kind})"

    # A knowingly-blocked criterion can never be a pass: the requirement was retained
    # but the platform cannot measure it here (AutoSciRub: keep the requirement, record
    # the blocker, do not silently drop or silently satisfy it).
    if criterion.blocked_reason:
        if status == "verified":
            status = "partial"
        score = min(score, BLOCKED_SCORE)
        note = f"{note}；受阻: {criterion.blocked_reason}"

    # Was the criterion actually *decided* on evidence? ``missing`` means the evidence
    # never reached the auditor, and ``blocked_reason`` means the task definition lacks
    # the input — neither is a research failure, so both are excluded from the weighted
    # aggregate (see the module docstring, rule 2) while remaining reported as unmet.
    evaluable = status != "missing" and not criterion.blocked_reason

    return {
        "id": f"rubric:{criterion.criterion_id}",
        "description": criterion.requirement,
        "status": status,
        "evidence_ref": ctx.get("report_ref"),
        "score": round(score, 4),
        "note": note,
        # ---- rubric provenance (extra keys; the audit shape allows them) ----
        "criterion_id": criterion.criterion_id,
        "dimension": criterion.dimension,
        "priority": criterion.priority,
        "goal_ids": list(criterion.goal_ids),
        "satisfaction_condition": criterion.satisfaction_condition,
        "check_kind": kind,
        "evaluated_by": evaluated_by,
        "blocked_reason": criterion.blocked_reason,
        "weight": criterion.weight,
        "evaluable": evaluable,
    }


def evaluate_criteria(
    criteria: Sequence[RubricCriterion],
    ctx: dict[str, Any],
    judge: Callable[[str, str, str], float] | None = None,
    answer: str = "",
) -> list[dict[str, Any]]:
    """Evaluate every criterion; order is preserved (stable rubric ids)."""

    return [evaluate_criterion(c, ctx, judge=judge, answer=answer) for c in criteria]


def summarize(constraints: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per-criterion pass/fail roll-up (AutoSciRub verification-report style).

    Deliberately reports *which* criteria are unmet rather than only a scalar, while
    still exposing the weighted score the existing router consumes.
    """

    passed = [c for c in constraints if c.get("status") == "verified"]
    failed = [c for c in constraints if c.get("status") != "verified"]
    # The scalar is computed over EVALUABLE criteria only (see module docstring rule 2);
    # ``all_satisfied`` / ``failed`` stay honest and count everything.
    scored = [c for c in constraints if c.get("evaluable", True)]
    total_w = sum(float(c.get("weight", 1.0)) for c in scored)
    weighted = (
        sum(float(c.get("score", 0.0)) * float(c.get("weight", 1.0)) for c in scored) / total_w
        if total_w > 0
        else 0.0
    )
    by_dim: dict[str, dict[str, int]] = {}
    for c in constraints:
        d = str(c.get("dimension") or "other")
        slot = by_dim.setdefault(d, {"passed": 0, "failed": 0})
        slot["passed" if c.get("status") == "verified" else "failed"] += 1
    not_evaluable = [c for c in constraints if not c.get("evaluable", True)]
    return {
        "all_satisfied": not failed,
        "passed": len(passed),
        "failed": len(failed),
        # Criteria that could not be decided on evidence (blocked / no data reported).
        # Reported separately so "未达标" is never confused with "无法判定".
        "not_evaluable": len(not_evaluable),
        "not_evaluable_criterion_ids": [
            str(c.get("criterion_id")) for c in not_evaluable if c.get("criterion_id")
        ],
        "weighted_score": round(weighted, 4),
        "by_dimension": by_dim,
        "highest_priority_gaps": [
            str(c.get("description")) for c in failed if c.get("priority") == "high"
        ][:5],
        "unmet_criterion_ids": [str(c.get("criterion_id")) for c in failed if c.get("criterion_id")],
    }
