"""ScienceAgentBench suite integration (OSU-NLP-Group, arXiv:2410.05080).

102 data-driven scientific-discovery tasks extracted from 44 peer-reviewed
publications across 4 disciplines. Target output of every task is a
self-contained Python program; evaluation runs the program and checks the
result with a per-task eval script (plus GPT-4o visual judge for figures).

Bundled manifest (``data/science_agent_bench_tasks.json``) is the public HF
annotation sheet (verified split, 2026-04-30) with the fields each agent
needs: task_inst / dataset_folder_tree / gold_program_name / eval_script_name
(long preview fields truncated). The actual datasets + eval/gold programs are
NOT redistributed here — fetch them via ``data_acquisition``.
"""

from __future__ import annotations

from . import BaselineResult, BenchmarkSuite

# Official results — paper Table 3 (SR = success rate %, 3 attempts/task).
# "wk" suffix = with expert-provided knowledge. CBS = CodeBERTScore,
# VER = valid execution rate, cost = avg USD/task.
_B = BaselineResult
_SRC = "ScienceAgentBench paper Table 3 (arXiv:2410.05080)"
BASELINES: list[BaselineResult] = [
    # ---- self-debug framework (strongest per-cost) ----
    _B("self-debug", "OpenAI o1-preview", "success_rate", 42.2,
       {"sr_with_knowledge": 41.2, "cbs": 88.4, "ver": 92.2, "cost_usd": 0.636},
       _SRC, "2024-10-24", is_headline=True),
    _B("self-debug", "Claude-3.5-Sonnet (2024-06-20)", "success_rate", 32.4,
       {"sr_with_knowledge": 34.3, "cbs": 86.4, "ver": 92.2, "cost_usd": 0.057}, _SRC, "2024-10-07"),
    _B("self-debug", "Mistral-Large-2 (2407)", "success_rate", 23.5,
       {"sr_with_knowledge": 27.5, "cbs": 85.1, "ver": 83.3, "cost_usd": 0.034}, _SRC, "2024-10-07"),
    _B("self-debug", "GPT-4o (2024-05-13)", "success_rate", 22.6,
       {"sr_with_knowledge": 23.5, "cbs": 84.4, "ver": 83.3, "cost_usd": 0.047}, _SRC, "2024-10-07"),
    _B("self-debug", "Llama-3.1-Instruct-405B", "success_rate", 14.7,
       {"sr_with_knowledge": 13.7, "cbs": 82.9, "ver": 78.4, "cost_usd": 0.047}, _SRC, "2024-10-07"),
    _B("self-debug", "Llama-3.1-Instruct-70B", "success_rate", 13.7,
       {"sr_with_knowledge": 16.7, "cbs": 82.7, "ver": 80.4, "cost_usd": 0.007}, _SRC, "2024-10-07"),
    # ---- OpenHands CodeAct ----
    _B("OpenHands CodeAct", "Claude-3.5-Sonnet (2024-06-20)", "success_rate", 21.6,
       {"sr_with_knowledge": 24.5, "cbs": 83.6, "ver": 87.3, "cost_usd": 0.958}, _SRC, "2024-10-07"),
    _B("OpenHands CodeAct", "GPT-4o (2024-05-13)", "success_rate", 19.6,
       {"sr_with_knowledge": 27.5, "cbs": 83.1, "ver": 78.4, "cost_usd": 0.803}, _SRC, "2024-10-07"),
    _B("OpenHands CodeAct", "Mistral-Large-2 (2407)", "success_rate", 9.8,
       {"sr_with_knowledge": 13.7, "cbs": 72.5, "ver": 53.9, "cost_usd": 0.513}, _SRC, "2024-10-07"),
    # ---- direct prompting ----
    _B("direct prompting", "OpenAI o1-preview", "success_rate", 34.3,
       {"sr_with_knowledge": 31.4, "cbs": 87.1, "ver": 70.6, "cost_usd": 0.221}, _SRC, "2024-10-24"),
    _B("direct prompting", "Claude-3.5-Sonnet (2024-06-20)", "success_rate", 17.7,
       {"sr_with_knowledge": 21.6, "cbs": 83.6, "ver": 51.0, "cost_usd": 0.017}, _SRC, "2024-10-07"),
    _B("direct prompting", "GPT-4o (2024-05-13)", "success_rate", 11.8,
       {"sr_with_knowledge": 10.8, "cbs": 82.6, "ver": 52.9, "cost_usd": 0.011}, _SRC, "2024-10-07"),
]

