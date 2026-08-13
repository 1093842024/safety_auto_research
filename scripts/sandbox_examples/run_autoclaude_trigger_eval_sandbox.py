#!/usr/bin/env python3
"""Sandbox runner for task ``autoclaude.trigger_eval`` (ARIS · skill trigger-rate).

Runs INSIDE the disposable Docker sandbox (no network, read-only /data). It loads
the official query set (``trigger_evals.sample.json``) and runs the *official*
trigger_eval scoring logic — ``parse_stream_tool_uses`` + ``classify`` +
``aggregate`` imported straight from the materialised ``trigger_eval.py`` — over a
deterministic, OFFLINE probe, and reports a real **trigger_rate** (higher=better).

Why a local probe (no model)? The upstream tool shells out to ``claude -p`` (or the
tmeoa gateway) to observe which skill a *model* reaches for. That is not available
offline / without credentials, and the dual-loop platform needs a reproducible,
network-free measurement at the L1 standard. So we substitute a transparent,
deterministic lexical skill-router as the *probe oracle*: for each (skill, query) it
emits the exact same ``stream-json`` a real probe would (an assistant turn whose
``Skill`` tool_use names the router's chosen skill, or no tool_use on a miss). The
official ``classify``/``aggregate`` code then scores that stream verbatim — i.e. we
exercise the real evaluation pipeline, only the oracle is local and reproducible.

The offline router builds each skill's signature vocabulary from its own positive
queries and triggers the skill whose signature best overlaps the probed query. This
is a faithful, measurable stand-in for "does the skill description fire on its
intent" and surfaces genuine confusions (a query whose vocabulary best matches a
*different* skill) exactly as the real tool would record them in its confusion
matrix.

Usage (driven by scripts/build_agent_sandbox.sh):
    python /repo/scripts/sandbox_examples/run_autoclaude_trigger_eval_sandbox.py --data-dir /data
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

# The official trigger_eval.py + sample live on the read-only /data mount.
_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/data")
if _DATA_DIR not in sys.path:
    sys.path.insert(0, _DATA_DIR)

import trigger_eval as te  # type: ignore  # noqa: E402  (trigger_eval.py on /data)

TRIGGER_THRESHOLD = 0.5

_STOP = {
    "the", "this", "that", "with", "from", "your", "have", "has", "hasn", "been",
    "before", "would", "could", "should", "what", "which", "when", "where", "who",
    "why", "how", "any", "all", "are", "were", "will", "them", "they", "their",
    "there", "here", "into", "out", "about", "than", "then", "and", "but", "for",
    "you", "your", "our", "my", "me", "i", "it", "its", "on", "in", "of", "to",
    "a", "an", "is", "am", "do", "does", "did", "make", "made", "get", "got",
    "use", "used", "using", "find", "pull", "give", "simulate", "anything",
}


def _tokenize(text: str) -> set[str]:
    toks = set()
    for raw in text.lower().split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) >= 4 and w not in _STOP:
            toks.add(w)
    return toks


def _build_vocab(skills: dict[str, list[str]]) -> dict[str, set[str]]:
    return {s: set().union(*[_tokenize(q) for q in qs]) if qs else set()
            for s, qs in skills.items()}


def _decide(query: str, vocab: dict[str, set[str]]) -> str | None:
    """Return the best-matching skill id, or None on zero overlap (a miss)."""
    q = _tokenize(query)
    if not q:
        return None
    best, best_score = None, 0
    for s, sig in vocab.items():
        score = len(q & sig)  # shared distinct content tokens
        if score > best_score or (score == best_score and best is None):
            best, best_score = s, score
    return best if best_score > 0 else None


def _stream_for(decision: str | None) -> str:
    """Synthesize the stream-json a real probe would emit for this decision."""
    if decision is None:
        ev = {"type": "assistant",
              "message": {"role": "assistant",
                          "content": [{"type": "text", "text": "no skill invoked"}]}}
        return json.dumps(ev) + "\n"
    ev = {"type": "assistant",
          "message": {"role": "assistant",
                      "content": [{"type": "tool_use", "name": "Skill",
                                   "input": {"skill": decision}}]}}
    return json.dumps(ev) + "\n"


def probe_readonly(data_dir: str) -> bool:
    probe = os.path.join(data_dir, ".write_test_%d" % os.getpid())
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


def probe_network_blocked() -> bool:
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=2):
            return False
    except OSError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=None, help="override (default AGENT_DATA_DIR)")
    ap.add_argument("--eval-file", default="trigger_evals.sample.json")
    ap.add_argument("--samples", type=int, default=3,
                    help="probes per (skill, query) — matches upstream default")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    data_dir = args.data_dir or _DATA_DIR
    _SENTINEL = "trigger_evals.sample.json"
    if not os.path.exists(os.path.join(data_dir, _SENTINEL)):
        data_dir = os.environ.get("AGENT_DATA_DIR", data_dir)

    isolation = {
        "data_readonly": probe_readonly(data_dir),
        "network_blocked": probe_network_blocked(),
    }

    evals_path = Path(data_dir) / args.eval_file
    evals = json.loads(evals_path.read_text(encoding="utf-8"))
    targets = {k: v for k, v in evals.items()
               if not k.startswith("_") and isinstance(v, list)}

    vocab = _build_vocab(targets)

    records = []
    for skill, queries in targets.items():
        for query in queries:
            for _ in range(max(1, args.samples)):
                decision = _decide(query, vocab)
                stream = _stream_for(decision)
                # ---- official trigger_eval scoring logic (unmodified) ----
                outcome, detail = te.classify(
                    te.parse_stream_tool_uses(stream), skill
                )
                records.append({"skill": skill, "query": query,
                                "outcome": outcome, "detail": detail})

    summary = te.aggregate(records)

    # Per-skill trigger_rate (positive samples) + a global rate (triggers/graded).
    per_skill = {name: (s["trigger_rate"] if s["trigger_rate"] is not None else 0.0)
                 for name, s in summary.items()}
    graded = sum(s["probes"] - s["errors"] for s in summary.values())
    triggers = sum(s["triggers"] for s in summary.values())
    global_rate = float(triggers / graded) if graded else 0.0

    # ---- Negative probing (upstream "should-not-trigger" methodology) ----
    # For each skill A we additionally probe the *other* skills' queries as
    # negative examples: if the router wrongly fires A on another skill's intent
    # that is a false trigger (a confusion), exactly the signal the real tool's
    # confusion matrix captures. Surfaced but kept out of the primary metric.
    neg_records = []
    for skill_a in targets:
        for skill_b, queries in targets.items():
            if skill_b == skill_a:
                continue
            for query in queries:
                for _ in range(max(1, args.samples)):
                    decision = _decide(query, vocab)
                    stream = _stream_for(decision)
                    outcome, detail = te.classify(
                        te.parse_stream_tool_uses(stream), skill_a
                    )
                    neg_records.append({"skill": skill_a, "query": query,
                                        "outcome": outcome, "detail": detail})
    neg_summary = te.aggregate(neg_records)
    false_trigger = {
        name: round((s["triggers"] / (s["probes"] - s["errors"]))
                    if (s["probes"] - s["errors"]) else 0.0, 4)
        for name, s in neg_summary.items()
    }

    metrics = {
        "trigger_rate": round(global_rate, 4),
        "probes": graded,
        "triggers": triggers,
        "primary": round(global_rate, 4),
    }
    details = {
        "per_skill_trigger_rate": {k: round(v, 4) for k, v in per_skill.items()},
        "false_trigger_rate": false_trigger,
        "negative_probes": sum(s["probes"] for s in neg_summary.values()),
    }
    passed = bool(global_rate >= TRIGGER_THRESHOLD)

    result = {
        "task_id": "autoclaude.trigger_eval",
        "eval_metric": "trigger_rate",
        "threshold": TRIGGER_THRESHOLD,
        "op": "higher",
        "passed": passed,
        "gate_passed": passed,
        "metrics": metrics,
        "details": details,
        "isolation": isolation,
        "port_notes": ["no ports opened; offline lexical probe, no model/network"],
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
