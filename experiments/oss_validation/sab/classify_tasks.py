"""Classify the 102 ScienceAgentBench tasks into the lightweight CSV/JSON subset.

We read the official annotation sheet (HuggingFace ``osunlp/ScienceAgentBench``)
and label every instance by its *target output format* and *subtask mix*:

* ``light_csv_json`` — target output is ``.csv`` or ``.json``, the subtask does NOT
  include "Deep Learning" (no neural-net training) and is NOT a visualization
  (figure/map/plot) task. These are the tasks the user called "CSV/JSON 轻量任务"
  and are the ones codex (wired to tmeoa) can plausibly solve and that run_eval can
  grade without a vision judge.
* everything else is bucketed as ``deep_learning`` / ``visualization`` / ``other``.

The full benchmark (datasets + gold programs + eval scripts) lives behind a
password-gated OSU SharePoint zip, so this classifier works from the annotation
sheet alone; the produced index is consumed by ``batch_sab_codex.py``.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import hf_hub_download

HERE = Path(__file__).resolve().parent

VISUAL_TOKENS = ("Visualization", "Map Visual", "Plot", "Figure")
DEEP_TOKENS = ("Deep Learning",)


def classify(out_fname: str, subtask: str) -> tuple[str, str]:
    ext = out_fname.rsplit(".", 1)[-1].lower() if "." in out_fname else ""
    is_csv_json = ext in ("csv", "json")
    is_visual = any(t in subtask for t in VISUAL_TOKENS) or ext in ("png", "jpg", "pdf")
    is_deep = any(t in subtask for t in DEEP_TOKENS)

    if is_visual:
        return "visualization", "figure/map output or visual subtask"
    if is_deep and not is_csv_json:
        return "deep_learning", "neural-net training, non-csv/json output"
    if is_deep and is_csv_json:
        return "deep_learning_csv", "neural-net training but csv/json output"
    if is_csv_json:
        return "light_csv_json", "csv/json output, no DL/visual subtask"
    return "other", f"output .{ext or 'none'}"


def main() -> None:
    csv_path = hf_hub_download(
        "osunlp/ScienceAgentBench", "ScienceAgentBench.csv", repo_type="dataset"
    )
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    index = []
    buckets: dict[str, list[int]] = {}
    for r in rows:
        cat, reason = classify(r["output_fname"], r["subtask_categories"])
        buckets.setdefault(cat, []).append(int(r["instance_id"]))
        index.append(
            {
                "instance_id": int(r["instance_id"]),
                "domain": r["domain"],
                "subtask_categories": r["subtask_categories"],
                "github_name": r["github_name"],
                "output_fname": r["output_fname"],
                "eval_script_name": r["eval_script_name"],
                "gold_program_name": r["gold_program_name"],
                "category": cat,
                "reason": reason,
            }
        )

    light = sorted(buckets.get("light_csv_json", []))
    already_done = {5, 92}
    remaining = [i for i in light if i not in already_done]

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "osunlp/ScienceAgentBench annotation sheet (HuggingFace)",
        "n_total": len(index),
        "buckets": {k: sorted(v) for k, v in buckets.items()},
        "light_csv_json": light,
        "already_validated": sorted(already_done & set(light)),
        "remaining_light_csv_json": remaining,
        "corpus_note": (
            "Full benchmark (datasets/gold/eval) is gated behind an OSU SharePoint "
            "zip (password: scienceagentbench). The annotation sheet alone yields the "
            "task list + classification; executing a task needs its curated data dir."
        ),
        "tasks": index,
    }
    out = HERE / "sab_task_index.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    print(f"total tasks: {len(index)}")
    for k, v in buckets.items():
        print(f"  {k:18s}: {len(v):3d}  {v}")
    print(f"light_csv_json total : {len(light)}")
    print(f"already validated   : {sorted(already_done & set(light))}")
    print(f"REMAINING to verify : {len(remaining)} -> {remaining}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
