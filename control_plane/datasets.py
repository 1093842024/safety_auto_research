"""Dataset registry for autonomous-research bootstrap (数据集管理).

Researchers register *local* datasets (path-based import; no copying) so the
platform can track the initial train/eval data of a research effort:

* modality: ``text`` / ``image`` / ``audio`` (图文音)
* task kind: ``classification`` (图文音分类) / ``llm_generation`` (大模型回答生成)
* ``data_path``  — where the data lives (a file or a directory; per-class
  sub-directories are the usual layout for image/audio classification)
* ``label_file`` — optional separate annotation file (CSV/JSONL manifest)
* ``label_field``/``content_field`` — which columns/keys hold the label / content

The store is a small JSON file (``data/datasets.json``) with atomic writes; the
whole thing is read-only for the research pipeline (registration & stats only).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCK = threading.Lock()

MODALITIES = ("text", "image", "audio")
TASK_KINDS = ("classification", "llm_generation")

_MEDIA_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tiff"},
    "audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a"},
    "text": {".txt"},
}
_DATA_EXTS = {".csv", ".jsonl", ".json", ".parquet", ".tsv"}
_TRUNC = 120


def _store_path() -> str:
    return os.environ.get(
        "DATASET_STORE", os.path.join(_PKG_ROOT, "data", "datasets.json")
    )


def _read_raw() -> list[dict[str, Any]]:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_raw(items: list[dict[str, Any]]) -> None:
    tmp = _store_path() + ".tmp"
    os.makedirs(os.path.dirname(_store_path()), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _store_path())


def list_datasets() -> list[dict[str, Any]]:
    with _LOCK:
        items = _read_raw()
    return sorted(items, key=lambda d: d.get("created_at", ""), reverse=True)


def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for d in _read_raw():
            if d.get("dataset_id") == dataset_id:
                return d
    return None


def delete_dataset(dataset_id: str) -> bool:
    with _LOCK:
        items = _read_raw()
        kept = [d for d in items if d.get("dataset_id") != dataset_id]
        if len(kept) == len(items):
            return False
        _write_raw(kept)
    return True


def remap_foreign_path(p: str) -> str:
    """Re-root a foreign-machine path at this checkout.

    Custom registrations made elsewhere carry absolute dataset paths like
    ``/Users/<x>/.../safety_auto_research/benchmark_tasks/sample_data/...``;
    everything after the last ``safety_auto_research/`` component is re-rooted
    at this package root so previously-registered datasets keep working.
    """
    p = str(p or "")
    marker = "safety_auto_research" + os.sep
    idx = p.rfind(marker)
    if idx >= 0:
        rest = p[idx + len(marker):].lstrip(os.sep)
        return os.path.join(_PKG_ROOT, rest)
    return p


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def _read_table(path: str, limit: int = 5) -> tuple[int, list[str], list[list[Any]]]:
    """Rows / columns / first ``limit`` rows for csv / jsonl / json-list files."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        import pandas as pd

        head = pd.read_csv(path, nrows=limit)
        with open(path, "rb") as f:
            rows = max(0, sum(1 for _ in f) - 1)
        return (
            rows,
            [str(c) for c in head.columns],
            [[_trunc(x) for x in row] for row in head.values.tolist()],
        )
    if ext == ".jsonl":
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        cols: list[str] = []
        samples = []
        for ln in lines[:limit]:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                obj = {}
            if isinstance(obj, dict):
                for k in obj:
                    if k not in cols:
                        cols.append(k)
                samples.append([_trunc(f"{k}={obj.get(k)}") for k in list(obj)[:8]])
            else:
                samples.append([_trunc(obj)])
        return len(lines), cols, samples
    if ext == ".json":
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            cols = list(data[0].keys())[:20] if data and isinstance(data[0], dict) else []
            samples = [
                [_trunc(f"{k}={x.get(k)}") for k in list(x)[:8]]
                if isinstance(x, dict)
                else [_trunc(x)]
                for x in data[:limit]
            ]
            return len(data), cols, samples
        return 0, [str(k) for k in list(data)[:20]], [
            [_trunc(f"{k}: {len(v) if isinstance(v, (list, dict)) else v}")]
            for k, v in list(data.items())[:limit]
        ]
    if ext in (".tsv", ".parquet"):
        import pandas as pd

        sep = "\t" if ext == ".tsv" else None
        head = pd.read_csv(path, sep=sep, nrows=limit)
        rows = len(pd.read_csv(path, sep=sep, usecols=[0]))
        return rows, [str(c) for c in head.columns], [
            [_trunc(x) for x in row] for row in head.values.tolist()
        ]
    raise ValueError(f"unsupported table format: {ext}")


