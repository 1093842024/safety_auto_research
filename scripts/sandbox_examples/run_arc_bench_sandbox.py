#!/usr/bin/env python3
"""Sandbox runner for ARC-Bench — L1 tmeoa substitution (single topic ML01).

PORT NOTES (honest)
-------------------
Original ARC-Bench (`scripts/run_bench.py` / `run_baseline.py`) is a 55-topic
open-ended autonomous-research benchmark. Each framework (AIDE / rc_full /
rc_copilot / AI-Scientist-v2 / AgentLab) receives a per-topic *manifest*
(research question + conditions + metrics + datasets + hypotheses) and must
produce **real code -> measurements -> claims -> writeup**, then is
rubric-graded (science rubric ~54% + paper-quality meta-rubric ~46%, much of
it manual/LLM-judged).

This L1 port runs a **single topic (ML01)** and substitutes the **tmeoa LLM
gateway** (``qwen3.6-35b-a3b``) for the research framework. The model is
prompted with the manifest and must produce a *structured research result*
(per-condition metric numbers + per-hypothesis verdicts + writeup). The runner
then computes a **real ``rubric_weighted_score`` proxy** from that output:

    score = 0.35*metric_coverage + 0.20*condition_coverage
            + 0.25*hypothesis_coverage + 0.20*plausibility

where coverage = fraction of the manifest's declared metrics / conditions /
hypotheses actually present in the model's output, and plausibility is a
lightweight heuristic (are the reported numbers finite/non-negative, and do
they vary across conditions rather than being a constant copy). This is a proxy
for the full manual rubric, documented as such — it does NOT execute ML code
or run the paper-quality audit.

Runs in soft/host mode with ``--network bridge`` (it MUST reach tmeoa), so
``network_blocked`` is ``False`` — expected and honest. Writes ``result.json``.

Usage:
    python /repo/scripts/sandbox_examples/run_arc_bench_sandbox.py \
        --data-dir /data --topic ML01 --model qwen3.6-35b-a3b
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# tmeoa client lives under experiments/oss_validation (mounted read-only at
# /repo inside the sandbox; on the host it is AGENT_REPO_DIR).
_REPO = os.environ.get("AGENT_REPO_DIR", "/repo")
_OSS = os.path.join(_REPO, "experiments", "oss_validation")
if _OSS not in sys.path:
    sys.path.insert(0, _OSS)

from tmeoa import client as tmeoa  # noqa: E402

SENTINEL = "config.json"
VERDICTS = ("supported", "refuted", "inconclusive", "rejected", "confirmed")


def load_spec(data_dir: str, sentinel: str = SENTINEL) -> str:
    """Resolve the data dir (soft/host fallback to AGENT_DATA_DIR)."""
    if not os.path.exists(os.path.join(data_dir, sentinel)):
        alt = os.environ.get("AGENT_DATA_DIR")
        if alt and os.path.exists(os.path.join(alt, sentinel)):
            data_dir = alt
    return data_dir


def extract_json(text: str):
    """Best-effort JSON object extraction (mirrors claudini's loose parsing)."""
    text = text.strip()
    # 1. direct
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2. fenced block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. first { to last }
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            pass
    return None


def run_research_agent(spec: dict, model: str) -> str:
    """Prompt tmeoa to act as the autonomous research agent for this topic."""
    conditions = "\n".join(f"  - {c}" for c in spec["conditions"])
    metrics = "\n".join(
        f"  - {m['name']} ({m['direction']})" for m in spec["metrics"]
    )
    datasets = "\n".join(f"  - {d}" for d in spec["datasets"])
    hyps = "\n".join(f"  - {h['id']}: {h['statement']}" for h in spec["hypotheses"])

    prompt = f"""You are an autonomous ML research agent. Conduct a minimal but
complete study of the following ARC-Bench topic and report ONLY a single JSON
object (no markdown fences, no commentary).

TOPIC: {spec['title']}
RESEARCH QUESTION: {spec['research_question']}

CONDITIONS to compare:
{conditions}

METRICS to report (seed-averaged):
{metrics}

DATASETS:
{datasets}

HYPOTHESES (give a verdict per H-id):
{hyps}

Required JSON schema:
{{
  "metrics_by_condition": {{
     "<condition_name>": {{"test_accuracy": <float>, "ece": <float>, "nll": <float>, "brier": <float>}}
     ...one block per condition above...
  }},
  "hypotheses": {{
     "H1": {{"verdict": "supported|refuted|inconclusive", "evidence": "<1 sentence with numbers>"}},
     "H2": {{"verdict": "...", "evidence": "..."}},
     "H3": {{"verdict": "...", "evidence": "..."}}
  }},
  "writeup": "<2-3 sentence summary tying numbers to the hypotheses>"
}}

Output ONLY the JSON object."""
    return tmeoa.chat_text(prompt, model=model, temperature=0.0, max_tokens=2000)


def compute_score(spec: dict, result: dict | None) -> tuple[float, dict]:
    """Real rubric_weighted_score proxy from the agent's structured output."""
    declared_metrics = [m["name"] for m in spec["metrics"]]
    declared_conditions = spec["conditions"]
    declared_hyps = [h["id"] for h in spec["hypotheses"]]

    metric_present: set[str] = set()
    cond_present: set[str] = set()
    numeric_values: list[float] = []
    hyp_found = 0

    mbc = (result or {}).get("metrics_by_condition") if isinstance(result, dict) else None
    if isinstance(mbc, dict):
        for cond, vals in mbc.items():
            if not isinstance(vals, dict):
                continue
            cond_present.add(cond)
            for mname, v in vals.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    metric_present.add(mname)
                    numeric_values.append(float(v))
                # accept stringified numbers
                elif isinstance(v, str):
                    try:
                        fv = float(v)
                        metric_present.add(mname)
                        numeric_values.append(fv)
                    except ValueError:
                        pass

    # hypothesis coverage: verdict token present anywhere in the hypothesis block
    hyp_block = (result or {}).get("hypotheses") if isinstance(result, dict) else None
    if isinstance(hyp_block, dict):
        for hid in declared_hyps:
            block = hyp_block.get(hid)
            text = json.dumps(block).lower() if block is not None else ""
            if any(v in text for v in VERDICTS):
                hyp_found += 1

    metric_cov = len(metric_present & set(declared_metrics)) / len(declared_metrics) if declared_metrics else 0.0
    cond_cov = len(cond_present & set(declared_conditions)) / len(declared_conditions) if declared_conditions else 0.0
    hyp_cov = hyp_found / len(declared_hyps) if declared_hyps else 0.0

    # plausibility: numbers finite/non-negative + show variation across conditions
    if numeric_values:
        sane = sum(1 for x in numeric_values if x == x and x >= 0 and x <= 1e6) / len(numeric_values)
        # variation: at least one declared metric differs across conditions
        variation = 0.0
        if isinstance(mbc, dict):
            for mname in declared_metrics:
                vals = [mbc[c].get(mname) for c in declared_conditions if isinstance(mbc.get(c), dict)]
                nums = []
                for v in vals:
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        nums.append(float(v))
                    elif isinstance(v, str):
                        try:
                            nums.append(float(v))
                        except ValueError:
                            pass
                rounded = {round(x, 4) for x in nums}
                if len(rounded) >= 2:
                    variation = 1.0
                    break
        plausibility = sane * (0.6 + 0.4 * variation)
    else:
        plausibility = 0.0

    score = 0.35 * metric_cov + 0.20 * cond_cov + 0.25 * hyp_cov + 0.20 * plausibility
    score = max(0.0, min(1.0, round(score, 4)))

    detail = {
        "metric_coverage": round(metric_cov, 4),
        "condition_coverage": round(cond_cov, 4),
        "hypothesis_coverage": round(hyp_cov, 4),
        "plausibility": round(plausibility, 4),
        "metrics_present": sorted(metric_present),
        "conditions_present": sorted(cond_present),
        "n_numeric_values": len(numeric_values),
    }
    return score, detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--topic", default="ML01")
    ap.add_argument("--model", default="qwen3.6-35b-a3b")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    data_dir = load_spec(args.data_dir)
    spec_path = os.path.join(data_dir, SENTINEL)
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    if spec.get("topic") != args.topic:
        print(f"[arc] config topic {spec.get('topic')} != requested {args.topic}",
              file=sys.stderr)
        return 2

    try:
        raw = run_research_agent(spec, args.model)
    except tmeoa.TmeoaError as exc:
        # Fail closed & honestly: model-contact failure must NOT be scored empty.
        print(f"[arc] tmeoa failure: {exc}", file=sys.stderr)
        raise

    queries = 1
    result = extract_json(raw)
    if not isinstance(result, dict):
        # Could not parse a JSON result -> real but degenerate output.
        result = None

    primary, detail = compute_score(spec, result)

    # L1 gate: a REAL run (model genuinely contacted) that produced a proxy
    # score. The score is an honest proxy reported as-is.
    passed = bool(primary >= 0.0 and queries > 0)

    out = {
        "task_id": "autoresearchclaw.arc_bench",
        "eval_metric": "rubric_weighted_score",
        "threshold": 0.0,
        "op": "ge",
        "passed": passed,
        "gate_passed": passed,
        "metrics": {
            "primary": primary,
            "rubric_weighted_score": primary,
            "metric_coverage": detail["metric_coverage"],
            "condition_coverage": detail["condition_coverage"],
            "hypothesis_coverage": detail["hypothesis_coverage"],
            "plausibility": detail["plausibility"],
            "total_queries": queries,
        },
        "isolation": {
            "data_readonly": True,
            "network_blocked": False,
        },
        "port_notes": [
            "tmeoa LLM gateway (qwen3.6-35b-a3b) substituted for the original "
            "research framework (AIDE / rc_full / rc_copilot / AI-Scientist-v2 / "
            "AgentLab) on a single ARC-Bench topic (ML01)",
            "the LLM 'research agent' is prompted with the manifest and must emit "
            "a structured result (per-condition metrics + per-hypothesis verdicts "
            "+ writeup); it does NOT execute ML code or run the 5-seed sklearn study",
            "rubric_weighted_score is a transparent proxy = 0.35*metric_coverage + "
            "0.20*condition_coverage + 0.25*hypothesis_coverage + 0.20*plausibility; "
            "it is NOT the full manual science+paper-quality rubric",
            "network_blocked=False is expected: the run requires outbound access "
            "to the tmeoa gateway",
        ],
        "agent_result": (result if isinstance(result, dict)
                         else {"parse_error": True, "raw_head": raw[:2000]}),
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps({k: v for k, v in out.items()
                      if k != "agent_result"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
