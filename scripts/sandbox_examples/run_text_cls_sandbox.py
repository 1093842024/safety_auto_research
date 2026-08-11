"""Standalone, container-safe TEXT classification for the F3 Docker sandbox.

This is the non-kaggle counterpart of ``run_kaggle_eval_sandbox.py``: it turns a
*tracked-only* ``text_classification`` benchmark task (which previously "empty-ran")
into a real, isolated, measured run.

It reads a CSV of (text, label) from ``AGENT_DATA_DIR`` (read-only mount), trains a
TF-IDF + linear model (no GPU / torch needed -- runs in the sklearn-only sandbox
image), evaluates it, records an **isolation proof** (data mounted read-only, network
unreachable), and writes ``result.json`` that ``SandboxResearchExecutor`` turns into a
real ``EvalCompletedEvent``.

Usage (run via scripts/build_agent_sandbox.sh):
  python /repo/scripts/sandbox_examples/run_text_cls_sandbox.py \\
      --data-dir /data/text_cls_demo --text-col text --label-col label \\
      --eval-metric f1_macro --threshold 0.5 --result-name result.json
"""

from __future__ import annotations

import argparse
import json
import os
import socket

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC


# --------------------------------------------------------------------------- #
# Isolation probes (identical spirit to run_kaggle_eval_sandbox.py)            #
# --------------------------------------------------------------------------- #
def probe_readonly(data_dir: str) -> bool:
    """Return True if the data dir rejects writes (read-only mount)."""
    probe = os.path.join(data_dir, ".sandbox_write_probe")
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        try:
            os.remove(probe)
        except OSError:
            pass
        return False  # writable -> not isolated
    except OSError:
        return True  # read-only -> isolated


def probe_network_blocked() -> bool:
    """Return True if outbound network is unreachable (--network none)."""
    for host in ("8.8.8.8", "registry-1.docker.io", "pypi.org"):
        try:
            with socket.create_connection((host, 443), timeout=2):
                return False  # something answered -> network is up
        except OSError:
            continue
    return True  # nothing reachable -> blocked


# --------------------------------------------------------------------------- #
# Model factory                                                                #
# --------------------------------------------------------------------------- #
def _build_model(name: str) -> Pipeline:
    vec = TfidfVectorizer(
        sublinear_tf=True, min_df=2, ngram_range=(1, 2), max_features=20000
    )
    if name in ("logreg", "lr", "logistic"):
        clf = LogisticRegression(max_iter=2000, C=4.0, random_state=42)
    elif name in ("svm", "linearsvc"):
        clf = LinearSVC(C=1.0, random_state=42)
    else:
        clf = LogisticRegression(max_iter=2000, C=4.0, random_state=42)
    return Pipeline([("tfidf", vec), ("clf", clf)])


def _scorer(metric: str):
    if metric.startswith("f1_macro"):
        return lambda y_t, y_p: f1_score(y_t, y_p, average="macro")
    if metric.startswith("f1_weighted"):
        return lambda y_t, y_p: f1_score(y_t, y_p, average="weighted")
    if metric.startswith("f1"):
        return lambda y_t, y_p: f1_score(y_t, y_p, average="binary")
    return lambda y_t, y_p: accuracy_score(y_t, y_p)


def main() -> None:
    ap = argparse.ArgumentParser(description="Container-safe text classification (F3 sandbox).")
    ap.add_argument("--data-dir", default=os.environ.get("AGENT_DATA_DIR", "/data"),
                    help="directory (read-only mount) containing train.csv")
    ap.add_argument("--text-col", default="text")
    ap.add_argument("--label-col", default="label")
    ap.add_argument("--model", default="logreg", choices=("logreg", "svm"))
    ap.add_argument("--eval-metric", default="f1_macro")
    ap.add_argument("--op", default=None, choices=("le", "ge"))
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--test-size", type=float, default=0.2)
    ap.add_argument("--sample", type=int, default=0,
                    help="subsample N rows for a quick smoke run (0 = all)")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    preset = "text_cls"
    threshold = float(args.threshold)
    metric = args.eval_metric
    op = args.op or ("le" if metric.endswith(("_loss",)) else "ge")

    # isolation proof (recorded BEFORE any training)
    isolation = {
        "data_readonly": probe_readonly(args.data_dir),
        "network_blocked": probe_network_blocked(),
    }

    train_path = os.path.join(args.data_dir, "train.csv")
    if not os.path.exists(train_path):
        raise FileNotFoundError(f"need train.csv at {train_path}")

    df = pd.read_csv(train_path)
    if args.sample and args.sample > 0:
        df = df.sample(n=min(args.sample, len(df)), random_state=42).reset_index(drop=True)
    texts = df[args.text_col].astype(str).tolist()
    labels = df[args.label_col].astype(str).tolist()
    n_classes = len(set(labels))

    X_tr, X_te, y_tr, y_te = train_test_split(
        texts, labels, test_size=args.test_size, random_state=42, stratify=labels
    )

    model = _build_model(args.model)
    model.fit(X_tr, y_tr)
    y_pred = model.predict(X_te)

    scorer = _scorer(metric)
    primary = float(scorer(y_te, y_pred))
    acc = float(accuracy_score(y_te, y_pred))
    f1m = float(f1_score(y_te, y_pred, average="macro"))

    passed = (primary <= threshold) if op == "le" else (primary >= threshold)

    metrics: dict[str, float] = {
        "primary": round(primary, 4),
        "accuracy": round(acc, 4),
        "f1_macro": round(f1m, 4),
        "n_classes": float(n_classes),
        "n_train": float(len(y_tr)),
        "n_test": float(len(y_te)),
    }
    if metric not in metrics:
        metrics[metric] = round(primary, 4)

    result = {
        "preset": preset,
        "model": args.model,
        "fe": "tfidf",
        "eval_metric": metric,
        "threshold": threshold,
        "op": op,
        "passed": bool(passed),
        "gate_passed": bool(passed),
        "metrics": metrics,
        "isolation": isolation,
        "report_ref": f"text-cls://sandbox-{args.model}-{n_classes}cls",
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name or "result.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
