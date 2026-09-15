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

import json
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
    # 主分类（任务研究什么，v2 分类体系 2026-09）：
    # ml_modeling | perf_opt | safety_adversarial | agent_eval
    category: str
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
    # Research background / motivation (why this task exists as *research work* and what
    # a researcher optimising it is actually studying). Authored centrally in
    # ``_BACKGROUNDS`` (keyed by task_id, ``sab.*`` prefix-fallback) so curated and
    # batch-wired entries share one place; per-instance values win when set.
    background: str = ""
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
    # 二级子类（主分类内的细分，见 SUBCATEGORY_LABELS）；空串表示无子类
    sub_category: str = ""


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
        category="perf_opt",
        sub_category="compression",
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
        category="agent_eval",
        sub_category="ml_engineering",
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
        sub_category="scientific_discovery",
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
        category="agent_eval",
        sub_category="ml_engineering",
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
        category="ml_modeling",
        sub_category="tabular",
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
        category="ml_modeling",
        sub_category="tabular",
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
        category="ml_modeling",
        sub_category="tabular",
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
        category="ml_modeling",
        sub_category="tabular",
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
        category="ml_modeling",
        sub_category="tabular",
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
        sub_category="scientific_discovery",
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


# --------------------------------------------------------------------------- #
# Task data preview (任务数据样本): materialised train/eval data cases + volume
# stats for the catalog detail panel. Read-only; never executes task code.
# --------------------------------------------------------------------------- #
_DATA_EXTS = {".csv", ".jsonl", ".json", ".npz", ".parquet"}
_MAX_SAMPLE_BYTES = 64 * 1024 * 1024  # sample only files <= 64 MiB
_TRUNC = 120  # truncate long cell values for the wire


def _truncate(v: Any) -> Any:
    s = str(v)
    return s if len(s) <= _TRUNC else s[: _TRUNC - 1] + "…"


def _preview_csv(path: str, sample_rows: int) -> dict[str, Any]:
    import pandas as pd

    head = pd.read_csv(path, nrows=sample_rows)
    # Row count: cheap newline count (CSV without embedded newlines in this catalog).
    with open(path, "rb") as f:
        rows = max(0, sum(1 for _ in f) - 1)
    return {
        "kind": "csv",
        "columns": [str(c) for c in head.columns][:20],
        "dtypes": [str(d) for d in head.dtypes][:20],
        "rows": rows,
        "samples": [[_truncate(x) for x in row] for row in head.values.tolist()],
    }


def _preview_jsonl(path: str, sample_rows: int) -> dict[str, Any]:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    samples = []
    for ln in lines[:sample_rows]:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            obj = ln.strip()[:_TRUNC]
        samples.append(obj if isinstance(obj, list) else [obj] if not isinstance(obj, dict) else
                       [_truncate(f"{k}={v}") for k, v in list(obj.items())[:8]])
    return {
        "kind": "jsonl",
        "columns": None,
        "dtypes": None,
        "rows": len(lines),
        "samples": samples,
    }


def _preview_json(path: str, sample_rows: int) -> dict[str, Any]:
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if isinstance(data, list):
        rows = len(data)
        samples = [
            [_truncate(f"{k}={v}") for k, v in list(x.items())[:8]]
            if isinstance(x, dict) else [_truncate(x)]
            for x in data[:sample_rows]
        ]
        return {"kind": "json", "columns": None, "dtypes": None, "rows": rows, "samples": samples}
    if isinstance(data, dict):
        # dict-of-arrays (e.g. {"train": [...], "test": [...]}) or config — show keys.
        inner = {k: len(v) for k, v in data.items() if isinstance(v, (list, dict))}
        return {
            "kind": "json",
            "columns": [str(k) for k in list(data.keys())[:20]],
            "dtypes": None,
            "rows": None,
            "samples": [[_truncate(f"{k}: {v} 条" if isinstance(v, (list, dict)) else f"{k}={v}")]
                        for k, v in list(data.items())[:sample_rows]],
            "kv_counts": inner,
        }
    return {"kind": "json", "columns": None, "dtypes": None, "rows": None, "samples": [[_truncate(data)]]}


