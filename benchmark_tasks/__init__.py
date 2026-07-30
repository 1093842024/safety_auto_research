"""Curated benchmark / baseline research-task catalog for safety_auto_research.

The catalog is mined from the open-source projects living under the parent
repository (autolab, claudini, Arbor, AutoResearchClaw, Agent-Native-Research-
Artifact, Auto-claude-code-research-in-sleep, MLEvolve) plus the two Kaggle
competitions the dual-loop platform already supports end-to-end.

Each entry carries the dataset description and the exact evaluation metric
(metric / direction / baseline / reference anchors / pass gates) so the control
plane can surface it in the frontend as a *selectable research task* and (for
platform-native tasks) actually drive the dual loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Absolute root of the safety_auto_research package (one level up from this file).
import os as _os

_PKG_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_OSS_ROOT = _os.path.dirname(_PKG_ROOT)  # /Users/glennge/work/github/AI_research


@dataclass
class BenchmarkTask:
    task_id: str
    name: str
    source_project: str
    category: str  # model_dev | system_opt | puzzle | cuda | adversarial | efficiency | agent_eval | idea_eval | tooling | platform_native
    modality: str
    dataset_desc: str
    eval_metric: str
    direction: str  # lower | higher
    baseline: float | None
    reference: float | None
    gates: dict[str, Any]
    harness: str  # autolab_harness | claudini | arbor | arc_bench | manual | kaggle_eval
    run_command: str
    source_path: str
    tags: list[str]
    supported_by_platform: bool = False  # True => dual-loop can actually execute it
    note: str = ""
    # How the task is evaluated / scored (script or procedure). Kept as core info so an
    # agent executor (Task 4) knows the evaluation method after docker/Arbor deps are stripped.
    eval_method: str = ""
    # ---- custom-registered tasks only (empty for curated entries) ----
    task_type: str = ""  # e.g. tabular_classification / llm_sft / llm_opd ...
    type_config: dict[str, Any] | None = None  # raw registration form values


def _oss(p: str) -> str:
    """Resolve a path that may be expressed relative to the OSS parent root."""
    return p if _os.path.isabs(p) else _os.path.join(_OSS_ROOT, p)


# --------------------------------------------------------------------------- #
# Curated catalog (real tasks with clear data + evaluation, mined from the    #
# open-source projects under AI_research/).                                    #
# --------------------------------------------------------------------------- #
_TASKS: list[BenchmarkTask] = [
    # ---------------- autolab (standardized task.toml live benchmark) -------- #
    BenchmarkTask(
        task_id="autolab.safety_router",
        name="Smallest Safety Router",
        source_project="autolab",
        category="puzzle",
        modality="tabular",
        dataset_desc="Fixed numeric features for a refusal router (answer=0 / refuse=1); "
        "train/val/test_public .npz splits. Architecture is a 2-layer MLP (model.py hash-pinned, read-only).",
        eval_metric="total_params",
        direction="lower",
        baseline=16641,
        reference=2081,
        gates={"accuracy>=": 0.64, "unsafe_recall>=": 0.66, "safe_recall>=": 0.57},
        harness="autolab_harness",
        run_command="harbor run -p tasks/safety_router   # or: cd environment && bash run_baseline.sh",
        source_path=_oss("autolab/tasks/safety_router"),
        tags=["safety", "mlp", "model-compression", "classification"],
        note="Star task used by the evaluation-benchmark runbook (Harbor sandbox).",
    ),
    BenchmarkTask(
        task_id="autolab.grpo_multisource",
        name="GRPO Multi-Source",
        source_project="autolab",
        category="model_dev",
        modality="text",
        dataset_desc="Multi-source reasoning corpus; agent must improve a GRPO-trained model's "
        "multimodal math reasoning without a retention gate regression.",
        eval_metric="mathvista_accuracy",
        direction="higher",
        baseline=0.20,
        reference=0.65,
        gates={"retention_gate": "no regression"},
        harness="autolab_harness",
        run_command="harbor run -p tasks/grpo_multisource",
        source_path=_oss("autolab/tasks/grpo_multisource"),
        tags=["grpo", "math", "alignment", "reasoning"],
    ),
    BenchmarkTask(
        task_id="autolab.flash_attention",
        name="Flash Attention",
        source_project="autolab",
        category="system_opt",
        modality="kernel",
        dataset_desc="Reference attention kernel; agent optimizes a CUDA/CPU attention implementation "
        "for wall-clock latency on fixed benchmark shapes.",
        eval_metric="runtime_seconds",
        direction="lower",
        baseline=0.75,
        reference=0.10,
        gates={},
        harness="autolab_harness",
        run_command="harbor run -p tasks/flash_attention",
        source_path=_oss("autolab/tasks/flash_attention"),
        tags=["cuda", "kernel", "attention", "latency"],
    ),
    BenchmarkTask(
        task_id="autolab.aes128_ctr",
        name="AES-128 CTR",
        source_project="autolab",
        category="system_opt",
        modality="kernel",
        dataset_desc="Reference AES-128 CTR implementation; agent must optimize throughput.",
        eval_metric="runtime_seconds",
        direction="lower",
        baseline=3.0,
        reference=0.10,
        gates={},
        harness="autolab_harness",
        run_command="harbor run -p tasks/aes128_ctr",
        source_path=_oss("autolab/tasks/aes128_ctr"),
        tags=["crypto", "kernel", "throughput"],
    ),
    BenchmarkTask(
        task_id="autolab.adaptive_compression",
        name="Adaptive Compression",
        source_project="autolab",
        category="puzzle",
        modality="sequence",
        dataset_desc="Byte-level sequence compression; agent improves a context-modeling compressor "
        "toward a PPM-style reference.",
        eval_metric="bits_per_byte",
        direction="lower",
        baseline=5.0,
        reference=3.8,
        gates={},
        harness="autolab_harness",
        run_command="harbor run -p tasks/adaptive_compression",
        source_path=_oss("autolab/tasks/adaptive_compression"),
        tags=["compression", "sequence", "information-theory"],
    ),
    BenchmarkTask(
        task_id="autolab.ntt_butterfly_cuda",
        name="NTT Butterfly (CUDA)",
        source_project="autolab",
        category="cuda",
        modality="kernel",
        dataset_desc="Number-theoretic transform butterfly kernel; agent optimizes GPU latency.",
        eval_metric="runtime_ms",
        direction="lower",
        baseline=109.8,
        reference=1.28,
        gates={},
        harness="autolab_harness",
        run_command="harbor run -p tasks/ntt_butterfly_cuda",
        source_path=_oss("autolab/tasks/ntt_butterfly_cuda"),
        tags=["cuda", "ntt", "kernel", "latency"],
    ),
    BenchmarkTask(
        task_id="autolab.llm_online_serving",
        name="LLM Online Serving",
        source_project="autolab",
        category="model_dev",
        modality="serving",
        dataset_desc="Online LLM serving stack; agent improves a composite serving score under load.",
        eval_metric="serving_score",
        direction="higher",
        baseline=1.0,
        reference=1.5,
        gates={},
        harness="autolab_harness",
        run_command="harbor run -p tasks/llm_online_serving",
        source_path=_oss("autolab/tasks/llm_online_serving"),
        tags=["serving", "llm", "systems"],
    ),
    # ---------------- claudini (adversarial / jailbreak benchmark) ---------- #
    BenchmarkTask(
        task_id="claudini.random",
        name="Claudini · Random-target suffix attack",
        source_project="claudini",
        category="adversarial",
        modality="text",
        dataset_desc="White-box discrete suffix adversarial attack (GCG-style) on synthetic random-ASCII "
        "target sequences. 15-20 samples; metric = suffix generation loss + ASR.",
        eval_metric="loss / ASR",
        direction="lower",  # loss lower is better; ASR higher is better
        baseline=None,
        reference=None,
        gates={"max_flops": "1e17"},
        harness="claudini",
        run_command="python claudini/run_bench.py --config configs/random_train.yaml",
        source_path=_oss("claudini/configs/random_train.yaml"),
        tags=["jailbreak", "gcg", "suffix-attack", "adversarial"],
        note="Primary ranking metric is cross-entropy loss (lower=better); ASR reported alongside.",
    ),
    BenchmarkTask(
        task_id="claudini.injection",
        name="Claudini · Prompt-injection attack",
        source_project="claudini",
        category="adversarial",
        modality="text",
        dataset_desc="Prompt-injection attack on AlpacaFarm prompts (50 samples) targeting the word "
        "'Hacked'; models Meta-SecAlign-70B / 8B.",
        eval_metric="loss / ASR",
        direction="lower",
        baseline=None,
        reference=None,
        gates={"max_flops": "3e17"},
        harness="claudini",
        run_command="python claudini/run_bench.py --config configs/injection_8b.yaml",
        source_path=_oss("claudini/configs/injection_8b.yaml"),
        tags=["prompt-injection", "jailbreak", "adversarial", "safety"],
    ),
    BenchmarkTask(
        task_id="claudini.safeguard",
        name="Claudini · Safeguard-bypass attack",
        source_project="claudini",
        category="adversarial",
        modality="text",
        dataset_desc="Safeguard-bypass attack on ClearHarm (40 samples) targeting the refusal response; "
        "model gpt-oss-safeguard-20b.",
        eval_metric="loss / ASR",
        direction="lower",
        baseline=None,
        reference=None,
        gates={"max_flops": "1e18"},
        harness="claudini",
        run_command="python claudini/run_bench.py --config configs/safeguard_train.yaml",
        source_path=_oss("claudini/configs/safeguard_train.yaml"),
        tags=["safeguard", "jailbreak", "adversarial", "safety"],
        note="Quality red line: ASR must be reported together with defender-side safety degradation.",
    ),
    # ---------------- Arbor (efficiency benchmark) ------------------------- #
    BenchmarkTask(
        task_id="arbor.algotune_knn",
        name="Arbor · AlgoTune kNN speedup",
        source_project="Arbor",
        category="efficiency",
        modality="tabular",
        dataset_desc="k-nearest-neighbour (Euclidean) brute-force; dev/test on disjoint random-seed "
        "ranges (dev 1000+ / test 9000+). Solution must pass a correctness gate on every instance.",
        eval_metric="speedup",
        direction="higher",
        baseline=1.0,
        reference=None,
        gates={"correctness": "must pass on all instances (else score=0.0)"},
        harness="arbor",
        run_command="arbor benchmark verify arbor-zoo/algotune_knn",
        source_path=_oss("Arbor/arbor-zoo/algotune_knn"),
        tags=["knn", "efficiency", "speedup", "cpu"],
    ),
    # ---------------- AutoResearchClaw (agent research benchmark) ----------- #
    BenchmarkTask(
        task_id="autoresearchclaw.arc_bench",
        name="ARC-Bench · 55-topic open research",
        source_project="AutoResearchClaw",
        category="agent_eval",
        modality="mixed",
        dataset_desc="55 open research topics across ML(25)/HEP(10)/quantum(10)/biology(7)/statistics(3); "
        "each topic a manifest with research question + metrics + datasets. Rubric-weighted score "
        "(~54% science + 46% paper-quality).",
        eval_metric="rubric_weighted_score",
        direction="higher",
        baseline=None,
        reference=None,
        gates={"metrics_verified": "declared metric keys must validate"},
        harness="arc_bench",
        run_command="python experiments/arc_bench/scripts/run_bench.py --mode rc_full --topic ML01",
        source_path=_oss("AutoResearchClaw/experiments/arc_bench"),
        tags=["agent-eval", "research-agent", "rubric", "cross-domain"],
        note="Compares frameworks AIDE / AI-Scientist-v2 / AgentLab / rc_full / rc_copilot as baselines.",
    ),
    # ---------------- Agent-Native-Research-Artifact ----------------------- #
    BenchmarkTask(
        task_id="ara.understanding",
        name="ARA · Artifact understanding eval",
        source_project="Agent-Native-Research-Artifact",
        category="agent_eval",
        modality="text",
        dataset_desc="Papers + per-paper questions (catA/B/C) with gold answers; measures an agent's "
        "ability to understand/reproduce/extend a research artifact vs a PDF+repo baseline.",
        eval_metric="absolute_correctness_success_rate",
        direction="higher",
        baseline=None,
        reference=None,
        gates={},
        harness="manual",
        run_command="python docs/the-ara-of-ara/src/eval/run_understanding_eval.py all",
        source_path=_oss("Agent-Native-Research-Artifact/docs/the-ara-of-ara/src/eval"),
        tags=["artifact", "understanding", "eval", "mcnemar"],
    ),
    # ---------------- Auto-claude-code-research-in-sleep -------------------- #
    BenchmarkTask(
        task_id="autoclaude.trigger_eval",
        name="ARIS · Skill trigger-rate eval",
        source_project="Auto-claude-code-research-in-sleep",
        category="tooling",
        modality="text",
        dataset_desc="JSON of {skill: [queries]} with positive + negative (should-not-trigger) samples; "
        "measures whether skill descriptions are correctly triggered by user intent.",
        eval_metric="trigger_rate",
        direction="higher",
        baseline=None,
        reference=None,
        gates={},
        harness="manual",
        run_command="python3 tools/meta_opt/trigger_eval.py --eval-file tools/meta_opt/trigger_evals.sample.json",
        source_path=_oss("Auto-claude-code-research-in-sleep/tools/meta_opt/trigger_eval.py"),
        tags=["skill-trigger", "meta-opt", "tooling", "eval"],
    ),
    # ---------------- MLEvolve (external MLE-bench, 75 tasks) --------------- #
    BenchmarkTask(
        task_id="mlevolve.mle_bench",
        name="MLE-bench · 75 Kaggle tasks (external)",
        source_project="MLEvolve",
        category="model_dev",
        modality="tabular",
        dataset_desc="75 Kaggle-style ML competitions (accuracy / ROC-AUC / F1 per task); aggregated "
        "metric = medal rate. Data lives in external openai/mle-bench (not bundled in this repo).",
        eval_metric="medal_rate",
        direction="higher",
        baseline=None,
        reference=None,
        gates={},
        harness="manual",
        run_command="bash engine/coldstart/run_single_task.sh <EXP_ID> <DATASET_DIR>",
        source_path=_oss("MLEvolve"),
        tags=["mle-bench", "kaggle", "ml", "external-dependency"],
        note="External dependency: requires local mle-bench dataset_dir; repo provides only the harness.",
    ),
    # ---------------- External suites (Weng harness appendix, tracked) ----- #
    BenchmarkTask(
        task_id="suite.science_agent_bench",
        name="ScienceAgentBench · 102 data-driven discovery tasks",
        source_project="OSU-NLP-Group/ScienceAgentBench",
        category="agent_eval",
        modality="mixed",
        dataset_desc="102 tasks from 44 peer-reviewed publications in 4 disciplines "
        "(Bioinformatics 27 / Comp. Chemistry 20 / GIS 27 / Psych&CogSci 28); covers data "
        "processing, model development, data analysis, visualization. Target output = "
        "self-contained Python program. Full manifest bundled (suites/data/); datasets + eval "
        "programs fetched externally (HF annotation sheet + SharePoint zip).",
        eval_metric="success_rate",
        direction="higher",
        baseline=32.4,  # Claude-3.5-Sonnet self-debug, w/o expert knowledge
        reference=42.2,  # o1-preview self-debug (>10x cost)
        gates={"attempts": 3, "output": "self-contained Python program passing eval script"},
        harness="manual",
        run_command="GET /benchmark-suites/science_agent_bench/tasks  (manifest); "
        "python agent.py --framework self_debug  (in SAB repo)",
        source_path=_os.path.join(_PKG_ROOT, "benchmark_tasks", "suites", "data",
                                  "science_agent_bench_tasks.json"),
        tags=["science-discovery", "agent-eval", "code-generation", "weng-harness", "suite"],
        note="Suite integrated with full 102-task manifest + paper Table 3 baselines "
        "(SR/CBS/VER/cost). See /benchmark-suites/science_agent_bench.",
        eval_method="每任务生成自包含 Python 程序，跑通并通过任务专属 eval 脚本判定成功（SR，3 次尝试）；"
        "辅以 VER（可执行率）、CodeBERTScore、API cost；可视化输出由 GPT-4o judge 评分。",
    ),
    BenchmarkTask(
        task_id="suite.mle_bench",
        name="MLE-bench · 75 offline Kaggle competitions (official suite)",
        source_project="openai/mle-bench",
        category="model_dev",
        modality="mixed",
        dataset_desc="75 Kaggle ML-engineering competitions (low 22 / medium 38 / high 15; lite=low, "
        "158 GB vs full 3.3 TB). Train models, prepare data, run experiments, submit CSV to "
        "grading scripts; Kaggle public leaderboard = human baseline. Full competition manifest "
        "bundled incl. official leakage flags; raw data via Kaggle API (mlebench prepare).",
        eval_metric="any_medal_percentage",
        direction="higher",
        baseline=16.9,  # o1-preview + AIDE (paper headline, Weng post)
        reference=64.44,  # leaderboard top (Famou-Agent 2.0, 2026-02)
        gates={"medal": ">= Kaggle bronze per competition", "seeds": ">=3, mean±SEM"},
        harness="manual",
        run_command="GET /benchmark-suites/mle_bench/tasks  (manifest); "
        "mlebench prepare --lite && mlebench grade ...  (in mle-bench repo)",
        source_path=_os.path.join(_PKG_ROOT, "benchmark_tasks", "suites", "data",
                                  "mle_bench_competitions.json"),
        tags=["kaggle", "mle-bench", "ml-engineering", "weng-harness", "suite"],
        note="Suite integrated with 75-competition manifest + leaderboard baselines + "
        "resource-scaling & contamination analyses. Complements mlevolve.mle_bench (local "
        "harness) and platform.titanic/spaceship (dual-loop-executable toy equivalents). "
        "See /benchmark-suites/mle_bench.",
        eval_method="每竞赛以 CSV 提交至官方 grade.py，按真实 Kaggle 排行榜奖牌线判定是否≥铜牌；"
        "汇总指标 any_medal_percentage（按 low/medium/high/split75 分档，≥3 seeds 取均值±SEM）。",
    ),
    # ---------------- Platform-native (dual-loop can actually run) --------- #
    BenchmarkTask(
        task_id="platform.titanic",
        name="Kaggle · Titanic (binary classification)",
        source_project="safety_auto_research",
        category="platform_native",
        modality="tabular",
        dataset_desc="Titanic survival classification; train.csv / test.csv. Drives the inner research "
        "loop (gbm baseline) -> outer audit -> recursive improvement.",
        eval_metric="cv_accuracy",
        direction="higher",
        baseline=0.78,
        reference=None,
        gates={"threshold": 0.82},
        harness="kaggle_eval",
        run_command="POST /benchmark-tasks/platform.titanic/launch  (runs dual loop)",
        source_path=_PKG_ROOT + "/data/kaggle/titanic",
        tags=["kaggle", "classification", "dual-loop"],
        supported_by_platform=True,
        note="Fully executable by the dual-loop platform (inner=kaggle_eval gbm, 5-fold CV).",
    ),
    BenchmarkTask(
        task_id="platform.spaceship",
        name="Kaggle · Spaceship-Titanic (binary classification)",
        source_project="safety_auto_research",
        category="platform_native",
        modality="tabular",
        dataset_desc="Spaceship-Titanic passenger transport classification; train.csv / test.csv. "
        "End-to-end dual-loop demo task.",
        eval_metric="cv_accuracy",
        direction="higher",
        baseline=0.79,
        reference=None,
        gates={"threshold": 0.80},
        harness="kaggle_eval",
        run_command="POST /benchmark-tasks/platform.spaceship/launch  (runs dual loop)",
        source_path=_PKG_ROOT + "/data/kaggle/spaceship-titanic",
        tags=["kaggle", "classification", "dual-loop"],
        supported_by_platform=True,
        note="Fully executable by the dual-loop platform (the demo task used in validation).",
    ),
    BenchmarkTask(
        task_id="platform.wine",
        name="Wine Quality (binary classification)",
        source_project="safety_auto_research",
        category="platform_native",
        modality="tabular",
        dataset_desc="Wine chemical analysis → binary quality (sklearn load_wine, flavanoids threshold). 178 rows x 14 features.",
        eval_metric="f1",
        direction="higher",
        baseline=0.72,
        reference=None,
        gates={"threshold": 0.78},
        harness="kaggle_eval",
        run_command="POST /benchmark-tasks/platform.wine/launch  (runs dual loop)",
        source_path=_PKG_ROOT + "/data/kaggle/wine",
        tags=["sklearn", "classification", "dual-loop", "wine"],
        supported_by_platform=True,
        note="Generated from sklearn load_wine. Binary target from flavanoids median split.",
    ),
    BenchmarkTask(
        task_id="platform.iris",
        name="Iris Species (multi-class)",
        source_project="safety_auto_research",
        category="platform_native",
        modality="tabular",
        dataset_desc="Fisher Iris flower species: 3 classes, 150 rows x 4 features. Classic ML benchmark.",
        eval_metric="accuracy",
        direction="higher",
        baseline=0.90,
        reference=None,
        gates={"threshold": 0.92},
        harness="kaggle_eval",
        run_command="POST /benchmark-tasks/platform.iris/launch  (runs dual loop)",
        source_path=_PKG_ROOT + "/data/kaggle/iris",
        tags=["sklearn", "classification", "multi-class", "dual-loop"],
        supported_by_platform=True,
        note="Generated from sklearn load_iris. 3 balanced classes, strong baseline with GBM.",
    ),
    BenchmarkTask(
        task_id="platform.breast_cancer",
        name="Breast Cancer Wisconsin (binary classification)",
        source_project="safety_auto_research",
        category="platform_native",
        modality="tabular",
        dataset_desc="Breast cancer diagnosis from 30 numeric features. 569 rows, binary target. sklearn load_breast_cancer.",
        eval_metric="accuracy",
        direction="higher",
        baseline=0.92,
        reference=None,
        gates={"threshold": 0.94},
        harness="kaggle_eval",
        run_command="POST /benchmark-tasks/platform.breast_cancer/launch  (runs dual loop)",
        source_path=_PKG_ROOT + "/data/kaggle/breast_cancer",
        tags=["sklearn", "classification", "medical", "dual-loop"],
        supported_by_platform=True,
        note="Generated from sklearn load_breast_cancer. 30 numeric features, strong GBM baseline >0.95.",
    ),
]


# ---- external suite access (ScienceAgentBench / MLE-bench) --------------- #
def get_suites():
    """List integrated external benchmark suites (see suites/)."""
    from . import suites

    return suites.list_suites()


def get_suite(suite_id: str):
    from . import suites

    return suites.get_suite(suite_id)


def _custom_tasks() -> list[BenchmarkTask]:
    """Custom-registered tasks (see registry.py), converted to BenchmarkTask."""
    from . import registry

    out: list[BenchmarkTask] = []
    for rec in registry.list_custom_tasks():
        d = registry.custom_task_to_benchmark_dict(rec)
        out.append(BenchmarkTask(**d))
    return out


def get_catalog() -> list[BenchmarkTask]:
    return list(_TASKS) + _custom_tasks()


def get_task(task_id: str) -> BenchmarkTask | None:
    for t in get_catalog():
        if t.task_id == task_id:
            return t
    return None


def _default_eval_method(t: BenchmarkTask) -> str:
    """Compose a concise evaluation-method description from core fields (fallback)."""
    direction = "越高越好" if t.direction == "higher" else "越低越好"
    gate = t.gates if t.gates else "无"
    if t.harness == "kaggle_eval" or t.supported_by_platform:
        return (
            f"以 {t.eval_metric} 为指标（{direction}），通过门限 {gate}；"
            f"基线 baseline={t.baseline}，参考 reference={t.reference}。"
            "平台内 kaggle_eval 执行交叉验证（默认 5 折）。"
        )
    return (
        f"以 {t.eval_metric} 为指标（{direction}），门限 {gate}；"
        f"基线 baseline={t.baseline}，参考 reference={t.reference}。"
        f"原始评估由 {t.harness} 在外部环境中完成，现改为 agent 模式执行（已剥离 docker/Arbor 依赖）。"
    )


def to_dict(t: BenchmarkTask) -> dict[str, Any]:
    eval_method = t.eval_method or _default_eval_method(t)
    execution_mode = "platform" if (t.harness == "kaggle_eval" or t.supported_by_platform) else "agent"
    direction = "越高越好" if t.direction == "higher" else "越低越好"
    goal = (
        f"优化指标 {t.eval_metric}（{direction}），baseline={t.baseline}"
        + (f"，reference={t.reference}" if t.reference is not None else "")
        + f"；{t.name}"
    )
    return {
        "task_id": t.task_id,
        "name": t.name,
        "source_project": t.source_project,
        "category": t.category,
        "modality": t.modality,
        "dataset_desc": t.dataset_desc,
        "eval_metric": t.eval_metric,
        "direction": t.direction,
        "baseline": t.baseline,
        "reference": t.reference,
        "gates": t.gates,
        "harness": t.harness,
        "execution_mode": execution_mode,
        "eval_method": eval_method,
        "goal": goal,
        "run_command": t.run_command,
        "source_path": t.source_path,
        "tags": t.tags,
        "supported_by_platform": t.supported_by_platform,
        "note": t.note,
        "task_type": t.task_type,
        "type_config": t.type_config,
    }


CATEGORY_LABELS = {
    "model_dev": "模型开发",
    "system_opt": "系统优化",
    "puzzle": "谜题/挑战",
    "cuda": "CUDA 内核",
    "adversarial": "对抗/越狱",
    "efficiency": "效率基准",
    "agent_eval": "科研 Agent 评测",
    "idea_eval": "想法质量评测",
    "tooling": "工具型元评测",
    "platform_native": "平台原生(可实跑)",
    "custom": "自定义注册任务",
}
