#!/usr/bin/env python3
"""Standalone, container-safe Kaggle eval for the F3 Docker sandbox.

Faithfully reproduces ``execution_plane/capabilities/kaggle_eval_executor.py``
(real training — no mock) so agent-mode research runs INSIDE an isolated
container and writes a structured ``result.json`` the host can read back.

Design (see doc/design_notes.md §3):
  * Reads AGENT_DATA_DIR   (read-only mount, default /data): <preset>/train.csv
  * Writes AGENT_SCRATCH_DIR (writable mount, default /scratch): result_<preset>.json
  * Records an ISOLATION PROOF: data mount read-only + network unreachable,
    so the host can assert the execution really was confined.

Usage (run via scripts/build_agent_sandbox.sh):
  python /repo/scripts/sandbox_examples/run_kaggle_eval_sandbox.py \
      --preset titanic --model gbm --threshold 0.82
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, f1_score, make_scorer
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

# --- competition presets (mirror kaggle_eval_executor.PRESETS) ----------------
PRESETS: dict[str, dict[str, Any]] = {
    "titanic": {"target": "Survived", "threshold": 0.82, "gold_label": "public leaderboard gold ~= 0.82 accuracy"},
    "spaceship": {"target": "Transported", "threshold": 0.80, "gold_label": "Kaggle public leaderboard gold ~= 0.80 accuracy"},
    "wine": {"target": "quality", "threshold": 0.78, "gold_label": "binary classification via flavanoids, baseline ~0.78 accuracy"},
    "iris": {"target": "species", "threshold": 0.90, "gold_label": "3-class classification, strong baseline ~0.95 accuracy"},
    "breast_cancer": {"target": "diagnosis", "threshold": 0.92, "gold_label": "binary diagnosis, strong baseline ~0.95 accuracy"},
}

# Preset -> on-disk data subdir (the Kaggle folder name differs from the preset key).
DATA_SUBDIR: dict[str, str] = {"spaceship": "spaceship-titanic"}


def derive_gate_op(params: dict[str, Any], obj: dict[str, Any]) -> str:
    """Resolve gate operator (mirror kaggle_eval_executor.derive_gate_op)."""
    explicit_op = params.get("op") or obj.get("op")
    if explicit_op in ("le", "ge"):
        return explicit_op
    direction = str(obj.get("direction") or "higher").strip().lower()
    return "le" if direction == "lower" else "ge"


def _build_xy(df: pd.DataFrame, target: str, preset: str, drop_cols: list[str], fe: str):
    """Mirror kaggle_eval_executor._build_xy (leakage-free feature engineering)."""
    df = df.copy()
    if preset == "titanic":
        df["Title"] = df["Name"].str.extract(r" ([A-Za-z]+)\.", expand=False)
        df["Title"] = df["Title"].replace(
            {
                "Lady": "Rare", "Countess": "Rare", "Capt": "Rare", "Jonkheer": "Rare",
                "Sir": "Rare", "Don": "Rare", "Dona": "Rare", "Dr": "Rare", "Major": "Rare",
                "Col": "Rare", "Rev": "Rare", "Mme": "Rare", "Ms": "Rare",
            }
        )
        df["FamilySize"] = df["SibSp"] + df["Parch"] + 1
        df["IsAlone"] = (df["FamilySize"] == 1).astype(int)
        df = df.drop(columns=[c for c in ["PassengerId", "Name", "Ticket", "Cabin"] if c in df])
    elif preset == "spaceship":
        df["CabinDeck"] = df["Cabin"].astype(str).str[0].replace("nan", "U")
        if fe == "rich":
            cabin = df["Cabin"].astype(str).str.split("/", expand=True)
            df["CabinNum"] = pd.to_numeric(cabin[1], errors="coerce")
            df["CabinSide"] = cabin[2].fillna("U")
            spend_cols = [c for c in ["RoomService", "FoodCourt", "ShoppingMall", "Spa", "VRDeck"] if c in df]
            df["TotalSpend"] = df[spend_cols].fillna(0).sum(axis=1)
            df["NoSpend"] = (df["TotalSpend"] == 0).astype(int)
            group = df["PassengerId"].astype(str).str.split("_").str[0]
            df["GroupSize"] = group.map(group.value_counts())
            df["IsAlone"] = (df["GroupSize"] == 1).astype(int)
        df = df.drop(columns=[c for c in ["PassengerId", "Name", "Cabin"] if c in df])
        for c in ["CryoSleep", "VIP"]:
            if c in df:
                df[c] = df[c].astype(str)
    else:
        df = df.drop(columns=[c for c in drop_cols if c in df])

    if target not in df.columns:
        raise ValueError(f"target '{target}' not in dataset. columns: {list(df.columns)}")
    df = df[df[target].notna()]
    y = df[target]
    if y.dtype == bool:
        y = y.astype(int)
    else:
        y_num = pd.to_numeric(y, errors="coerce")
        if y_num.notna().all():
            y = y_num
        else:
            y = pd.Series(pd.factorize(y)[0], index=y.index, name=target)
    X = df.drop(columns=[target])
    return X, y


# --------------------------------------------------------------------- isolation
def probe_readonly(data_dir: str) -> bool:
    """Return True if the data mount is genuinely read-only."""
    probe = os.path.join(data_dir, ".write_test_%d" % os.getpid())
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


def probe_network_blocked() -> bool:
    """Return True if outbound network is unreachable (the --network none proof)."""
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=2):
            return False
    except OSError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preset", default="titanic", choices=list(PRESETS.keys()))
    ap.add_argument("--target", default=None, help="override target column")
    ap.add_argument("--data-dir", default=None, help="override (default AGENT_DATA_DIR/<preset>)")
    ap.add_argument("--model", default="gbm", help="gbm|logreg|rf|hgb")
    ap.add_argument("--fe", default="basic", help="basic|rich")
    ap.add_argument("--cv-folds", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--heldout-frac", type=float, default=0.15)
    ap.add_argument("--eval-metric", default="accuracy")
    ap.add_argument("--op", default=None, choices=("le", "ge"),
                    help="gate comparison op (default: 'ge' for higher-is-better metrics)")
    ap.add_argument("--result-name", default=None,
                    help="override the result json filename (default result_<preset>.json)")
    args = ap.parse_args()

    preset_cfg = PRESETS.get(args.preset, {})
    target = args.target or preset_cfg.get("target", "Survived")
    data_dir = args.data_dir or os.path.join(
        os.environ.get("AGENT_DATA_DIR", "/data"), DATA_SUBDIR.get(args.preset, args.preset)
    )
    threshold = args.threshold if args.threshold is not None else preset_cfg["threshold"]
    fe = args.fe
    model_name = args.model

    # isolation proof (recorded BEFORE any training)
    isolation = {
        "data_readonly": probe_readonly(data_dir),
        "network_blocked": probe_network_blocked(),
    }

    train_path = os.path.join(data_dir, "train.csv")
    if not os.path.exists(train_path):
        raise FileNotFoundError(f"need train.csv at {train_path}")
    df = pd.read_csv(train_path)
    X, y = _build_xy(df, target, args.preset, [], fe)

    heldout_frac = min(max(args.heldout_frac, 0.0), 0.5)
    heldout_seed = 42
    X_fit, y_fit = X, y
    X_ho = y_ho = None
    heldout_skipped = False
    if heldout_frac > 0 and len(y) >= 50:
        _strat = y if pd.Series(y).value_counts().min() >= 2 else None
        X_fit, X_ho, y_fit, y_ho = train_test_split(
            X, y, test_size=heldout_frac, random_state=heldout_seed, stratify=_strat
        )
    elif heldout_frac > 0:
        heldout_skipped = True

    cv_folds = min(args.cv_folds, len(y_fit) // 2, 20)
    if cv_folds < 2:
        raise ValueError(f"dataset too small ({len(y_fit)}) for cv_folds>=2")
    _min_class = int(pd.Series(y_fit).value_counts().min())
    if _min_class < 2:
        raise ValueError(f"smallest class has only {_min_class} samples")
    if cv_folds > _min_class:
        cv_folds = max(2, _min_class)

    num_features = [c for c in X_fit.columns if pd.api.types.is_numeric_dtype(X_fit[c])]
    cat_features = [c for c in X_fit.columns if not pd.api.types.is_numeric_dtype(X_fit[c])]
    num_pipe = Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())])
    _dense = model_name in ("hgb", "gbm-strong", "histgb")
    cat_pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="most_frequent")),
            ("oh", OneHotEncoder(handle_unknown="ignore", sparse_output=not _dense)),
        ]
    )
    pre = ColumnTransformer([("n", num_pipe, num_features), ("c", cat_pipe, cat_features)])

    if model_name in ("gbm", "gradientboosting", "gb"):
        clf = GradientBoostingClassifier(random_state=42)
    elif model_name in ("hgb", "gbm-strong", "histgb"):
        from sklearn.ensemble import HistGradientBoostingClassifier

        clf = HistGradientBoostingClassifier(random_state=42)
    elif model_name in ("rf", "randomforest"):
        clf = "RandomForestClassifier" and __import__("sklearn.ensemble", fromlist=["RandomForestClassifier"]).RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)
    elif model_name in ("logreg", "lr", "logistic"):
        clf = __import__("sklearn.linear_model", fromlist=["LogisticRegression"]).LogisticRegression(max_iter=1000, random_state=42)
    else:
        clf = GradientBoostingClassifier(random_state=42)
    model = Pipeline([("pre", pre), ("clf", clf)])

    scorer = make_scorer(f1_score, average="macro") if args.eval_metric.startswith("f1") else args.eval_metric
    scores = cross_val_score(model, X_fit, y_fit, cv=cv_folds, scoring=scorer, n_jobs=-1)
    primary = float(np.mean(scores))
    primary_std = float(np.std(scores))
    acc_scores = cross_val_score(model, X_fit, y_fit, cv=cv_folds, scoring="accuracy", n_jobs=-1)
    f1_scores = cross_val_score(model, X_fit, y_fit, cv=cv_folds, scoring=make_scorer(f1_score, average="macro"), n_jobs=-1)
    accuracy = float(np.mean(acc_scores))
    f1 = float(np.mean(f1_scores))

    op = args.op or derive_gate_op({}, {})  # explicit --op wins; else accuracy -> "ge"
    passed = (primary <= threshold) if op == "le" else (primary >= threshold)

    metrics: dict[str, float] = {
        "accuracy": round(accuracy, 4),
        "accuracy_std": round(float(np.std(acc_scores)), 4),
        "f1_macro": round(f1, 4),
        "cv_folds": float(cv_folds),
        "primary": round(primary, 4),
        "primary_std": round(primary_std, 4),
    }
    if args.eval_metric not in metrics:
        metrics[args.eval_metric] = round(primary, 4)

    if X_ho is not None:
        model.fit(X_fit, y_fit)
        ho_pred = model.predict(X_ho)
        metrics["heldout_accuracy"] = round(float(accuracy_score(y_ho, ho_pred)), 4)
        metrics["heldout_f1_macro"] = round(float(f1_score(y_ho, ho_pred, average="macro")), 4)
        metrics["generalization_gap"] = round(accuracy - float(accuracy_score(y_ho, ho_pred)), 4)
        metrics["heldout_frac"] = heldout_frac
    elif heldout_skipped:
        metrics["heldout_skipped"] = True

    result = {
        "preset": args.preset,
        "model": model_name,
        "fe": fe,
        "eval_metric": args.eval_metric,
        "threshold": threshold,
        "op": op,
        "passed": bool(passed),
        "gate_passed": bool(passed),
        "metrics": metrics,
        "isolation": isolation,
        "report_ref": f"kaggle-eval://sandbox-{args.preset}-{model_name}",
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name or f"result_{args.preset}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