def _preview_npz(path: str, sample_rows: int) -> dict[str, Any]:
    import numpy as np

    with np.load(path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    out: dict[str, Any] = {"kind": "npz", "columns": list(arrays.keys())[:20], "dtypes": None,
                           "rows": None, "samples": [], "arrays": []}
    for name, arr in list(arrays.items())[:12]:
        shape = list(arr.shape)
        out["arrays"].append({"name": name, "shape": shape, "dtype": str(arr.dtype)})
        if arr.ndim == 1:
            out["samples"].append([_truncate(x) for x in arr[:sample_rows]])
        elif arr.ndim == 2:
            out["samples"].append(
                [[_truncate(x) for x in row[:10]] for row in arr[:sample_rows]]
            )
    # A (n, f) 2-D array is the case count.
    for name, arr in arrays.items():
        if arr.ndim == 2:
            out["rows"] = int(arr.shape[0])
            break
    return out


def get_task_data_preview(task_id: str, max_files: int = 8, sample_rows: int = 5) -> dict[str, Any]:
    """Sample cases + volume stats for a task's materialised train/eval data.

    Looks in ``source_path`` (if a directory) and ``data/oss/<task_id>``. Only
    small data files (<= ``_MAX_SAMPLE_BYTES``) are sampled; huge binaries are
    listed with their size but no rows/samples.
    """
    t = get_task(task_id)
    if t is None:
        return {"task_id": task_id, "found": False, "dirs": [], "files": [], "total_bytes": 0}

    def _remap(p: str) -> str:
        """Remap a foreign-machine path (e.g. /Users/<x>/.../safety_auto_research/<rest>)
        into this checkout: everything after the last ``safety_auto_research/`` component
        is re-rooted at _PKG_ROOT. Custom tasks registered on another machine carry such
        absolute dataset paths in their type_config."""
        p = str(p or "")
        marker = "safety_auto_research" + _os.sep
        idx = p.rfind(marker)
        if idx >= 0:
            rest = p[idx + len(marker):].lstrip(_os.sep)
            return _os.path.join(_PKG_ROOT, rest)
        return p

    raw_candidates: list[str] = []
    if t.source_path:
        raw_candidates.append(t.source_path)
    oss_dir = _os.path.join(_PKG_ROOT, "data", "oss", task_id)
    raw_candidates.append(oss_dir)
    # Custom-registered tasks keep their dataset paths in type_config.
    cfg = t.type_config or {}
    for k in ("dataset_path", "data_dir", "manifest_path", "prompt_dataset"):
        if cfg.get(k):
            raw_candidates.append(str(cfg[k]))

    candidates: list[str] = []
    for raw in raw_candidates:
        p = _remap(raw)
        if not p:
            continue
        # A file path (e.g. .../train.csv) is scanned via its parent directory.
        if _os.path.isfile(p):
            p = _os.path.dirname(p)
        if _os.path.isdir(p) and p not in candidates:
            candidates.append(p)
    if not candidates:
        return {"task_id": task_id, "found": True, "dirs": [], "files": [], "total_bytes": 0,
                "note": "该任务的数据未物化到本地（外部依赖或仅保留任务定义）。"}

    files: list[str] = []
    for d in candidates[:2]:
        for root, dirs, names in _os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            if root[len(d):].count(_os.sep) >= 2:
                dirs[:] = []
            for n in sorted(names):
                p = _os.path.join(root, n)
                if _os.path.splitext(n)[1].lower() in _DATA_EXTS:
                    files.append(p)
    # Prefer the most-informative files: sort small-first so big suites don't crowd out samples.
    files.sort(key=lambda p: _os.path.getsize(p))
    picked = files[:max_files]

    previews: list[dict[str, Any]] = []
    total_bytes = 0
    for p in picked:
        size = _os.path.getsize(p)
        total_bytes += size
        entry: dict[str, Any] = {
            "path": _os.path.relpath(p, candidates[0]),
            "size_bytes": size,
            "preview": None,
        }
        if size <= _MAX_SAMPLE_BYTES:
            ext = _os.path.splitext(p)[1].lower()
            try:
                if ext == ".csv":
                    entry["preview"] = _preview_csv(p, sample_rows)
                elif ext == ".jsonl":
                    entry["preview"] = _preview_jsonl(p, sample_rows)
                elif ext == ".json":
                    entry["preview"] = _preview_json(p, sample_rows)
                elif ext == ".npz":
                    entry["preview"] = _preview_npz(p, sample_rows)
            except Exception as exc:  # a broken file must not kill the whole preview
                entry["error"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["error"] = "文件过大，仅列出规模"
        previews.append(entry)

    return {
        "task_id": task_id,
        "found": True,
        "dirs": [_os.path.relpath(d, _PKG_ROOT) for d in candidates[:2]],
        "files": previews,
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


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


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _sandbox_isolation(t: BenchmarkTask) -> str:
    """Isolation level the task will run under when launched in agent mode.

    Docker 隔离现在是**默认启用**的：只要本机 Docker 可用，agent 模式任务一律在
    一次性容器（read-only 数据/代码挂载 + 掉权 + 资源限制）内执行。显式禁用：
    ``AGENT_SANDBOX=0`` / ``AGENT_SANDBOX_DISABLE=1``；需完全离线可设
    ``AGENT_SANDBOX_NETWORK=none``（默认 bridge）。

    - "none"           platform-native (kaggle_eval / supported) dual loop; no sandbox needed
    - "container-hard" agent mode + Docker available（默认）
    - "container-soft" Docker unavailable（回退宿主软隔离，结果诚实标注）
    - "host"           agent mode + 显式禁用沙箱；runs on the host with no isolation
    """
    if t.harness == "kaggle_eval" or t.supported_by_platform:
        return "none"
    if _os.environ.get("AGENT_SANDBOX") == "0" or _os.environ.get("AGENT_SANDBOX_DISABLE") == "1":
        return "host"
    return "container-hard" if _docker_available() else "container-soft"


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


# --------------------------------------------------------------------------- #
# Research background per task (研究背景). Single place so both curated and
# batch-wired entries carry a human-readable "what is this research actually
# studying" description in the catalog detail panel. ``sab.*`` entries fall back
# to the family entry; explicit per-instance ``background`` always wins.
# --------------------------------------------------------------------------- #
_BACKGROUNDS: dict[str, str] = {
    "autolab.safety_router": (
        "LLM 安全部署研究：安全路由器依据输入特征决定「直接回答 / 转交安全审查」二分类。"
        "该任务研究的是**模型压缩与安全性的权衡**——在固定 2 层 MLP 结构（model.py 哈希钉死）下，"
        "把参数量压到最小，同时保住整体准确率与不安全样本召回（unsafe_recall）、安全样本召回三道门限。"
        "研究者优化的本质是：容量预算内安全分类边界的保留程度。"
    ),
    "autolab.grpo_multisource": (
        "RLHF/GRPO 训练系统研究：GRPO（组相对策略优化）是多源视觉-数学对齐训练的主流算法，"
        "其训练步由 rollout 采样、组内优势归一化与策略梯度更新组成，吞吐直接决定训练成本。"
        "本任务以真实 NumPy 参考步测步延迟，研究训练步实现的可行性与吞吐特征（原任务需 L40S GPU 微调 7B 模型，"
        "此处为声明的 CPU 代理）。"
    ),
    "autolab.flash_attention": (
        "注意力核性能优化研究：softmax(QK^T/√d)V 是 Transformer 推理/训练的热点算子，"
        "FlashAttention 类分块+重计算实现是其标准优化。本任务在 n=4096、d=64 的真实张量上测核延迟，"
        "研究算子实现层面的延迟优化空间。"
    ),
    "autolab.aes128_ctr": (
        "密码工程吞吐优化研究：AES-128-CTR 是对称加密的标准工作模式，生产实现依赖 AES-NI 等 "
        "SIMD 指令集。本任务以纯 Python 真实实现对 1 MiB 缓冲计时并外推吞吐，"
        "研究实现方式（查表/位运算/向量化）对加密吞吐的影响。"
    ),
    "autolab.adaptive_compression": (
        "无损压缩的信息论研究：字节级上下文混合建模（PPM 族）是压缩率的核心。"
        "本任务在 9 族官方可见序列上测整体 bits_per_byte，研究上下文阶数与混合策略"
        "对压缩率的收益——每降 0.1 bpb 都是真实的建模改进。"
    ),
    "autolab.ntt_butterfly_cuda": (
        "数论变换（NTT）核优化研究：NTT 是同态加密与格密码的核心运算，Goldilocks 素域上的 "
        "bit-exact 变换尤其重要。本任务实现 radix-2 Cooley-Tukey NTT 并对 n=65536 计时，"
        "研究素域变换的实现延迟空间（往返正确性有断言保证）。"
    ),
    "autolab.llm_online_serving": (
        "LLM 推理服务系统研究：在线服务的核心杠杆是连续批处理（continuous batching）——"
        "把并发请求动态拼批以同时提升吞吐与降低完成时间。本任务以真实本地请求处理器对比"
        "连续批处理与串行基线，研究批处理策略对 serving_score（吞吐/完成时间复合分）的影响。"
    ),
    "arbor.algotune_knn": (
        "算法效率优化研究（AlgoTune 范式）：kNN 暴力解是 O(n·d) 距离计算，存在空间划分、"
        "向量化、部分排序等大量优化路径。研究者编辑 solution.py 搜索更快实现，"
        "正确性门限由独立校验器在全部实例上验证（任一失败计 0 分），"
        "研究的是**正确性约束下的真实加速比**。"
    ),
    "autoclaude.trigger_eval": (
        "Agent 技能路由元评测研究：skill 化 agent 依赖「描述与用户意图的匹配」来触发正确技能，"
        "误触发/漏触发直接决定 agent 行为质量。本任务以 (skill, query) 正负样本集评测触发判定，"
        "研究技能描述工程与触发判定策略对 trigger_rate 与混淆率的影响。"
    ),
    "claudini.random": (
        "LLM 对抗鲁棒性研究（GCG 范式）：随机-ASCII 目标序列攻击是衡量「模型能否被驱使生成任意固定串」"
        "的对照实验——目标无语义，攻击成功即证明优化过程本身有效。本任务以灰盒 logprob 爬山在查询预算内"
        "优化对抗后缀，研究攻击迁移性与查询效率（ASR 为越低越好方向的诚实度量）。"
    ),
    "claudini.injection": (
        "提示注入攻击研究：注入攻击考察模型对上下文中指令与系统指令的区分能力。"
        "本任务在 AlpacaFarm 风格提示上以对抗后缀诱导输出 'Hacked'，"
        "并排除「复述注入指令」类假阳性，研究注入成功（robust ASR）与模型抗性边界。"
    ),
    "claudini.safeguard": (
        "安全护栏绕过研究：对齐模型以拒绝机制抵御有害查询，绕过攻击测量该机制的鲁棒性。"
        "本任务在 ClearHarm 风格有害查询上优化后缀以诱导 compliance opener（'Sure'），"
        "研究拒绝机制的失效模式——对齐良好的受害者 ASR≈0 即攻击未果（诚实指标）。"
    ),
    "ara.understanding": (
        "科研产物理解评测研究：一个科研 agent 能否读懂已有研究产物（论文/代码/实验）"
        "决定其复现与扩展能力。本任务以论文级问题集（理解/复现/扩展三类）评测 LLM 的闭卷理解正确率，"
        "研究 agent 的科研产物理解边界。"
    ),
    "autoresearchclaw.arc_bench": (
        "开放式科研智能体评测研究：给定真实研究课题（55 主题跨 ML/HEP/量子/生物/统计），"
        "评测 agent 能否产出结构化研究结果（分条件指标 + 假设裁决 + 报告）。"
        "本任务在 ML01 主题上以 rubric 加权分（指标/条件/假设覆盖 + 合理性）度量研究智能体的开环研究能力。"
    ),
    "sab.h_importances_92": (
        "科学发现智能体评测研究（ScienceAgentBench）：真实科研中的数据分析任务（此处为由 JNMF 矩阵"
        "计算重要性因子）要求 agent 产出可执行且与 gold 供程序一致的 Python 程序，"
        "研究 agent 在数值科学工作流上的正确性边界（max_abs_error≤1e-4）。"
    ),
    "sab.*": (
        "科学发现智能体评测研究（ScienceAgentBench 轻量实例）：SAB 从 44 篇同行评审论文中提炼 "
        "102 个数据驱动的科学发现任务（生物信息/计算化学/GIS/心理认知），要求 agent 产出能通过"
        "官方 eval 脚本的自包含 Python 程序。本条目为其中已物化到本地的轻量实例，"
        "研究 agent 在具体科学计算工作流上的程序合成正确性（详见评估方式与 manifest）。"
    ),
    "platform.titanic": (
        "平台原生端到端研究基线：泰坦尼克生存预测是 Kaggle 入门竞赛，"
        "作为双循环（内循环训练优化 → 外循环审计 → 递归改进）的**可复现最小研究闭环**样例，"
        "用于验证平台的研究-审计-改进机制本身，而非追求榜单名次。"
    ),
    "platform.spaceship": (
        "平台原生端到端研究基线：Spaceship-Titanic 是 Kaggle 二分类竞赛（传送门乘客是否被送入另一维度），"
        "特征含缺失与类别变量，比 Titanic 略复杂，作为双循环机制验证的第二个实跑任务。"
    ),
    "platform.wine": (
        "平台原生端到端研究基线：葡萄酒理化指标 → 品质二分类（sklearn load_wine，按 flavanoids 中位数切分），"
        "小样本（178 行）高维场景，验证双循环在小数据上的评测与审计行为。"
    ),
    "platform.iris": (
        "平台原生端到端研究基线：Fisher 鸢尾花三分类，经典 ML 基准（150 行 × 4 特征），"
        "用于验证双循环在多分类指标（accuracy）下的执行与审计。"
    ),
    "platform.breast_cancer": (
        "平台原生端到端研究基线：威斯康星乳腺癌诊断（569 行 × 30 特征二分类），"
        "医疗类强基线任务（GBM>0.95），验证双循环在高基线任务上的门限与审计判定。"
    ),
    "mlevolve.mle_bench": (
        "ML 工程智能体评测研究：MLE-bench 覆盖 75 个真实 Kaggle 竞赛，以奖牌率衡量 agent 的"
        "端到端 ML 工程能力（读数据/建模/训练/提交）。本平台仅集成其 harness，数据外部依赖。"
    ),
    "suite.science_agent_bench": (
        "科学发现智能体评测研究（套件级）：SAB 102 任务覆盖四大自然与社会科学学科，"
        "以成功率（SR）/可执行率（VER）/代码质量/成本多维评测 agent 的科学工作流能力。"
        "套件 manifest 与论文基线已内置，原始数据集需外部获取。"
    ),
    "suite.mle_bench": (
        "ML 工程智能体评测研究（套件级）：openai/mle-bench 官方 75 竞赛套件，"
        "以 any_medal_percentage（≥铜牌占比，≥3 seeds）为主指标，含官方数据泄漏标注剔除。"
        "原始竞赛数据经 Kaggle API 外部获取。"
    ),
}


def _background_for(t: BenchmarkTask) -> str:
    if t.background:
        return t.background
    if t.task_id in _BACKGROUNDS:
        return _BACKGROUNDS[t.task_id]
    if t.task_id.startswith("sab."):
        return _BACKGROUNDS.get("sab.*", "")
    return ""


def _metric_detail_for(metric_id: str) -> dict[str, Any] | None:
    """指标详解（含义 / 计算方式 / 参考实现），来自评估指标目录。"""
    try:
        from .metric_catalog import get_metric_detail

        return get_metric_detail(metric_id)
    except Exception:
        return None


def to_dict(t: BenchmarkTask) -> dict[str, Any]:
    eval_method = t.eval_method or _default_eval_method(t)
    execution_mode = "platform" if (t.harness == "kaggle_eval" or t.supported_by_platform) else "agent"
    sandbox_isolation = _sandbox_isolation(t)
    enabled, unavailable_reason, data_size_bytes = _availability(t)
    # source_path 回退：在原机器上注册的任务其 source_path 指向外部兄弟仓库
    # （_OSS_ROOT 下的 autolab/... 等），本机不存在；只要任务数据已物化到本仓
    # data/oss/<task_id>/，就回退到该目录——launch 的沙箱挂载、数据预览、桥接
    # 推荐调用都依赖它。
    source_path = t.source_path
    if not source_path or not _os.path.exists(source_path):
        oss_dir = _os.path.join(_PKG_ROOT, "data", "oss", t.task_id)
        if _os.path.isdir(oss_dir):
            source_path = oss_dir
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
        "sub_category": t.sub_category,
        "modality": t.modality,
        "dataset_desc": t.dataset_desc,
        "eval_metric": t.eval_metric,
        "metric_detail": _metric_detail_for(t.eval_metric),
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
        "source_path": source_path,
        "tags": t.tags,
        "supported_by_platform": t.supported_by_platform,
        "note": t.note,
        "background": _background_for(t),
        "task_type": t.task_type,
        "type_config": t.type_config,
        # ---- gray-out policy (data-size > 1 GiB / external / LLM weights) ----
        "enabled": enabled,
        "unavailable_reason": unavailable_reason,
        "data_size_bytes": data_size_bytes,
    }


CATEGORY_LABELS = {
    # v2 分类体系（2026-09）：主分类只描述「任务研究什么」，执行方式/来源等
    # 正交属性不再混入 category（见 doc 中的分类规范与 SUBCATEGORY_LABELS）。
    "ml_modeling": "机器学习建模",
    "perf_opt": "性能与效率优化",
    "safety_adversarial": "安全与对抗",
    "agent_eval": "智能体能力评测",
}

SUBCATEGORY_LABELS = {
    # ml_modeling
    "tabular": "表格建模",
    "sandbox": "沙箱建模",
    # perf_opt
    "kernel": "算子与内核",
    "algo": "算法加速",
    "compression": "模型/编码压缩",
    "llm_systems": "LLM 训练与服务",
    # safety_adversarial
    "attack": "攻击与越狱",
    # agent_eval
    "scientific_discovery": "科学发现",
    "ml_engineering": "ML 工程",
    "open_research": "开放式科研",
    "meta_eval": "元评测",
}
