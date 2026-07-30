"""MLE-bench suite integration (OpenAI, arXiv:2410.07095).

75 offline Kaggle ML-engineering competitions. Agents must train models,
prepare datasets, run experiments and submit predictions (CSV) to per-
competition grading scripts; the Kaggle public leaderboard provides the
human baseline. Headline metric = any_medal_percentage (% of competitions
where the agent reaches at least Kaggle bronze).

Bundled manifest (``data/mle_bench_competitions.json``) lists all 75
competitions with complexity split (low 22 / medium 38 / high 15), the
lite-split category + dataset size, and the officially acknowledged
leakage / preparation issues (contamination analysis). Raw competition
data (3.3 TB full / 158 GB lite) is fetched externally via the Kaggle API.
"""

from __future__ import annotations

from . import BaselineResult, BenchmarkSuite

_B = BaselineResult
_PAPER = "MLE-bench paper (arXiv:2410.07095)"
_LB = "openai/mle-bench leaderboard"

# Official baselines. Paper headline: o1-preview + AIDE achieved at least a
# Kaggle bronze medal in 16.9% of competitions (the number cited in Weng's
# harness post); the maintained leaderboard reports 17.12 +/- 0.61 for the
# same setup. extra = per-split any_medal_percentage + runtime hours.
BASELINES: list[BaselineResult] = [
    _B("AIDE", "o1-preview", "any_medal_percentage", 16.9,
       {"leaderboard_all": 17.12, "sem": 0.61, "low": 35.91, "medium": 8.45, "high": 11.67,
        "hours": 24},
       f"{_PAPER}; leaderboard 17.12±0.61", "2024-10-08", is_headline=True),
    _B("AIDE", "gpt-4o-2024-08-06", "any_medal_percentage", 8.63,
       {"low": 18.55, "medium": 3.06, "high": 8.15, "hours": 24}, _LB, "2024-10-08"),
    _B("AIDE", "claude-3-5-sonnet-20240620", "any_medal_percentage", 7.56,
       {"low": 19.70, "medium": 2.63, "high": 2.22, "hours": 24}, _LB, "2024-10-08"),
    _B("OpenHands", "gpt-4o-2024-08-06", "any_medal_percentage", 4.89,
       {"low": 12.12, "medium": 1.75, "high": 2.22, "hours": 24}, _LB, "2024-10-08"),
    _B("AIDE", "llama-3.1-405b-instruct", "any_medal_percentage", 3.33,
       {"low": 10.23, "medium": 0.66, "high": 0.0, "hours": 24}, _LB, "2024-10-08"),
    _B("MLAB", "gpt-4o-2024-08-06", "any_medal_percentage", 1.60,
       {"low": 4.55, "medium": 0.0, "high": 0.0, "hours": 24}, _LB, "2024-10-08"),
    # ---- later leaderboard entries (context for current SOTA) ----
    _B("R&D-Agent", "o1-preview", "any_medal_percentage", 22.40,
       {"low": 48.18, "medium": 8.95, "high": 18.67, "hours": 24}, _LB, "2025-05-14"),
    _B("MLEvolve", "Gemini-3-Pro-Preview", "any_medal_percentage", 61.33,
       {"low": 80.30, "medium": 57.89, "high": 42.22, "hours": 12}, _LB, "2026-02-14"),
    _B("Famou-Agent 2.0", "Gemini-3-Pro-Preview", "any_medal_percentage", 64.44,
       {"low": 80.3, "medium": 64.04, "high": 42.22, "hours": 24}, _LB, "2026-02-23"),
]

