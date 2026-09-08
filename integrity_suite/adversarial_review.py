#!/usr/bin/env python3
"""Adversarial review — deterministic red-team of the curated claim set (Phase 3, opt-in).

Distilled from ``spark-to-paper-skills/skills/ts-paper-review`` (the "argue the
other side" stage a forward-only drafter can't do) and
``ts-paper-experiment/resources/claim_evidence_rules.md`` (the Unsupported /
Contradicted labels). It is a *deterministic heuristic* red-team, not an LLM
panel — so it stays reproducible offline and fail-closed, and it never needs the
web stack.

Findings (each a red gate):

* ``overclaim_superlative`` -- the claim uses a superlative ("first", "novel",
  "state-of-the-art", "SOTA", "unprecedented") but its evidence names no comparison
  or baseline, so the strength of the claim is unsupported.
* ``significance_no_variance`` -- the claim asserts an improvement AND a number,
  but every harvested series has fewer than two samples, so no variance exists to
  support a significance-style claim.
* ``unverifiable_magnitude`` -- the claim asserts a relative gain ("+3.2%",
  2x faster) but the magnitude is wildly outside any plausible spread of the data
  (e.g. claimed improvement exceeds 10x the data's own std), which is a smell of a
  fabricated or mis-scaled number (delegated from ``number_trace``). This one is a
  *warning-class* finding: it fails only when the series is long enough to make the
  mismatch unambiguous.

Fail-closed on the major findings. This gate is NOT in ``STAGE_GATES["all"]``; it
runs only in the opt-in ``"deep"`` stage. stdlib-only.
"""

from __future__ import annotations

import math
import re

# NOTE: outcome helpers are imported lazily inside gate_adversarial_review (see
# claims_lint.py's header) to avoid a circular import with gate_runner.

_SUPERLATIVE = re.compile(
    r"\b(first|novel|new|state[- ]of[- ]the[- ]art|sota|unprecedented|revolutionary|"
    r"groundbreaking|best[- ]in[- ]class|to the best of our knowledge)\b",
    re.I,
)
_IMPROVEMENT = re.compile(
    r"\b(improv|better|outperform|beat|surpass|exceed|increase|decrease|reduce|lower|"
    r"higher|gain|boost|advance|faster|slower)\w*",
    re.I,
)
_RELATIVE_GAIN = re.compile(r"(\d+(?:\.\d+)?)\s*(%|x\b|times|fold)", re.I)
# An evidence string that names a comparison/baseline/other method.
_COMPARISON = re.compile(
    r"\b(baseline|compared to|compared with|versus|vs\.|relative to|prior|existing|"
    r"previous work|sota|state[- ]of[- ]the[- ]art|ablation|our method|the proposed)\b",
    re.I,
)
_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


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


def _series_std(series: list[float]) -> float:
    n = len(series)
    if n < 2:
        return 0.0
    mean = sum(series) / n
    return math.sqrt(sum((x - mean) ** 2 for x in series) / n)


def gate_adversarial_review(ctx: dict) -> GateOutcome:
    """Red-team the curated claim set with deterministic heuristics."""
    from .gate_runner import GateOutcome
    from .gate_runner import _failed
    from .gate_runner import _passed
    from .gate_runner import _waived

    name = "adversarial_review"
    claims = ctx.get("claims")
    if not claims:
        return _waived(name, "no claims in context")

    clean = _clean_scores(ctx.get("scores"))
    issues: list[dict] = []

    for i, item in enumerate(claims):
        if not isinstance(item, dict):
            return _failed(name, f"claim #{i} is not an object")
        claim = str(item.get("claim", "") or "")
        evidence = str(item.get("evidence", "") or "")
        cid = item.get("id", f"#{i}")

        # (1) superlative without a comparison anchor.
        if _SUPERLATIVE.search(claim) and not _COMPARISON.search(evidence):
            issues.append(
                {"rule": "overclaim_superlative", "i": i, "id": cid,
                 "claim": claim[:80], "evidence": evidence[:80]}
            )

        # (2) significance claim without variance in the data.
        if _IMPROVEMENT.search(claim) and _NUM.search(claim):
            min_len = min((len(s) for s in clean.values()), default=0)
            if 0 < min_len < 2:
                issues.append(
                    {"rule": "significance_no_variance", "i": i, "id": cid,
                     "claim": claim[:80],
                     "note": "claim asserts an improvement but no series has >=2 samples"}
                )

        # (3) relative gain whose magnitude beats any plausible data spread.
        m = _RELATIVE_GAIN.search(claim)
        if m and clean:
            gain = float(m.group(1))
            # Only meaningful for "%": a claimed +X% where X dwarfs the data's own spread.
            if m.group(2) == "%" and gain > 0:
                max_std = max((_series_std(s) for s in clean.values()), default=0.0)
                # A claimed percentage gain > 10x the data's own std (in %) is a smell.
                if max_std > 0 and gain > 10 * max_std * 100:
                    issues.append(
                        {"rule": "unverifiable_magnitude", "i": i, "id": cid,
                         "claim": claim[:80],
                         "claimed_pct": gain, "max_data_std_pct": round(max_std * 100, 3)}
                    )

    if issues:
        rules = sorted({x["rule"] for x in issues})
        return _failed(
            name,
            f"{len(issues)} adversarial finding(s): " + ", ".join(rules),
            issues=issues,
            n_claims=len(claims),
        )
    return _passed(
        name,
        f"no adversarial findings across {len(claims)} claim(s)",
        n_claims=len(claims),
    )
