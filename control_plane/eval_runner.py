"""Pluggable evaluation for tracked-only tasks (LLM SFT/RL/OPD, embedding).

Platform-executable tasks (tabular classification) compute their metric inside the
dual loop and are captured automatically by ``ControlPlaneService.capture_run_record``.
Tracked-only tasks are run by the user in an *external* harness; the platform's job
is to (a) accept a self-reported metric via ``report_run_metric``, and (b) optionally
compute a metric when the user supplies both the held-out eval set and the model
outputs.

The LLM-as-judge path ships with a deterministic **heuristic** judge (string
normalization + token-overlap F1) so the whole chain is runnable with zero GPU / LLM
dependencies. Set the ``LLM_JUDGE_URL`` environment variable to route to a real LLM
judge instead (the function signature is the same: it returns a dict of metrics).

Nothing here imports torch / transformers — those are the user's external harness
concern. The platform only reads outputs the user produced.
"""

from __future__ import annotations

import csv
import json
import os
import re
from typing import Any

DEFAULT_JUDGE_URL = os.environ.get("LLM_JUDGE_URL")


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"[^0-9a-z一-鿿]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_f1(a: str, b: str) -> float:
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    prec = len(inter) / len(ta)
    rec = len(inter) / len(tb)
    return 2 * prec * rec / (prec + rec)


def _read_rows(path: str) -> list[dict[str, Any]]:
    """Read a csv/jsonl/json file into a list of dict rows."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
            return list(csv.DictReader(fh))
    if ext == ".jsonl":
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    if ext == ".json":
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return json.load(fh)
    raise ValueError(f"不支持的评测数据格式: {ext}（请用 csv/jsonl/json）")


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# --------------------------------------------------------------------------- #
# LLM-as-judge (text generation: SFT / RL / OPD outputs)                       #
# --------------------------------------------------------------------------- #
def llm_judge_eval(
    eval_dataset_path: str,
    predictions_path: str,
    judge_url: str | None = DEFAULT_JUDGE_URL,
    metric: str = "f1",
) -> dict[str, Any]:
    """Compare model outputs against references.

    ``eval_dataset_path`` rows: ``{prompt/prompt, reference/answer/label}``.
    ``predictions_path`` rows: ``{prompt/question, output/prediction/answer}``.

    Returns ``{metric_name, direction, score, details, n}``.
    """
    refs = _read_rows(eval_dataset_path)
    preds = _read_rows(predictions_path)

    ref_by_prompt: dict[str, str] = {}
    for r in refs:
        p = _norm(str(_pick(r, "prompt", "question", "input", "instruction") or ""))
        a = _pick(r, "reference", "answer", "label", "target", "output")
        if p:
            ref_by_prompt[p] = str(a or "")
        elif a is not None:
            # un-keyed: store index-aligned below
            pass

    # Align predictions to references (by prompt key when possible, else by order).
    paired: list[tuple[str, str]] = []
    if ref_by_prompt:
        for pr in preds:
            p = _norm(str(_pick(pr, "prompt", "question", "input", "instruction") or ""))
            out = str(_pick(pr, "output", "prediction", "answer", "generated") or "")
            if p in ref_by_prompt:
                paired.append((out, ref_by_prompt[p]))
    if not paired and len(refs) == len(preds):
        for r, pr in zip(refs, preds):
            out = str(_pick(pr, "output", "prediction", "answer", "generated") or "")
            a = _pick(r, "reference", "answer", "label", "target", "output")
            paired.append((out, str(a or "")))

    if not paired:
        raise ValueError(
            "无法对齐评测集与预测结果：请保证两者含相同 prompt/question 键，或行数一致。"
        )

    if judge_url:
        # A real LLM judge would be called here; signature kept identical.
        raise NotImplementedError(
            f"真实 LLM judge 接入点已预留（{judge_url}），当前环境未实现 HTTP 调用。"
        )

    # Deterministic heuristic judge: token-overlap F1 of prediction vs reference.
    scores = [_token_f1(out, ref) for out, ref in paired]
    n = len(scores)
    avg = sum(scores) / n if n else 0.0
    details = {
        "n": n,
        "f1": round(avg, 6),
        "precision": round(
            sum(_token_f1(out, ref) for out, ref in paired) / n, 6
        ) if n else 0.0,
        "recall": round(
            sum(_token_f1(ref, out) for out, ref in paired) / n, 6
        ) if n else 0.0,
        "judge": "heuristic-token-f1",
    }
    metric_name = metric
    return {
        "metric_name": metric_name,
        "direction": "higher",
        "score": round(avg, 6),
        "details": details,
        "n": n,
    }


# --------------------------------------------------------------------------- #
# Embedding retrieval recall@K (given precomputed ranked lists)                #
# --------------------------------------------------------------------------- #
def embedding_retrieval_eval(
    ranked_lists_path: str,
    ks: tuple[int, ...] = (1, 5, 10),
) -> dict[str, Any]:
    """Compute recall@K over precomputed ranked lists.

    ``ranked_lists_path`` rows: ``{query, gold/gold_doc, ranked/ranked_docs:[...]}``.
    Returns ``{metric_name, direction, score (=recall@max K), details, n}``.
    """
    rows = _read_rows(ranked_lists_path)
    recalls: dict[int, float] = {k: 0.0 for k in ks}
    n = 0
    for row in rows:
        gold = str(_pick(row, "gold", "gold_doc", "target", "relevant") or "")
        ranked = _pick(row, "ranked", "ranked_docs", "topk")
        if gold == "" or ranked is None:
            continue
        if isinstance(ranked, str):
            ranked = json.loads(ranked) if ranked.strip().startswith("[") else [ranked]
        ranked = list(ranked)
        n += 1
        for k in ks:
            topk = ranked[:k]
            recalls[k] += (1.0 if gold in topk else 0.0)
    if n:
        for k in ks:
            recalls[k] = round(recalls[k] / n, 6)
    top_k = max(ks)
    return {
        "metric_name": f"recall_at_{top_k}",
        "direction": "higher",
        "score": recalls.get(top_k, 0.0),
        "details": {"n": n, **{f"recall@{k}": recalls[k] for k in ks}},
        "n": n,
    }


# --------------------------------------------------------------------------- #
# Dispatcher                                                                   #
# --------------------------------------------------------------------------- #
def evaluate_for_task(
    task_type: str,
    eval_dataset_path: str | None = None,
    predictions_path: str | None = None,
    ranked_lists_path: str | None = None,
    judge_url: str | None = DEFAULT_JUDGE_URL,
) -> dict[str, Any]:
    """Pick the right evaluator by task type and return a metric dict."""
    if task_type in ("llm_sft", "llm_rl", "llm_opd"):
        if not eval_dataset_path or not predictions_path:
            raise ValueError(
                "LLM 任务评估需同时提供 eval_dataset_path（prompt+reference）与 predictions_path（模型输出）。"
            )
        return llm_judge_eval(eval_dataset_path, predictions_path, judge_url=judge_url)
    if task_type == "embedding_contrastive":
        if ranked_lists_path:
            return embedding_retrieval_eval(ranked_lists_path)
        if eval_dataset_path and predictions_path:
            # Treat predictions as the ranked lists file if it carries `ranked`.
            return embedding_retrieval_eval(predictions_path)
        raise ValueError(
            "Embedding 任务评估需提供 ranked_lists_path（含 query/gold/ranked），或 predictions_path。"
        )
    raise ValueError(f"任务类型 {task_type} 暂不支持平台端评估（请使用 report-metric 手动上报）。")