SUITE = BenchmarkSuite(
    suite_id="mle_bench",
    name="MLE-bench · 65 usable / 75 published Kaggle ML-engineering competitions",
    paper="arXiv:2410.07095 (OpenAI, ICLR 2025)",
    homepage="https://github.com/openai/mle-bench",
    description=(
        "Evaluates ML-engineering agents on 75 competitions curated from Kaggle: train models, "
        "prepare datasets, run experiments, and submit predictions to grading scripts. Kaggle "
        "public leaderboards serve as the human baseline; the headline metric is the percentage "
        "of competitions where the agent earns at least a bronze medal (any_medal_percentage). "
        "10 competitions with official Known-Issues (label/test-set leakage or preparation-script "
        "defects) are excluded from the default task list, leaving 65 usable competitions."
    ),
    task_count=65,
    official_task_count=75,
    headline_metric="any_medal_percentage",
    direction="higher",
    disciplines={"low": 19, "medium": 32, "high": 14},
    evaluation={
        "any_medal_percentage": (
            "% of competitions reaching >= Kaggle bronze; per-competition grade.py compares the "
            "CSV submission against medal thresholds derived from the real leaderboard"
        ),
        "grading": "mlebench grade --submission <jsonl> (fields: competition_id, submission_path)",
        "aggregation": (
            "experiments/aggregate_grading_reports.py per split (low/medium/high/split75); "
            "report mean ± SEM over >= 3 seeds"
        ),
        "human_baseline": "Kaggle public leaderboard medal thresholds",
    },
    data_acquisition={
        "install": "git clone https://github.com/openai/mle-bench && pip install -e . (git-lfs required)",
        "credentials": "Kaggle API token at ~/.kaggle/kaggle.json",
        "prepare_all": "mlebench prepare --all   # 75 comps, ~3.3 TB, ~2 days",
        "prepare_lite": "mlebench prepare --lite  # low split, 22 comps, ~158 GB",
        "prepare_one": "mlebench prepare -c <competition-id>",
        "grade_sample": "mlebench grade-sample <submission.csv> <competition-id>",
    },
    analyses={
        "resource_scaling": {
            "reference_hardware": "24h runtime, 36 vCPU, 440 GB RAM, 1x A10 24GB GPU (recommended)",
            "protocol": ">=3 seeds, report any_medal_percentage mean ± SEM per split",
            "finding": "paper §3.3/3.4: performance scales with attempts (pass@k) and runtime budget",
        },
        "contamination": {
            "method": (
                "paper checks GPT-4o familiarity with competition discussions and finds no "
                "significant correlation between contamination signals and performance; "
                "plagiarism detection run on agent code vs top Kaggle notebooks"
            ),
            "known_leakage": (
                "officially tracked in README Known Issues. We apply an exclusion policy: the 11 "
                "competitions with label/test-set leakage or preparation-script defects are flagged "
                "excluded in the manifest (10 are in our 75-competition catalog: dog-breed-identification, "
                "random-acts-of-pizza, ranzcr-clip-catheter-line-classification, champs-scalar-coupling, "
                "hubmap-kidney-segmentation, icecube-neutrinos-in-deep-ice, multi-modal-gesture-recognition, "
                "tensorflow-speech-recognition-challenge, tensorflow2-question-answering, smartphone-decimeter-2022; "
                "invasive-species-monitoring is not in the 75-split catalog). The 3 crowded-leaderboard "
                "competitions (tabular-playground-series-dec-2021, tabular-playground-series-may-2022, "
                "jigsaw-toxic-comment-classification-challenge) are RETAINED but flagged noisy_leaderboard. "
                "Fix targeted in MLE-bench v2."
            ),
        },
    },
    license="MIT (repo LICENSE); competition data subject to Kaggle terms.",
    manifest_file="mle_bench_competitions.json",
    baselines=BASELINES,
    note=(
        "Cited in Lilian Weng's harness post: best setup o1-preview + AIDE scaffolding reaches "
        "bronze-or-better in 16.9% of competitions. Related local harness: MLEvolve/ (task "
        "mlevolve.mle_bench) provides a runner but not the data; platform.titanic/spaceship are "
        "the same task-format at toy scale and ARE executable by the dual loop."
    ),
)