SUITE = BenchmarkSuite(
    suite_id="science_agent_bench",
    name="ScienceAgentBench · 102 data-driven discovery tasks",
    paper="arXiv:2410.05080 (ICLR 2025)",
    homepage="https://github.com/OSU-NLP-Group/ScienceAgentBench",
    description=(
        "Evaluates LLM agents on data-driven scientific discovery: 102 tasks extracted from "
        "44 peer-reviewed publications in four disciplines, validated by nine subject-matter "
        "experts. Covers essential data-science steps — data processing, model development, "
        "data analysis, information visualization. Every task's target output is a "
        "self-contained Python program graded by a per-task eval script."
    ),
    task_count=102,
    headline_metric="success_rate",
    direction="higher",
    disciplines={
        "Bioinformatics": 27,
        "Computational Chemistry": 20,
        "Geographical Information Science": 27,
        "Psychology and Cognitive science": 28,
    },
    evaluation={
        "success_rate": "SR — program runs and output passes the task's eval script (3 attempts/task)",
        "valid_execution_rate": "VER — program executes without error and saves the required output file",
        "codebert_score": "CBS — CodeBERTScore F1 of generated vs annotated gold program",
        "api_cost": "average USD cost per task",
        "visual_judge": "GPT-4o judge for visualization outputs (gpt4_visual_judge.py)",
    },
    data_acquisition={
        "annotation_sheet_hf": "osunlp/ScienceAgentBench (split=verified; public inputs only)",
        "full_benchmark": (
            "SharePoint zip linked from the GitHub README (benchmark/: datasets/, eval_programs/, "
            "gold_programs/, scoring_rubrics/); unzip password: scienceagentbench; "
            "verified fixes in benchmark_verified.zip (2026-04-30). Redistribution prohibited."
        ),
        "fetch_manifest_cmd": (
            "python -m safety_auto_research.benchmark_tasks.suites.science_agent_bench --refresh-manifest"
        ),
        "run_agent_cmd": "python agent.py --model <name> --framework self_debug  (in the SAB repo)",
        "eval_cmd": "python calculate_metrics.py  (in the SAB repo, after recover_pred_from_log.py)",
    },
    analyses={
        "contamination_mitigation": (
            "Two strategies from the paper: (1) test datasets/labels are modified vs the public "
            "originals so memorized shortcuts fail; (2) tasks are adapted (not copied) from "
            "published code. The 2026-04-30 'verified' split further fixes false negatives."
        ),
        "annotation_effort": "≥2.5-3h per task for a trained annotator; agents draft in ~10min.",
    },
    license="Tasks CC BY 4.0 (exceptions: rasterio-derived #32/46/53/54/84, matminer #3); code MIT.",
    manifest_file="science_agent_bench_tasks.json",
    baselines=BASELINES,
    note=(
        "Best agent solves only 32.4% independently / 34.3% with expert knowledge "
        "(Claude-3.5-Sonnet self-debug); o1-preview self-debug reaches 42.2% at >10x cost. "
        "Cited in Lilian Weng's harness post (2026-07) as a useful auto-research benchmark."
    ),
)


def refresh_manifest(out_path: str | None = None) -> int:
    """Re-fetch the 102-task annotation sheet from the HF datasets-server."""
    import json
    import os
    import urllib.request

    rows: list[dict] = []
    for off in range(0, 102, 34):
        url = (
            "https://datasets-server.huggingface.co/rows?dataset=osunlp%2FScienceAgentBench"
            f"&config=default&split=verified&offset={off}&length=34"
        )
        with urllib.request.urlopen(url, timeout=60) as r:
            rows += [x["row"] for x in json.load(r)["rows"]]
    keep = [
        {
            "instance_id": r["instance_id"],
            "domain": r["domain"],
            "subtask_categories": r["subtask_categories"],
            "github_name": r["github_name"],
            "task_inst": r["task_inst"],
            "domain_knowledge": (r.get("domain_knowledge") or "")[:600],
            "dataset_folder_tree": (r.get("dataset_folder_tree") or "")[:400],
            "src_file_or_path": r["src_file_or_path"],
            "gold_program_name": r["gold_program_name"],
            "output_fname": r["output_fname"],
            "eval_script_name": r["eval_script_name"],
        }
        for r in rows
    ]
    if out_path is None:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", SUITE.manifest_file)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, ensure_ascii=False, indent=1)
    return len(keep)


if __name__ == "__main__":
    import sys

    if "--refresh-manifest" in sys.argv:
        print(f"refreshed {refresh_manifest()} tasks")