def _value_distribution(path: str, column: str, top: int = 20) -> dict[str, int]:
    """Class distribution of ``column`` in a csv/jsonl/json file (top ``top``)."""
    ext = os.path.splitext(path)[1].lower()
    counts: dict[str, int] = {}
    if ext == ".csv":
        import pandas as pd

        if column not in pd.read_csv(path, nrows=1).columns:
            return counts
        for v in pd.read_csv(path, usecols=[column])[column]:
            key = str(v)
            counts[key] = counts.get(key, 0) + 1
    elif ext == ".jsonl":
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and column in obj:
                    key = str(obj[column])
                    counts[key] = counts.get(key, 0) + 1
    elif ext == ".json":
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            for x in data:
                if isinstance(x, dict) and column in x:
                    key = str(x[column])
                    counts[key] = counts.get(key, 0) + 1
    if len(counts) > top:
        kept = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:top])
        others = sum(counts.values()) - sum(kept.values())
        kept["__other__"] = others
        counts = kept
    return counts


def analyze_dataset(
    *,
    modality: str,
    task_kind: str,
    data_path: str,
    label_file: str = "",
    label_field: str = "",
    content_field: str = "",
) -> dict[str, Any]:
    """Compute live stats + samples for a registered dataset (no data is copied)."""
    media = _MEDIA_EXTS.get(modality, set())
    stats: dict[str, Any] = {
        "num_samples": None,
        "size_bytes": 0,
        "columns": [],
        "samples": [],
        "label_stats": None,
        "media_file_count": None,
        "notes": [],
    }

    data_path = data_path.strip()
    if os.path.isfile(data_path):
        roots = [os.path.dirname(data_path)]
        primary_file = data_path
    else:
        roots = [data_path]
        primary_file = ""

    # ---- table files (csv/jsonl/...) under the roots, depth<=2 ----
    tables: list[str] = []
    media_count = 0
    for root in roots:
        for cur, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if cur[len(root):].count(os.sep) >= 2:
                dirs[:] = []
            for n in sorted(names):
                p = os.path.join(cur, n)
                ext = os.path.splitext(n)[1].lower()
                if ext in _DATA_EXTS:
                    tables.append(p)
                if ext in media:
                    media_count += 1
    stats["size_bytes"] = sum(_dir_size(r) for r in roots) if os.path.isdir(roots[0]) else (
        os.path.getsize(roots[0]) if os.path.isfile(roots[0]) else 0
    )

    # ---- classification over a per-class directory tree ----
    if os.path.isdir(data_path) and media and media_count > 0:
        per_class: dict[str, int] = {}
        for cur, _dirs, names in os.walk(data_path):
            rel = os.path.relpath(cur, data_path)
            if rel.startswith("."):
                continue
            label = rel.split(os.sep)[0] if rel != "." else "(root)"
            n = sum(1 for n in names if os.path.splitext(n)[1].lower() in media)
            if n:
                per_class[label] = per_class.get(label, 0) + n
        if len(per_class) > 1 or "(root)" not in per_class:
            stats["media_file_count"] = media_count
            stats["num_samples"] = media_count
            stats["label_stats"] = dict(
                sorted(per_class.items(), key=lambda kv: -kv[1])[:30]
            )
            stats["notes"].append("按子目录名作为类别标签统计（每类文件数）。")
            return stats

    # ---- table-driven (classification manifest or LLM generation pairs) ----
    target_table = primary_file or (tables[0] if tables else "")
    if target_table:
        try:
            rows, cols, samples = _read_table(target_table)
            stats["num_samples"] = rows
            stats["columns"] = cols
            stats["samples"] = samples
            label_col = label_field or ("label" if "label" in cols else "")
            if task_kind == "classification" and label_col:
                stats["label_stats"] = _value_distribution(target_table, label_col)
            elif task_kind == "llm_generation":
                stats["notes"].append(
                    "回答生成数据：应包含 prompt/instruction 字段与参考回答字段（如 response/output）。"
                )
        except Exception as exc:
            stats["notes"].append(f"主数据文件解析失败: {type(exc).__name__}: {exc}")

    # ---- separate label/annotation file ----
    label_file = label_file.strip()
    if label_file:
        try:
            lrows, lcols, lsamples = _read_table(label_file)
            stats["label_file_stats"] = {
                "rows": lrows,
                "columns": lcols,
                "samples": lsamples,
            }
            lf_col = label_field or next(
                (c for c in lcols if c.lower() in ("label", "class", "category", "answer")), ""
            )
            if lf_col:
                stats["label_stats"] = _value_distribution(label_file, lf_col)
                stats["notes"].append(f"标签取自标签文件的 {lf_col} 字段。")
            if stats.get("num_samples") is None:
                stats["num_samples"] = lrows
        except Exception as exc:
            stats["notes"].append(f"标签文件解析失败: {type(exc).__name__}: {exc}")

    if media_count:
        stats["media_file_count"] = media_count
        stats["notes"].append(f"目录内媒体文件（{modality}）共 {media_count} 个。")
    return stats


