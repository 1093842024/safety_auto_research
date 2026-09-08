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

import shutil
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
    # ---- availability / gray-out (data-size policy) ----
    # ``data_size_bytes`` is the *lower bound* on train+eval data volume. ``None`` means
    # "unknown / external" (treated as not-locally-completable). Tasks whose data exceeds
    # the 1 GiB policy, or that depend on external repos / model weights too large to host
    # locally, are grayed out (``enabled=False``) and excluded from training-optimization.
    data_size_bytes: int | None = None
    enabled: bool = True
    unavailable_reason: str = ""
    # True => the task's data + official scripts have been materialised locally
    # (data/oss/<task_id>/) and a sandbox runner exists, so it is launchable /
    # experimentally validatable even though its source project is an external repo.
    data_local: bool = False


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
        category="model_dev",
        modality="tabular",
        dataset_desc="Fixed numeric features for a refusal router (answer=0 / refuse=1); "
        "train/val/test_public .npz splits. Architecture is a 2-layer MLP (model.py hash-pinned, read-only).",
        eval_metric="total_params",
        direction="lower",
        baseline=16641,
        reference=2081,
        gates={"accuracy>=": 0.64, "unsafe_recall>=": 0.66, "safe_recall>=": 0.57},
        harness="autolab_harness",
        run_command="python /repo/scripts/sandbox_examples/run_safety_router_sandbox.py --data-dir /data/",
        source_path=_oss("autolab/tasks/safety_router"),
        tags=["safety", "mlp", "model-compression", "classification"],
        data_local=True,
        eval_method=(
            "平台研究方式：数据已软连接/物化到 data/oss/autolab.safety_router/（含官方 model.py/"
            "train.py/evaluate_local.py 与 .npz 数据），由 run_safety_router_sandbox.py 在 Docker 沙箱内"
            "训练 2 层 MLP 并在 test_public.npz 上评分；指标为 accuracy/unsafe_recall/safe_recall/"
            "total_params，门限 accuracy>=0.64 & unsafe_recall>=0.66 & safe_recall>=0.57。双循环可由 "
            "agent 搜索 hidden_dim/epochs 以最小化 total_params 同时保住门限。"
        ),
        note=(
            "Harbor/Arbor 训练类代表任务（已接通平台）：数据 + 官方脚本已物化到 data/oss/，并由 "
            "scripts/sandbox_examples/run_safety_router_sandbox.py 在沙箱内执行，L1 单次 launch 即产出真实指标。"
        ),
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
    # ---------------- OSS task representatives (wired into the platform) -------- #
    # These are the *sample-first* (样板优先) representatives per the user's
    # directive. Each has its data + official scripts materialised under
    # data/oss/<task_id>/ and a sandbox runner under scripts/sandbox_examples/,
    # so a single launch produces a real metric (result.json + EvalCompletedEvent)
    # at the L1 standard. The remaining tasks in each category replicate this pattern
    # (see the batch-wired entries appended via _oss_wired_extra below).
    BenchmarkTask(
        task_id="sab.h_importances_92",
        name="SAB #92 · JNMF importance factors (agent-eval rep)",
        source_project="OSU-NLP-Group/ScienceAgentBench",
        category="agent_eval",
        modality="mixed",
        dataset_desc=(
            "ScienceAgentBench 实例 #92：由拟合的 JNMF W/H 矩阵计算 6 个重要性因子（纯 numpy）。"
            "官方 gold program 产出 pred，官方 run_eval.py 在容差 1e-4 内比对 gold 参考。数据集 .npy "
            "物化在 /repo 的 vendor 语料中（运行时软链，无需复制 3.7GB）。"
        ),
        eval_metric="max_abs_error",
        direction="lower",
        baseline=None,
        reference=None,
        gates={"tolerance": 1e-4},
        harness="sab_eval",
        run_command="python /repo/scripts/sandbox_examples/run_sab_sandbox.py --data-dir /data/",
        source_path=_oss("experiments/oss_validation/sab/task_92_h_importances"),
        tags=["science-discovery", "agent-eval", "numpy", "jnmf"],
        data_local=True,
        eval_method=(
            "平台研究方式：solve.py（gold-as-solve，代表 agent 产出程序）在沙箱内重跑，run_eval.py 比对 gold；"
            "指标 max_abs_error<=1e-4 即通过。数据集 .npy 运行时从 /repo vendor 软链，避免复制 3.7GB。"
        ),
        note="Agent 评测类代表任务（按 directive ①）。SAB 20 个轻量任务中 15 个已端到端验证。",
    ),
]

# --------------------------------------------------------------------------- #
# Batch-wired OSS tasks (样板优先 replication per 报告第5节).
# Each entry is produced by _oss_wired_extra.build_extra_tasks and mirrors the
# three verified representatives. They became launchable + L1-verifiable
# (real result.json + EvalCompletedEvent). See benchmark_tasks/_oss_wired_extra.py.
# --------------------------------------------------------------------------- #
from ._oss_wired_extra import build_extra_tasks  # noqa: E402
_TASKS.extend(build_extra_tasks(BenchmarkTask, _oss))


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


