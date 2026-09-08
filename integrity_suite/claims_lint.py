#!/usr/bin/env python3
"""Claims-lint — evidence-quality gate (Phase 3, opt-in, NOT in the default loop).

Adapted from ``spark-to-paper-skills/skills/ts-paper-cite/scripts/citations_lint.py``
(the "stub / orphan / weak-match" detection that kills the fabricated-reference
path). The unit under test here is not a BibTeX entry but a *curated claim* — the
``{claim, answer, evidence}`` triple the audit already consumes. The same three
failure classes transfer cleanly:

* ``stub_or_incomplete`` -- a claim with no real evidence (placeholder like "TBD")
* ``duplicate_claim``     -- the same claim text appears twice (curated-set decay)
* ``claim_without_anchor`` -- evidence is short and cites no concrete artifact
                              (run id, metric name, file path, or score value)

Fail-closed: any issue is a red gate. This gate is intentionally NOT in
``STAGE_GATES["all"]`` (the default loop); it runs only in the opt-in ``"deep"``
stage, so the default control plane is never affected.

stdlib-only; imports nothing from the web stack.
"""

from __future__ import annotations

import re

# NOTE: the outcome helpers (GateOutcome/_passed/_failed/_waived) are imported
# lazily INSIDE gate_claims_lint, not at module top. gate_runner registers this
# gate at the bottom of its own module; a top-level `from .gate_runner import ...`
# here would create a circular import that breaks `python -m ... gate_runner`.

# A short evidence string that is not real evidence.
_PLACEHOLDER = re.compile(r"^(n/?a|tbd|todo|tba|none|null|unknown|author|pending|—|-+|\.+)$", re.I)
# A real evidence string names a concrete artifact we can chase down.
_ANCHOR = re.compile(
    r"(run[-_:]?id|run_id|metric|accuracy|acc|f1|auc|roc|precision|recall|score|cv_|"
    r"loss|error|figure|fig\.|table|tab\.|appendix|\.csv|\.json|\.png|\.pdf|experiment|"
    r"seed|@|\b[a-f0-9]{8,}\b)",
    re.I,
)
_NONWORDS = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NONWORDS.sub(" ", (text or "").lower()).strip()


def _looks_like_placeholder(ev: str) -> bool:
    e = (ev or "").strip()
    if not e:
        return True
    return bool(_PLACEHOLDER.match(e))


def gate_claims_lint(ctx: dict) -> GateOutcome:
    """Lint the curated claim set for evidence-quality defects."""
    from .gate_runner import GateOutcome
    from .gate_runner import _failed
    from .gate_runner import _passed
    from .gate_runner import _waived

    name = "claims_lint"
    claims = ctx.get("claims")
    if not claims:
        return _waived(name, "no claims in context")
    if not isinstance(claims, list):
        return _failed(name, "claims is not a list")

    issues: list[dict] = []
    seen_norm: dict[str, int] = {}
    for i, item in enumerate(claims):
        if not isinstance(item, dict):
            return _failed(name, f"claim #{i} is not an object: {item!r}")
        claim = str(item.get("claim", "") or "").strip()
        if not claim:
            return _failed(name, f"claim #{i} has empty 'claim' text")
        evidence = str(item.get("evidence", "") or "").strip()
        cid = item.get("id", f"#{i}")

        # (1) stub / incomplete evidence.
        if _looks_like_placeholder(evidence):
            issues.append(
                {"rule": "stub_or_incomplete", "i": i, "id": cid,
                 "claim": claim[:80], "evidence": evidence[:40]}
            )
        # (2) short evidence that names no traceable artifact.
        elif len(evidence) < 20 and not _ANCHOR.search(evidence):
            issues.append(
                {"rule": "claim_without_anchor", "i": i, "id": cid,
                 "claim": claim[:80], "evidence": evidence[:80]}
            )
        # (3) duplicate claim text (curated-set decay).
        n = _norm(claim)
        if n in seen_norm:
            issues.append(
                {"rule": "duplicate_claim", "i": i, "id": cid,
                 "dup_of": seen_norm[n], "claim": claim[:80]}
            )
        else:
            seen_norm[n] = i

    if issues:
        rules = sorted({x["rule"] for x in issues})
        return _failed(
            name,
            f"{len(issues)} claim(s) fail evidence-quality lint: " + ", ".join(rules),
            issues=issues,
            n_claims=len(claims),
        )
    return _passed(
        name,
        f"all {len(claims)} claim(s) carry concrete, non-duplicate evidence",
        n_claims=len(claims),
    )