def register_dataset(
    *,
    name: str,
    modality: str,
    task_kind: str,
    data_path: str,
    label_file: str = "",
    label_field: str = "",
    content_field: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Validate + analyze + persist a dataset registration. Raises ValueError on bad input."""
    if modality not in MODALITIES:
        raise ValueError(f"modality 必须是 {MODALITIES} 之一")
    if task_kind not in TASK_KINDS:
        raise ValueError(f"task_kind 必须是 {TASK_KINDS} 之一")
    if not name.strip():
        raise ValueError("name 不能为空")

    data_path = remap_foreign_path(data_path.strip())
    label_file = remap_foreign_path(label_file.strip())
    if not data_path:
        raise ValueError("data_path 不能为空")
    if not os.path.exists(data_path):
        raise ValueError(f"data_path 不存在: {data_path}")
    if label_file and not os.path.exists(label_file):
        raise ValueError(f"label_file 不存在: {label_file}")

    stats = analyze_dataset(
        modality=modality,
        task_kind=task_kind,
        data_path=data_path,
        label_file=label_file,
        label_field=label_field,
        content_field=content_field,
    )
    record = {
        "dataset_id": "ds-" + uuid.uuid4().hex[:12],
        "name": name.strip(),
        "modality": modality,
        "task_kind": task_kind,
        "data_path": data_path,
        "label_file": label_file,
        "label_field": label_field.strip(),
        "content_field": content_field.strip(),
        "format": ("dir" if os.path.isdir(data_path) else os.path.splitext(data_path)[1].lstrip(".").lower() or "file"),
        "notes": notes.strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **stats,
    }
    with _LOCK:
        items = _read_raw()
        items.append(record)
        _write_raw(items)
    return record


def refresh_dataset(dataset_id: str) -> dict[str, Any] | None:
    """Re-run live analysis for a stored dataset (paths may have grown/changed)."""
    with _LOCK:
        items = _read_raw()
        rec = next((d for d in items if d.get("dataset_id") == dataset_id), None)
        if rec is None:
            return None
    if not os.path.exists(rec["data_path"]):
        return rec
    stats = analyze_dataset(
        modality=rec["modality"],
        task_kind=rec["task_kind"],
        data_path=rec["data_path"],
        label_file=rec.get("label_file", ""),
        label_field=rec.get("label_field", ""),
        content_field=rec.get("content_field", ""),
    )
    rec = {**rec, **stats}
    with _LOCK:
        items = _read_raw()
        items = [rec if d.get("dataset_id") == dataset_id else d for d in items]
        _write_raw(items)
    return rec


def _trunc(v: Any) -> Any:
    s = str(v)
    return s if len(s) <= _TRUNC else s[: _TRUNC - 1] + "…"