def _sandbox_isolation(t: BenchmarkTask) -> str:
    """Isolation level the task will run under when launched in agent mode.

    - "none"           platform-native (kaggle_eval / supported) dual loop; no sandbox needed
    - "container-hard" agent mode + AGENT_SANDBOX=1 + Docker available (read-only data, no net)
    - "container-soft" agent mode + AGENT_SANDBOX=1 but Docker unavailable (host soft isolation)
    - "host"           agent mode but AGENT_SANDBOX not set; runs on the host with no isolation
    """
    if t.harness == "kaggle_eval" or t.supported_by_platform:
        return "none"
    if _os.environ.get("AGENT_SANDBOX") == "1":
        return "container-hard" if shutil.which("docker") else "container-soft"
    return "host"


# The 1 GiB data-size policy: a task whose materialised data lower bound exceeds
# this is grayed out from training-optimization (the "data-size > 1 GiB" rule the
# ``_availability`` docstring promises).
_ONE_GIB = 1 << 30


def _availability(t: BenchmarkTask) -> tuple[bool, str, int | None]:
    """Gray-out policy for the benchmark catalog.

    A task is *unavailable* (grayed out, excluded from training-optimization) when:
      * its train/eval data exceeds the 1 GiB policy (e.g. the two external suites
        whose datasets total ~6.9 GB), or
      * it depends on an external sibling repo / Harbor / Arbor environment that is
        not present locally, or
      * it is a custom LLM task whose base-model weights alone exceed 1 GiB and thus
        cannot be trained in this environment.

    Returns ``(enabled, unavailable_reason, data_size_bytes)``. ``data_size_bytes`` is
    the known lower bound on data volume (``None`` == unknown / external).
    """
    tid = t.task_id
    # A task whose data + official scripts are materialised locally (data/oss/<id>/)
    # and that has a sandbox runner is launchable regardless of its source repo —
    # *unless* its declared data lower bound still exceeds the 1 GiB policy. (The
    # size check lives here rather than as a name-prefix rule so a future curated
    # task that materialises >1 GiB locally cannot slip through as enabled.)
    if t.data_local:
        if t.data_size_bytes is not None and t.data_size_bytes > _ONE_GIB:
            return (
                False,
                f"训练/评测数据 >1GB（{t.data_size_bytes:,} bytes，超出本地可训练策略）",
                t.data_size_bytes,
            )
        return (True, "", t.data_size_bytes)
    # Two external suites: data 6.9 GB on disk -> >1 GiB policy.
    if tid.startswith("suite."):
        return (
            False,
            "训练/评测数据 >1GB（外部套件，需手动拉取数据集与官方 grade 脚本）",
            6_900_000_000,
        )
    # External MLE-bench harness: depends on openai/mle-bench dataset (>1 GB).
    if tid == "mlevolve.mle_bench":
        return (
            False,
            "外部依赖 openai/mle-bench 数据集（>1GB），需 Kaggle API 凭据",
            None,
        )
    # Curated OSS tasks live in external sibling repos (Harbor / Arbor / etc.) with
    # no local data or runtime; they are tracked-only and cannot be trained here.
    _EXTERNAL_PREFIXES = (
        "autolab.", "claudini.", "arbor.", "autoresearchclaw.",
        "ara.", "autoclaude.",
    )
    if any(tid.startswith(p) for p in _EXTERNAL_PREFIXES):
        return (
            False,
            "外部兄弟仓库 + Harbor/Arbor 依赖，本仓无本地数据与运行环境",
            None,
        )
    # Custom LLM tasks: base-model weights alone exceed 1 GiB.
    if t.task_type in ("llm_sft", "llm_rl", "llm_opd"):
        return (
            False,
            "基座模型权重 >1GB，需 GPU 与大容量存储，本环境不内置训练",
            None,
        )
    return (True, "", t.data_size_bytes)


def to_dict(t: BenchmarkTask) -> dict[str, Any]:
    eval_method = t.eval_method or _default_eval_method(t)
    execution_mode = "platform" if (t.harness == "kaggle_eval" or t.supported_by_platform) else "agent"
    sandbox_isolation = _sandbox_isolation(t)
    enabled, unavailable_reason, data_size_bytes = _availability(t)
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
        "sandbox_isolation": sandbox_isolation,
        "eval_method": eval_method,
        "goal": goal,
        "run_command": t.run_command,
        "source_path": t.source_path,
        "tags": t.tags,
        "supported_by_platform": t.supported_by_platform,
        "note": t.note,
        "task_type": t.task_type,
        "type_config": t.type_config,
        # ---- gray-out policy (data-size > 1 GiB / external / LLM weights) ----
        "enabled": enabled,
        "unavailable_reason": unavailable_reason,
        "data_size_bytes": data_size_bytes,
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
