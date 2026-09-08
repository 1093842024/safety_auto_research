#!/usr/bin/env python3
"""Number-trace — every asserted number must trace to the run's data (Phase 3, opt-in).

Adapted from ``spark-to-paper-skills/skills/ts-paper-experiment/scripts/
check_result_recomputation.py`` (GR-019: reported table/figure values must be
recomputable from raw logs). In this platform a run's "raw logs" are its
``scores`` series (harvested from ``eval_completed`` events). The gate:

* extracts every numeric assertion from each curated claim
  ("accuracy 0.92", "improves by 3.2%", "error dropped to 0.05");
* classifies each number as TRACED (within tolerance of some value in ``scores``,
  or explicitly named in the evidence) or ORPHAN (no corresponding metric and the
  evidence names no metric);
* fails on any ORPHAN number or any number that CONTRADICTS the data (the evidence
  names a metric, but that metric's series does not contain the value).

Percentage scaling is tolerated: a claim of "95%" is matched against a series
value of 0.95 (and vice-versa), so unit conventions don't manufacture false
orphans.

Fail-closed: a number you cannot trace is treated like a fabricated DOI — a hard
fail. This gate is NOT in ``STAGE_GATES["all"]``; it runs only in the opt-in
``"deep"`` stage. stdlib-only.
"""

from __future__ import annotations

import math
import re

# NOTE: outcome helpers are imported lazily inside gate_number_trace (see
# claims_lint.py's header) to avoid a circular import with gate_runner.

_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
# Words that, when present in evidence, tie a number to a named run metric.
_METRIC_WORDS = re.compile(
    r"\b(accuracy|acc|f1|auc|roc|precision|recall|score|loss|error|rmse|mae|mse|nll|"
    r"logloss|cv|r2|spearman|pearson|map|ndcg|bleu|rouge|perplexity)\w*\b",
    re.I,
)


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM.finditer(text or ""):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            pass
    return out


def _clean_scores(scores: dict) -> dict[str, list[float]]:
    clean: dict[str, list[float]] = {}
    for nm, vals in (scores or {}).items():
        series: list[float] = []
        for v in (vals or []):
            try:
                series.append(float(v))
            except (TypeError, ValueError):
                pass
        if series:
            clean[str(nm)] = series
    return clean


def _traced_in(keys: list[str], clean: dict[str, list[float]], num: float,
               rel_tol: float = 0.02) -> bool:
    """Is ``num`` within tolerance of a value in the given series (allowing % scaling)?"""
    subjects = [clean[k] for k in keys if k in clean] or list(clean.values())
    for series in subjects:
        for v in series:
            if not math.isfinite(v):
                continue
            for scale in (1.0, 100.0, 0.01, 1000.0, 0.001):
                c = num * scale
                if c == v or abs(c - v) <= max(rel_tol, rel_tol * abs(v)):
                    return True
    return False


def _named_metric_keys(evidence: str, clean: dict[str, list[float]]) -> list[str] | None:
    """Return the series keys referenced by metric words in the evidence, or None if
    the evidence names no metric at all."""
    words = {w.lower() for w in _METRIC_WORDS.findall(evidence or "")}
    if not words:
        return None
    return [k for k in clean if any(w in k.lower() for w in words)]


def gate_number_trace(ctx: dict) -> GateOutcome:
    """Trace every asserted number in the curated claims back to the run's scores."""
    from .gate_runner import GateOutcome
    from .gate_runner import _failed
    from .gate_runner import _passed
    from .gate_runner import _waived

    name = "number_trace"
    claims = ctx.get("claims")
    if not claims:
        return _waived(name, "no claims in context")

    clean = _clean_scores(ctx.get("scores"))
    if not clean:
        return _waived(name, "no scores to trace against")

    issues: list[dict] = []
    n_numbers = 0
    for i, item in enumerate(claims):
        if not isinstance(item, dict):
            return _failed(name, f"claim #{i} is not an object")
        claim = str(item.get("claim", "") or "")
        evidence = str(item.get("evidence", "") or "")
        nums = _numbers(claim)
        n_numbers += len(nums)
        for num in nums:
            named_keys = _named_metric_keys(evidence, clean)
            if named_keys is None:
                # Evidence names no metric: the number must appear somewhere in the data.
                if _traced_in([], clean, num):
                    continue
                issues.append(
                    {"rule": "orphan_number", "i": i, "value": num, "claim": claim[:80]}
                )
            else:
                # Evidence names a metric: that metric's series must contain the value.
                if _traced_in(named_keys, clean, num):
                    continue
                issues.append(
                    {"rule": "number_contradicts_data", "i": i, "value": num,
                     "named_metrics": named_keys, "claim": claim[:80]}
                )

    if issues:
        rules = sorted({x["rule"] for x in issues})
        return _failed(
            name,
            f"{len(issues)} asserted number(s) are untraceable/contradictory: " + ", ".join(rules),
            issues=issues,
            n_numbers=n_numbers,
        )
    return _passed(
        name,
        f"all {n_numbers} asserted number(s) trace to run data",
        n_numbers=n_numbers,
    )
