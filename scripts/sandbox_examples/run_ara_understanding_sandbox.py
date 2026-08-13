#!/usr/bin/env python3
"""Sandbox runner for the ARA *understanding* layer — L1 tmeoa substitution.

PORT NOTES (honest)
-------------------
Original `run_understanding_eval.py` (Agent-Native-Research-Artifact):
  * Step 2 — a Claude-Code sub-agent *explores the paper PDF + ARA artifact*
            (Read/Grep/Glob over a large code/artifact tree) and answers every
            question; a second PDF-baseline sub-agent answers from the paper.
  * Step 3 — an Opus *judge* scores each answer for absolute correctness vs the
            gold answer (semantic, 1-5 rubric), producing
            ``absolute_correctness_success_rate``.
  * The paper PDFs / ARA artifacts are multi-MB trees that are NOT bundled in
    this port (they are not even present in the sibling repo).

This L1 port substitutes the **tmeoa LLM gateway** (``qwen3.6-35b-a3b``) for
the multi-agent exploration pipeline. Because no paper text is supplied, this
is a **closed-book** port: the model answers each question from parametric
knowledge only. Scoring is a **transparent contains-check / rubric proxy**
against gold *key tokens* (numbers + significant method/entity terms) — it is
NOT the Opus semantic judge. The primary metric is still reported under the
canonical name ``absolute_correctness_success_rate`` so the platform wiring is
identical; the score is a defensible proxy and is documented as such.

Runs in soft/host mode with ``--network bridge`` (it MUST reach tmeoa), so
``network_blocked`` is ``False`` — expected and honest. Writes ``result.json``.

Usage:
    python /repo/scripts/sandbox_examples/run_ara_understanding_sandbox.py \
        --data-dir /data --sample 5 --model qwen3.6-35b-a3b
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# tmeoa client lives under experiments/oss_validation (mounted read-only at
# /repo inside the sandbox; on the host it is AGENT_REPO_DIR).
_REPO = os.environ.get("AGENT_REPO_DIR", "/repo")
_OSS = os.path.join(_REPO, "experiments", "oss_validation")
if _OSS not in sys.path:
    sys.path.insert(0, _OSS)

from tmeoa import client as tmeoa  # noqa: E402

SENTINEL = "manifest.json"
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "has", "have", "per", "but", "not", "our", "its", "their", "than", "into",
    "over", "both", "each", "more", "less", "two", "one", "all", "why", "how",
    "what", "when", "where", "which", "who", "they", "them", "then", "also",
    "use", "uses", "used", "using", "via", "such", "these", "those", "between",
    "while", "because", "however", "therefore", "thus", "may", "can", "will",
    "would", "should", "does", "done", "new", "first", "second", "third",
    "given", "based", "other", "another", "same", "different", "best", "better",
    "worst", "high", "higher", "low", "lower", "large", "small", "non",
}


def load_spec(data_dir: str, sentinel: str = SENTINEL) -> str:
    """Resolve the data dir (soft/host fallback to AGENT_DATA_DIR)."""
    if not os.path.exists(os.path.join(data_dir, sentinel)):
        alt = os.environ.get("AGENT_DATA_DIR")
        if alt and os.path.exists(os.path.join(alt, sentinel)):
            data_dir = alt
    return data_dir


def _num_re() -> re.Pattern:
    return re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?")


def parse_num(s: str):
    s = s.replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def gold_tokens(gold: str):
    """Return (numeric_literals, significant_word_tokens) from a gold answer."""
    nums = _num_re().findall(gold)
    words: set[str] = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", gold):
        if len(w) < 3:
            continue
        is_sig = (
            w.isupper()
            or (w[0].isupper() and sum(1 for c in w if c.isupper()) >= 1
                and any(c.islower() for c in w))
            or any(ch.isdigit() for ch in w)
        )
        if is_sig:
            words.add(w)
    words = {w for w in words if w.lower() not in STOPWORDS}
    return nums, words


def score_question(gold: str, answer: str) -> float:
    """Transparent contains-check/rubric proxy in [0, 1].

    Combines a numeric-match score (gold numbers present in the answer, within
    1% tolerance) and a lexical score (gold method/entity terms present as
    case-insensitive substrings). Numbers and words are weighted equally when
    both are present; otherwise the available signal is used.
    """
    g_nums, g_words = gold_tokens(gold)
    a_nums = [v for v in (parse_num(x) for x in _num_re().findall(answer)) if v is not None]
    a_low = answer.lower()

    g_num_vals = [v for v in (parse_num(n) for n in g_nums) if v is not None]
    num_score = 0.0
    if g_num_vals:
        matches = 0
        for gv in g_num_vals:
            if gv == 0:
                if any(abs(av) <= 0.01 for av in a_nums):
                    matches += 1
            elif any(abs(gv - av) <= max(0.01 * abs(gv), 0.01) for av in a_nums):
                matches += 1
        num_score = matches / len(g_num_vals)

    word_score = 0.0
    if g_words:
        matches = sum(1 for w in g_words if w.lower() in a_low)
        word_score = matches / len(g_words)

    if g_num_vals and g_words:
        return round(0.5 * num_score + 0.5 * word_score, 4)
    if g_num_vals:
        return round(num_score, 4)
    return round(word_score, 4)


def build_questions(data_dir: str, sample: int) -> list[dict]:
    """Flatten materialised questions, interleave categories, take first `sample`."""
    registry_path = os.path.join(data_dir, "paper_registry.json")
    with open(registry_path, encoding="utf-8") as fh:
        registry = json.load(fh)

    qdir = os.path.join(data_dir, "questions")
    flat: list[dict] = []
    for paper in registry["papers"]:
        pid = paper["paper_id"]
        for cat in paper.get("categories", []):
            path = os.path.join(qdir, f"{pid}_cat{cat}.json")
            if not os.path.exists(path):
                continue
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for q in data.get("questions", []):
                q = dict(q)
                q["_paper_id"] = pid
                q["_category"] = cat
                flat.append(q)

    # Interleave so a small sample still spans A/B/C (stable by category then id).
    order = {"A": 0, "B": 1, "C": 2}
    flat.sort(key=lambda q: (order.get(q.get("_category", "A"), 9), q.get("id", "")))
    return flat[:sample] if sample and sample > 0 else flat


def answer_question(q: dict, model: str) -> str:
    pid = q.get("_paper_id", "unknown")
    cat = q.get("_category", "")
    prompt = (
        f"You are a senior ML researcher. Answer the following question about the "
        f"research paper/artifact '{pid}' (understanding category {cat}).\n\n"
        f"Question: {q['question']}\n\n"
        "Answer concisely and factually using your own knowledge. If the artifact "
        "or paper is not something you recognise, say so honestly rather than "
        "guessing. Do NOT reveal your reasoning — output only the answer."
    )
    return tmeoa.chat_text(prompt, model=model, temperature=0.0, max_tokens=800)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--sample", type=int, default=5,
                    help="number of questions to evaluate (across catA/B/C)")
    ap.add_argument("--model", default="qwen3.6-35b-a3b")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    data_dir = load_spec(args.data_dir)
    questions = build_questions(data_dir, args.sample)
    if not questions:
        print("[ara] no questions materialised", file=sys.stderr)
        return 2

    per_q: list[dict] = []
    queries = 0
    for q in questions:
        try:
            ans = answer_question(q, args.model)
            queries += 1
        except tmeoa.TmeoaError as exc:
            # Fail closed & honestly: a model-contact failure must NOT be
            # silently scored as an empty run.
            print(f"[ara] tmeoa failure on {q.get('id')}: {exc}", file=sys.stderr)
            raise

        score = score_question(q.get("gold_answer", ""), ans)
        per_q.append({
            "id": q.get("id"),
            "paper_id": q.get("_paper_id"),
            "category": q.get("_category"),
            "score": score,
            "success": bool(score >= 0.5),
            "answer": ans[:1000],
        })
        print(f"[ara] {q.get('id')}: score={score:.3f} success={score >= 0.5}")

    n = len(per_q)
    n_correct = sum(1 for r in per_q if r["success"])
    primary = round(n_correct / n, 4)
    mean_overlap = round(sum(r["score"] for r in per_q) / n, 4)

    # L1 gate: a REAL run (model genuinely contacted, queries>0) that produced a
    # non-negative rate. The rate itself is an honest proxy and is reported as-is.
    passed = bool(primary >= 0.0 and queries > 0)

    result = {
        "task_id": "ara.understanding",
        "eval_metric": "absolute_correctness_success_rate",
        "threshold": 0.0,
        "op": "ge",
        "passed": passed,
        "gate_passed": passed,
        "metrics": {
            "primary": primary,
            "absolute_correctness_success_rate": primary,
            "n_questions": n,
            "n_correct": n_correct,
            "mean_combined_overlap": mean_overlap,
            "total_queries": queries,
        },
        "isolation": {
            "data_readonly": True,
            # This task MUST reach tmeoa, so network is deliberately open here.
            "network_blocked": False,
        },
        "port_notes": [
            "tmeoa LLM gateway (qwen3.6-35b-a3b) substituted for the original "
            "Claude-Code multi-agent paper/artifact exploration pipeline",
            "closed-book port: paper PDFs / ARA artifacts are NOT bundled, so the "
            "model answers from parametric knowledge only",
            "scoring is a transparent contains-check/rubric proxy against gold key "
            "tokens (numbers + significant method/entity terms), NOT the Opus "
            "semantic judge; success = per-question overlap >= 0.5",
            "metric name kept canonical (absolute_correctness_success_rate) so the "
            "platform wiring is unchanged; the score is a defensible proxy",
            "network_blocked=False is expected: the run requires outbound access "
            "to the tmeoa gateway",
        ],
        "per_question": per_q,
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "per_question"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
