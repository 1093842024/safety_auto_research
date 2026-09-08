#!/usr/bin/env python3
"""Fail-closed gate scheduler — the single place that CONSUMES verdicts.

Adapted from ``spark-to-paper-skills/skills/ts-paper/scripts/run_gates.py`` (item
C1 of ``doc/analysis_spark_to_paper_vs_safety_2026-08-14.md``). The idea worth
importing is *not* any particular check — it is the separation:

    the SCHEDULER knows the order and the stop rule;
    the GATE knows the verdict;
    neither knows the other's internals.

So this module owns exactly two things: a registry (stage -> ordered gates) and a
stop rule (**exit nonzero on the FIRST failing gate**). It owns no measurement
logic. Every gate below is a thin wrapper that calls a validator the platform
*already* ships and reads its return value:

    novelty            -> control_plane.evolution.novelty_filter
    metric_direction   -> control_plane.service._select_objective
    claim_support      -> execution_plane.capabilities.audit_executor.evaluate_constraint
    svg                -> integrity_suite/svg_audit.py (subprocess, sibling path)

Nothing here mutates those functions or changes their thresholds' meaning. If a
gate and its underlying validator ever disagree, the validator wins — fix the
gate.

Exit-code vocabulary (identical to the upstream linters, so a shell/CI caller can
treat every gate the same way):

    0  ok      -- every gate in the stage passed (or was legitimately skipped)
    1  issues  -- a gate failed, OR a gate's machinery is unavailable/broken
    2  usage   -- bad invocation (unknown stage, unreadable context file)

Skip vs fail, the rule that keeps this honest:

* a gate whose **required input is genuinely absent** is *skipped* and reported
  as ``waived`` -- there was nothing to judge;
* a gate whose **machinery is missing** (import error, absent script) is a
  **failure**, never a skip. Fail-closed means an unavailable check can't be
  mistaken for a passing one. (Upstream does the same:
  ``if not script.exists(): return 1``.)

Status strings deliberately reuse the platform's own gate vocabulary from
``platform_contracts.enums.GateResult`` -- ``passed`` / ``failed`` / ``waived``
-- as plain strings, so downstream consumers speak one language without this
module importing (and thus hard-depending on) the contracts package.

CLI (workdir-independent; sibling scripts resolve relative to THIS file):

    python -m safety_auto_research.integrity_suite.gate_runner <context.json> <stage|all>

stdlib-only (dataclasses, json, math, subprocess, sys, tempfile, pathlib).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable

HERE = Path(__file__).resolve().parent
SVG_AUDIT = HERE / "svg_audit.py"  # sibling, resolved from __file__ not cwd

# Exit-code vocabulary shared with the upstream linters.
GATE_OK = 0
GATE_ISSUES = 1
GATE_USAGE = 2

# Status vocabulary — the string values of platform_contracts.enums.GateResult.
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_WAIVED = "waived"


@dataclass
class GateOutcome:
    """One gate's verdict.

    Named ``GateOutcome``, not ``GateResult``: the latter is already taken by the
    platform enum in ``platform_contracts.enums`` and shadowing it here would be
    a readability trap.
    """

    name: str
    code: int
    status: str
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == GATE_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.name,
            "code": self.code,
            "status": self.status,
            "detail": self.detail,
            "payload": self.payload,
        }


def _passed(name: str, detail: str, **payload: Any) -> GateOutcome:
    return GateOutcome(name, GATE_OK, STATUS_PASSED, detail, payload)


def _failed(name: str, detail: str, **payload: Any) -> GateOutcome:
    return GateOutcome(name, GATE_ISSUES, STATUS_FAILED, detail, payload)


def _waived(name: str, detail: str, **payload: Any) -> GateOutcome:
    """Absent input -> nothing to judge. Reported, not silent; code stays 0."""
    return GateOutcome(name, GATE_OK, STATUS_WAIVED, detail, payload)


# ---------------------------------------------------------------- gate: novelty
def gate_novelty(ctx: dict[str, Any]) -> GateOutcome:
    """Diversity-collapse guard, via the platform's own ``novelty_filter``.

    Rejecting a near-duplicate is *normal* loop behaviour, so a nonzero reject
    count is NOT a failure. What this gate catches is the pathological end state:
    a generation where (nearly) every proposal is a duplicate, i.e. the search has
    stopped exploring and the next iteration would burn budget re-measuring
    things already in the archive.

    Fails when the kept fraction drops below ``min_kept_frac`` (default 0.5), or
    when a non-empty batch keeps nothing at all.

    ``novelty_filter`` is called unchanged — including its two-grain rule (cosine
    for config nodes, exact-code dedup for program nodes). This gate never
    reimplements that comparison.
    """
    name = "novelty"
    raw = ctx.get("candidates")
    if not raw:
        return _waived(name, "no candidates in context")

    try:
        from ..control_plane.evolution import Candidate
        from ..control_plane.evolution import novelty_filter
    except Exception as exc:  # machinery unavailable -> fail-closed, never skip
        return _failed(name, f"gate machinery unavailable: {exc!r}")

    candidates: list[Any] = []
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            # CLI path: build the real dataclass so the filter sees real nodes.
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(item.get("candidate_id", f"c{i}")),
                        run_id=str(item.get("run_id", ctx.get("run_id", "ctx"))),
                        params=dict(item.get("params") or {}),
                        generation=int(item.get("generation", 0)),
                        node_kind=str(item.get("node_kind", "config")),
                        code=item.get("code"),
                    )
                )
            except (TypeError, ValueError) as exc:
                return _failed(name, f"candidate #{i} malformed: {exc}")
        else:
            candidates.append(item)  # in-process path: already Candidate objects

    total = len(candidates)
    threshold = float(ctx.get("novelty_threshold", 0.92))
    kept = novelty_filter(
        candidates,
        archived_params=ctx.get("archived_params"),
        archived_codes=ctx.get("archived_codes"),
        threshold=threshold,
    )
    n_kept = len(kept)
    frac = n_kept / total if total else 0.0
    min_frac = float(ctx.get("min_kept_frac", 0.5))
    payload = {
        "total": total,
        "kept": n_kept,
        "rejected": total - n_kept,
        "kept_frac": round(frac, 4),
        "min_kept_frac": min_frac,
        "threshold": threshold,
        "rejected_ids": [
            getattr(c, "candidate_id", "?")
            for c in candidates
            if getattr(c, "status", "") == "rejected_novelty"
        ],
    }
    if n_kept == 0:
        return _failed(
            name,
            f"diversity collapse: all {total} candidate(s) rejected as near-duplicates "
            f"(threshold={threshold}) — the search is no longer exploring",
            **payload,
        )
    if frac < min_frac:
        return _failed(
            name,
            f"diversity thin: kept {n_kept}/{total} ({frac:.0%}) < required {min_frac:.0%}",
            **payload,
        )
    return _passed(name, f"kept {n_kept}/{total} ({frac:.0%}) novel candidate(s)", **payload)


# ------------------------------------------------------ gate: metric direction
# Metrics the selector documents as *always* higher-is-better ("Accuracy-like
# metrics are always higher-is-better; never invert", service.py). A gate that
# ignored this exemption would fire a false positive on every lower-is-better
# task that happens to also report accuracy — so the exemption is mirrored, not
# re-litigated, here.
_ACCURACY_LIKE = ("accuracy", "eval.accuracy", "cv_accuracy", "score")


def _is_accuracy_like(name: str) -> bool:
    nm = name.strip().lower()
    return nm in _ACCURACY_LIKE or nm.replace("eval.", "") in _ACCURACY_LIKE


def gate_metric_direction(ctx: dict[str, Any]) -> GateOutcome:
    """Rankability + direction-consistency, via the platform's ``_select_objective``.

    Three failure modes this closes, all previously silent:

    1. **Unrankable run** — the selector returns ``None`` (no usable metric) or a
       non-finite value. Such a run must not reach a leaderboard: ``None`` sorts
       nowhere and NaN breaks the comparator outright.
    2. **Fabricated objective** — the returned value is not an extreme (``min``
       or ``max``) of *any* reported series, i.e. it did not come from the data.
    3. **Direction regression (the R12 class of bug)** — for ``direction="lower"``
       the value must be some series' ``min``, otherwise some series' ``max``. If
       the selector ever drifts back toward "always take max", this goes red
       instead of the platform quietly ranking lower-is-better tasks backwards.

    Deliberately *not* duplicated: the selector's priority order (declared metric
    -> ``primary`` -> accuracy-like -> any). This gate checks the answer's shape
    against the raw data, so it cannot drift out of sync with that order. The one
    rule it does mirror is the documented accuracy-like exemption above.
    """
    name = "metric_direction"
    scores = ctx.get("scores")
    if not scores:
        return _waived(name, "no scores in context")

    try:
        from ..control_plane.service import _select_objective
    except Exception as exc:
        return _failed(name, f"gate machinery unavailable: {exc!r}")

    metric = str(ctx.get("metric", "") or "")
    direction = str(ctx.get("direction", "higher") or "higher")
    clean: dict[str, list[float]] = {}
    for nm, vals in dict(scores).items():
        try:
            series = [float(v) for v in (vals or [])]
        except (TypeError, ValueError) as exc:
            return _failed(name, f"metric {nm!r} has a non-numeric value: {exc}")
        if series:
            clean[str(nm)] = series
    if not clean:
        return _failed(name, "scores present but every series is empty — run is unrankable")

    selected = _select_objective(clean, metric, direction)
    payload = {"metric": metric, "direction": direction, "selected": selected}
    if selected is None:
        return _failed(
            name,
            f"no usable objective for metric={metric!r} among {sorted(clean)} — run is unrankable",
            **payload,
        )
    if not math.isfinite(float(selected)):
        return _failed(name, f"selected objective is non-finite ({selected!r})", **payload)

    lower = direction.strip().lower() == "lower"
    want = "min" if lower else "max"
    payload["expected_extreme"] = want
    # Checking against EVERY series keeps this independent of the selector's
    # internal priority order (declared -> primary -> accuracy-like -> any).
    directional = any(
        (min(series) if lower else max(series)) == selected for series in clean.values()
    )
    # Documented exemption: an accuracy-like series is never inverted, so its max
    # is a legitimate answer even when direction == "lower".
    exempt = any(
        max(series) == selected for nm, series in clean.items() if _is_accuracy_like(nm)
    )
    any_extreme = any(
        selected in (min(series), max(series)) for series in clean.values()
    )
    payload["accuracy_exempt"] = bool(exempt and not directional)

    if not any_extreme:
        return _failed(
            name,
            f"objective {selected!r} is not the min or max of any reported series "
            f"{ {nm: [min(s), max(s)] for nm, s in clean.items()} } — it did not come "
            f"from the measured data",
            **payload,
        )
    if not directional and not exempt:
        return _failed(
            name,
            f"direction inconsistency: direction={direction!r} implies the {want} of "
            f"some series, but the selector returned {selected!r}, which is no series' "
            f"{want} and is not covered by the accuracy-like exemption "
            f"(R12-class regression)",
            **payload,
        )
    how = f"the {want} of its series" if directional else "an accuracy-like max (never inverted)"
    return _passed(
        name,
        f"objective {selected!r} is {how} (direction={direction!r}) — rankable",
        **payload,
    )


# ---------------------------------------------------------- gate: claim support
def gate_claim_support(ctx: dict[str, Any]) -> GateOutcome:
    """Unsupported-claim guard, via the platform's ``evaluate_constraint``.

    Reads ``claims``: a list of ``{claim, answer, evidence}`` triples drawn from
    the **curated audit input only**. This gate must never be fed inner-loop
    narrative — that would breach the layer_11 read-only curation boundary the
    dual loop depends on (see ``tests/test_dual_loop.py::ContextSeparationTest``).
    It is the caller's job to pass curated material; the gate simply scores what
    it is given with the platform's own judge.

    Fails when any claim scores ``missing`` (default), or below
    ``min_claim_score`` if that is supplied. A custom ``judge`` callable may be
    injected in-process, exactly like ``AuditExecutor`` does; absent one, the
    platform's deterministic ``_default_judge`` is used so the gate stays
    reproducible offline.
    """
    name = "claim_support"
    claims = ctx.get("claims")
    if not claims:
        return _waived(name, "no claims in context")

    try:
        from ..execution_plane.capabilities.audit_executor import _default_judge
        from ..execution_plane.capabilities.audit_executor import evaluate_constraint
    except Exception as exc:
        return _failed(name, f"gate machinery unavailable: {exc!r}")

    judge: Callable[[str, str, str], float] = ctx.get("judge") or _default_judge
    min_score = ctx.get("min_claim_score")
    rows: list[dict[str, Any]] = []
    offenders: list[str] = []
    for i, item in enumerate(claims):
        if not isinstance(item, dict):
            return _failed(name, f"claim #{i} is not an object: {item!r}")
        claim = str(item.get("claim", "") or "")
        if not claim.strip():
            return _failed(name, f"claim #{i} has an empty 'claim' field")
        try:
            score, status = evaluate_constraint(
                claim,
                judge,
                str(item.get("answer", "") or ""),
                str(item.get("evidence", "") or ""),
            )
        except Exception as exc:  # a judge that throws is a red gate, not a skip
            return _failed(name, f"judge raised on claim #{i}: {exc!r}")
        rows.append({"claim": claim, "score": score, "status": status})
        bad = status == "missing" if min_score is None else score < float(min_score)
        if bad:
            offenders.append(f"{claim!r} (score={score}, status={status})")

    payload = {"claims": rows, "min_claim_score": min_score, "unsupported": len(offenders)}
    if offenders:
        return _failed(
            name,
            f"{len(offenders)}/{len(rows)} claim(s) lack evidentiary support: "
            + "; ".join(offenders[:5])
            + (" ..." if len(offenders) > 5 else ""),
            **payload,
        )
    return _passed(name, f"all {len(rows)} claim(s) supported", **payload)


# --------------------------------------------------------------- gate: svg
# Closed whitelist of auditor tuning flags the gate will forward. Arity = number
# of value tokens each flag consumes. Anything else — most importantly ``--json``
# (argparse last-wins would let a caller retarget the report write to an
# arbitrary path — verified empirically, review 2026-09-08 N2) and ``--selftest``
# — is rejected before a subprocess is ever spawned.
_SVG_AUDIT_FLAGS: dict[str, int] = {
    "--min-font-px": 1,
    "--font-family": 1,
    "--max-marker": 1,
    "--pad": 1,
    "--pad-em": 1,
    "--min-cleanliness": 1,
    "--tol": 1,
    "--port-gap": 1,
}


def _sanitize_svg_audit_args(extra: list[str]) -> list[str]:
    """Validate caller-supplied auditor flags against the whitelist above.

    Raises ``ValueError`` with a human-readable reason on any rejection:
    unknown flags, positionals, missing values, or values that look like
    flags (which would shift every following token's meaning).
    """
    out: list[str] = []
    i = 0
    while i < len(extra):
        tok = extra[i]
        if not tok.startswith("-"):
            raise ValueError(
                f"positional argument {tok!r} is not allowed (tuning flags only)"
            )
        if tok not in _SVG_AUDIT_FLAGS:
            raise ValueError(
                f"flag {tok!r} is not allowed (allowed: {sorted(_SVG_AUDIT_FLAGS)})"
            )
        arity = _SVG_AUDIT_FLAGS[tok]
        if i + arity >= len(extra):
            raise ValueError(f"flag {tok!r} expects {arity} value(s)")
        out.append(tok)
        for j in range(1, arity + 1):
            val = extra[i + j]
            if val.startswith("-"):
                raise ValueError(f"flag {tok!r} expects a value, got {val!r}")
            out.append(val)
        i += arity + 1
    return out


def gate_svg(ctx: dict[str, Any]) -> GateOutcome:
    """Geometric SVG defect audit — the one gate dispatched as a subprocess.

    ``svg_audit.py`` is a standalone stdlib CLI (deliberately: it must stay runnable
    by hand, by a skill, and by this runner). It resolves via ``HERE`` so the runner
    is workdir-independent — the calling thread may reset cwd between invocations.

    Its CLI contract, which this gate depends on: ``--json <path>`` *writes* the
    report to that path (it is not a boolean flag), stdout is human prose, and the
    exit code is 0 ok / 1 defects / 2 usage. So the report is read from the file,
    never scraped from stdout.

    Extra auditor flags may be passed through ``ctx["svg_audit_args"]`` — but only
    from the closed whitelist in ``_SVG_AUDIT_FLAGS`` (e.g.
    ``["--min-font-px", "8"]`` for a figure whose type floor legitimately differs
    from the auditor's 20px publication default). ``--json`` and ``--selftest``
    are deliberately NOT forwardable: the first would retarget the report write,
    the second would bypass auditing entirely.

    The subprocess runs under ``ctx["svg_audit_timeout"]`` seconds (default 60):
    the auditor's text-collision check is O(n^2) in the number of text runs, so a
    pathological figure must never be able to wedge the calling request thread
    forever (review 2026-09-08 N1). A timeout is a red gate, not a skip.

    Fails on the first SVG whose report says ``ok != true``, that the auditor
    rejects outright, that produces no readable report, or that overruns the
    timeout. A missing ``svg_audit.py`` is a failure, not a skip.
    """
    name = "svg"
    figures = ctx.get("figures")
    if not figures:
        return _waived(name, "no figures in context")
    if not SVG_AUDIT.exists():
        return _failed(name, f"gate script not found: {SVG_AUDIT}")

    try:
        extra = _sanitize_svg_audit_args([str(a) for a in (ctx.get("svg_audit_args") or [])])
    except ValueError as exc:
        return _failed(name, f"svg_audit_args rejected: {exc}")
    try:
        timeout = float(ctx.get("svg_audit_timeout", 60.0))
    except (TypeError, ValueError):
        return _failed(name, "svg_audit_timeout must be a number of seconds")
    if not (timeout > 0):
        return _failed(name, "svg_audit_timeout must be > 0")

    reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="svg_gate_") as td:
        for spec in figures:
            p = Path(spec)
            if not p.is_absolute():
                p = (Path(ctx.get("workdir", ".")) / p).resolve()
            if not p.is_file():
                return _failed(name, f"figure not found: {p}", reports=reports)
            rep_path = Path(td) / f"{p.stem}.audit.json"
            try:
                proc = subprocess.run(
                    [sys.executable, str(SVG_AUDIT), str(p), "--json", str(rep_path), "--quiet", *extra],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return _failed(
                    name,
                    f"{p.name}: svg_audit exceeded {timeout:.0f}s and was killed — the figure "
                    f"is too complex to audit reliably (fail-closed)",
                    reports=reports,
                )
            if not rep_path.is_file():
                return _failed(
                    name,
                    f"{p.name}: auditor produced no report (exit={proc.returncode}): "
                    f"{((proc.stderr or proc.stdout) or '').strip()[:200]}",
                    reports=reports,
                )
            try:
                report = json.loads(rep_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return _failed(name, f"{p.name}: audit report unreadable: {exc}", reports=reports)
            codes = sorted({e.get("code", "?") for e in (report.get("errors") or [])})
            warn_codes = sorted({w.get("code", "?") for w in (report.get("warnings") or [])})
            reports.append(
                {
                    "figure": p.name,
                    "ok": bool(report.get("ok")),
                    "errors": codes,
                    "warnings": warn_codes,
                    "exit": proc.returncode,
                }
            )
            if not report.get("ok"):
                return _failed(
                    name,
                    f"{p.name}: SVG audit FAILED ({', '.join(codes) or 'unknown'}) — "
                    f"a figure with geometric defects is not shippable",
                    reports=reports,
                )
    return _passed(name, f"{len(reports)} figure(s) passed the geometric audit", reports=reports)


# ------------------------------------------------------------------- registries
GATES: dict[str, Callable[[dict[str, Any]], GateOutcome]] = {
    "novelty": gate_novelty,
    "metric_direction": gate_metric_direction,
    "claim_support": gate_claim_support,
    "svg": gate_svg,
}

# stage -> ordered gate names. Order matters: cheap/structural gates first, so a
# red gate short-circuits before anything expensive runs.
STAGE_GATES: dict[str, tuple[str, ...]] = {
    "evolution": ("novelty",),
    "records": ("metric_direction",),
    "audit": ("claim_support",),
    "figures": ("svg",),
    # Definition of Done: everything, in dependency order.
    "all": ("novelty", "metric_direction", "claim_support", "svg"),
}

# --- Phase 3 (opt-in, NOT in the default "all" loop) -------------------------
# The three deep gates below are ported from spark-to-paper's superior mechanisms
# (claim-evidence lint, number provenance, adversarial review). They are
# deliberately excluded from STAGE_GATES["all"] so the default path never triggers
# them; an operator opts in explicitly with stage="deep". They are pure heuristics
# over curated input, so they add zero runtime cost to the default loop.
from . import adversarial_review as _adversarial_review  # noqa: E402
from . import claims_lint as _claims_lint  # noqa: E402
from . import number_trace as _number_trace  # noqa: E402

GATES.update(
    {
        "claims_lint": _claims_lint.gate_claims_lint,
        "number_trace": _number_trace.gate_number_trace,
        "adversarial_review": _adversarial_review.gate_adversarial_review,
    }
)

# The opt-in deep battery: base claim support, then the three Phase 3 gates.
STAGE_GATES["deep"] = ("claim_support", "claims_lint", "number_trace", "adversarial_review")


def run_stage(stage: str, ctx: dict[str, Any]) -> tuple[int, list[GateOutcome]]:
    """Run one stage's gates in order; stop at the FIRST failing gate.

    Returns ``(exit_code, outcomes)``. ``outcomes`` holds every gate that actually
    ran, ending with the failure when there is one — so a caller can report *what*
    went red without re-running anything.
    """
    if stage not in STAGE_GATES:
        return GATE_USAGE, [
            GateOutcome(
                "runner",
                GATE_USAGE,
                STATUS_FAILED,
                f"unknown stage: {stage!r} (known: {sorted(STAGE_GATES)})",
            )
        ]
    outcomes: list[GateOutcome] = []
    for gate_name in STAGE_GATES[stage]:
        outcome = GATES[gate_name](ctx)
        outcomes.append(outcome)
        if not outcome.ok:
            return outcome.code, outcomes
    return GATE_OK, outcomes


def run_gates(stage: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Public in-process entry point. Returns a JSON-able report.

    ``{"ok": bool, "code": int, "stage": str, "gates": [...], "summary": str}``
    """
    code, outcomes = run_stage(stage, ctx)
    n_pass = sum(1 for o in outcomes if o.status == STATUS_PASSED)
    n_waived = sum(1 for o in outcomes if o.status == STATUS_WAIVED)
    n_fail = sum(1 for o in outcomes if o.status == STATUS_FAILED)
    if code == GATE_OK:
        summary = f"ALL GATES PASSED (stage={stage}): {n_pass} passed, {n_waived} waived"
    else:
        summary = f"GATE FAILED (stage={stage}): {outcomes[-1].name} — {outcomes[-1].detail}"
    return {
        "ok": code == GATE_OK,
        "code": code,
        "stage": stage,
        "gates": [o.to_dict() for o in outcomes],
        "counts": {"passed": n_pass, "waived": n_waived, "failed": n_fail},
        "summary": summary,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": GATE_USAGE,
                    "error": "usage: gate_runner.py <context.json> <stage|all>",
                    "stages": sorted(STAGE_GATES),
                },
                ensure_ascii=False,
            )
        )
        return GATE_USAGE
    ctx_path, stage = Path(args[0]), args[1]
    try:
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "code": GATE_USAGE, "error": f"unreadable context {ctx_path}: {exc}"},
                ensure_ascii=False,
            )
        )
        return GATE_USAGE
    if not isinstance(ctx, dict):
        print(
            json.dumps(
                {"ok": False, "code": GATE_USAGE, "error": "context JSON must be an object"},
                ensure_ascii=False,
            )
        )
        return GATE_USAGE
    ctx.setdefault("workdir", str(ctx_path.resolve().parent))
    report = run_gates(stage, ctx)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["code"])


if __name__ == "__main__":
    sys.exit(main())
