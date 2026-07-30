"""Real Kaggle evaluation capability — trains an actual model and scores it.

This is the *real* counterpart to the deterministic ``EvalExecutor`` mock. It loads a
Kaggle competition's training data, preprocesses it, trains a scikit-learn model, and
computes an honest cross-validated metric. The result is emitted as a real
``EvalCompletedEvent`` so the closed loop and guardrails operate on a genuine measurement
rather than a hash.

It is competition-configurable via ``params`` (``preset`` + ``target`` + ``threshold``),
so the same capability serves *any* Kaggle tabular task the agent chooses — Titanic,
Spaceship-Titanic, etc. — without code changes. Used for the end-to-end agent-driven run.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import EvalCompletedEvent
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK

# Default to the Titanic data downloaded for the demo; override via params["data_dir"].
_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "data",
    "kaggle",
    "titanic",
)

# Competition presets: feature engineering + target column + gold reference.
PRESETS = {
    "titanic": {
        "target": "Survived",
        "threshold": 0.82,
        "gold_label": "public leaderboard gold ~= 0.82 accuracy",
    },
    "spaceship": {
        "target": "Transported",
        "threshold": 0.80,
        "gold_label": "Kaggle public leaderboard gold ~= 0.80 accuracy",
    },
    "wine": {
        "target": "quality",
        "threshold": 0.78,
        "gold_label": "binary classification via flavanoids, baseline ~0.78 accuracy",
    },
    "iris": {
        "target": "species",
        "threshold": 0.90,
        "gold_label": "3-class classification, strong baseline ~0.95 accuracy",
    },
    "breast_cancer": {
        "target": "diagnosis",
        "threshold": 0.92,
        "gold_label": "binary diagnosis, strong baseline ~0.95 accuracy",
    },
}


class KaggleEvalExecutor(StageExecutor):
    """Trains a real classifier on a Kaggle-style tabular dataset and CV-scopes it."""

    stage_codes = ("kaggle_eval", "kaggle", "kaggle_competition_eval")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        # Imports kept local so the platform does not require sklearn unless this runs.
        import numpy as np
        import pandas as pd
        from sklearn.compose import ColumnTransformer
        from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import f1_score, make_scorer, roc_auc_score
        from sklearn.metrics import accuracy_score, precision_score, recall_score
        from sklearn.model_selection import cross_val_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder, StandardScaler

        preset = (params.get("preset") or "titanic").lower()
        preset_cfg = PRESETS.get(preset, {})
        data_dir = params.get("data_dir") or _DEFAULT_DATA_DIR
        target = params.get("target") or preset_cfg.get("target", "Survived")
        model_name = (params.get("model") or "gbm").lower()
        cv_folds = int(params.get("cv_folds", 5))
        drop_cols = params.get("drop_cols") or []
        # Parallel evaluation (Phase 3 evolution driver) sets n_jobs=1 per candidate to
        # avoid oversubscribing cores; single evals keep the -1 default.
        n_jobs = int(params.get("n_jobs", -1))

        train_path = os.path.join(data_dir, "train.csv")
        if not os.path.exists(train_path):
            raise FileNotFoundError(
                f"Kaggle eval needs train.csv at {train_path} "
                f"(pass params['data_dir'] or download the competition data)"
            )
        df = pd.read_csv(train_path)

        fe = (params.get("fe") or "basic").lower()
        X, y = self._build_xy(df, target, preset, drop_cols, fe)

        # ---- held-out split (Phase 2: evaluator hardening) ----
        # CV on the TRAIN split feeds the inner loop's feedback; the held-out split is
        # scored exactly once and reported alongside, so the OUTER audit can check the
        # generalization gap. The gate/threshold stays FROZEN (CV primary vs threshold)
        # — held-out never changes the gate, it only informs the audit.
        heldout_frac = min(max(float(params.get("heldout_frac", 0.15)), 0.0), 0.5)  # I9: clamp
        heldout_seed = int(params.get("heldout_seed", 42))
        X_fit, y_fit = X, y
        X_ho = y_ho = None
        heldout_skipped = False
        if heldout_frac > 0 and len(y) >= 50:
            from sklearn.model_selection import train_test_split

            _strat = y if pd.Series(y).value_counts().min() >= 2 else None
            X_fit, X_ho, y_fit, y_ho = train_test_split(
                X, y, test_size=heldout_frac, random_state=heldout_seed, stratify=_strat
            )
        elif heldout_frac > 0:
            heldout_skipped = True  # I9: explicitly marked, audit loses the overfit check

        cv_folds = min(cv_folds, len(y_fit) // 2, 20)
        if cv_folds < 2:
            raise ValueError(f"数据集太小（{len(y_fit)} 条），cv_folds 至少需要 2")

        # Guard: StratifiedKFold requires each class to have at least cv_folds samples.
        _min_class = int(pd.Series(y_fit).value_counts().min())
        if _min_class < 2:
            # I9 fix: cv_folds=max(2, 1) would still crash StratifiedKFold — fail loud.
            raise ValueError(
                f"最小类别只有 {_min_class} 条样本，无法分层交叉验证（每类至少 2 条）。"
                "请合并稀有类别或换用更大的数据集。"
            )
        if cv_folds > _min_class:
            cv_folds = max(2, _min_class)
            logging.warning(
                "cv_folds reduced to %d: the smallest class has only %d samples "
                "(StratifiedKFold would fail otherwise)",
                cv_folds, _min_class,
            )

        # --- auto split numeric / categorical columns ---
        num_features = [c for c in X_fit.columns if pd.api.types.is_numeric_dtype(X_fit[c])]
        cat_features = [c for c in X_fit.columns if not pd.api.types.is_numeric_dtype(X_fit[c])]

        num_pipe = Pipeline(
            [("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]
        )
        # HistGradientBoosting requires dense input; other models accept sparse.
        _dense = model_name in ("hgb", "gbm-strong", "histgb")
        cat_pipe = Pipeline(
            [
                ("imp", SimpleImputer(strategy="most_frequent")),
                ("oh", OneHotEncoder(handle_unknown="ignore", sparse_output=not _dense)),
            ]
        )
        pre = ColumnTransformer(
            [("n", num_pipe, num_features), ("c", cat_pipe, cat_features)]
        )

        if model_name in ("gbm", "gradientboosting", "gb"):
            clf = GradientBoostingClassifier(random_state=42)
        elif model_name in ("hgb", "gbm-strong", "histgb"):
            from sklearn.ensemble import HistGradientBoostingClassifier

            # Default HGB params measured strongest on spaceship (cv acc ~0.8006);
            # heavier settings (max_iter=400, lr=0.06) empirically overfit (~0.794).
            clf = HistGradientBoostingClassifier(random_state=42)
        elif model_name in ("rf", "randomforest"):
            clf = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)
        elif model_name in ("logreg", "lr", "logistic"):
            clf = LogisticRegression(max_iter=1000, random_state=42)
        else:
            clf = GradientBoostingClassifier(random_state=42)

        model = Pipeline([("pre", pre), ("clf", clf)])

        # ---- Multi-metric evaluation ----
        # Primary metric: configurable via params["eval_metric"] or objective_snapshot.
        # Defaults to "accuracy" for backward compatibility with titanic/spaceship.
        _SCORERS: dict[str, Any] = {
            "accuracy": "accuracy",
            "f1": make_scorer(f1_score, average="macro"),
            "f1_macro": make_scorer(f1_score, average="macro"),
            "precision": make_scorer(precision_score, average="macro", zero_division=0),
            "recall": make_scorer(recall_score, average="macro", zero_division=0),
            "roc_auc": make_scorer(roc_auc_score, needs_proba=True, average="macro", multi_class="ovr"),
            "log_loss": "neg_log_loss",
            "auc": make_scorer(roc_auc_score, needs_proba=True, average="macro", multi_class="ovr"),
        }
        run = sdk.load_object(f"run:{stage_run.run_id}")
        obj = run.objective_snapshot or {}
        _eval_metric = (params.get("eval_metric") or obj.get("eval_metric") or "accuracy").lower()
        # Map compound names to simple keys (e.g. "benchmark_*_accuracy" → "accuracy")
        for _k in sorted(_SCORERS, key=lambda k: -len(k)):
            if _k in _eval_metric:
                _eval_metric = _k
                break
        scorer = _SCORERS.get(_eval_metric, "accuracy")

        scores = cross_val_score(model, X_fit, y_fit, cv=cv_folds, scoring=scorer, n_jobs=n_jobs)
        # neg_log_loss needs sign flip
        if _eval_metric in ("log_loss", "neg_log_loss"):
            scores = -scores
        primary = float(np.mean(scores))
        primary_std = float(np.std(scores))
        # Always compute accuracy + F1 as secondary metrics for reporting.
        acc_scores = cross_val_score(model, X_fit, y_fit, cv=cv_folds, scoring="accuracy", n_jobs=n_jobs)
        f1_scores = cross_val_score(
            model, X_fit, y_fit, cv=cv_folds,
            scoring=make_scorer(f1_score, average="macro"), n_jobs=n_jobs,
        )
        accuracy = float(np.mean(acc_scores))
        f1 = float(np.mean(f1_scores))
        threshold = float(
            params.get("threshold")
            if params.get("threshold") is not None
            else obj.get("target_threshold", preset_cfg.get("threshold", 0.82))
        )
        op = params.get("op") or obj.get("op", "ge")
        passed = (primary <= threshold) if op == "le" else (primary >= threshold)
        gate_passed = passed

        metrics = {
            "accuracy": round(accuracy, 4),
            "accuracy_std": round(float(np.std(acc_scores)), 4),
            "f1_macro": round(f1, 4),
            "cv_folds": float(cv_folds),
            "primary": round(primary, 4),
            "primary_std": round(primary_std, 4),
        }
        # ---- held-out one-shot scoring (for the audit's generalization check) ----
        if X_ho is not None:
            model.fit(X_fit, y_fit)
            ho_pred = model.predict(X_ho)
            ho_acc = float(accuracy_score(y_ho, ho_pred))
            ho_f1 = float(f1_score(y_ho, ho_pred, average="macro"))
            metrics["heldout_accuracy"] = round(ho_acc, 4)
            metrics["heldout_f1_macro"] = round(ho_f1, 4)
            metrics["generalization_gap"] = round(accuracy - ho_acc, 4)
            metrics["heldout_frac"] = heldout_frac
        elif heldout_skipped:
            metrics["heldout_skipped"] = True
        # eval_metric is a display label, not a numeric value; carried in the event.
        _eval_metric_name = _eval_metric

        eval_suite_id = params.get("eval_suite_id", f"kaggle-{preset}-{target}")
        report_ref = f"kaggle-eval://{stage_run.stage_run_id}"

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=eval_suite_id,
            stage_run_id=stage_run.stage_run_id,
            passed=passed,
            metrics=metrics,
            gate_passed=gate_passed,
            report_ref=report_ref,
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": "eval_report",
                "uri": report_ref,
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={
                "suite": eval_suite_id,
                "gate_passed": gate_passed,
                "accuracy": round(accuracy, 4),
                "model": model_name,
                "preset": preset,
            },
        )
        sdk.record_metric(stage_run.run_id, "eval.accuracy", round(accuracy, 4), tags={"suite": eval_suite_id})
        sdk.record_metric(stage_run.run_id, "eval.f1_macro", round(f1, 4), tags={"suite": eval_suite_id})

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if gate_passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"Kaggle eval [{preset}/{model_name}]: {_eval_metric_name}={primary:.4f}±{primary_std:.4f}, "
                f"acc={accuracy:.4f}, f1={f1:.4f} vs {op} {threshold} -> {'PASS' if passed else 'FAIL'}"
            ),
        )

    # ----------------------------------------------------------- feature build
    @staticmethod
    def _build_xy(
        df: "Any", target: str, preset: str, drop_cols: list[str], fe: str = "basic"
    ) -> tuple["Any", "Any"]:
        import pandas as pd

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
                # Richer, still leakage-free features (all derived per-row / per-group
                # from the training frame only; CV folds re-split the same frame).
                cabin = df["Cabin"].astype(str).str.split("/", expand=True)
                df["CabinNum"] = pd.to_numeric(cabin[1], errors="coerce")
                df["CabinSide"] = cabin[2].fillna("U")
                spend_cols = [
                    c for c in ["RoomService", "FoodCourt", "ShoppingMall", "Spa", "VRDeck"]
                    if c in df
                ]
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
            raise ValueError(f"目标列 '{target}' 不在数据集中。可用列: {list(df.columns)}")

        # I9 fix: drop rows with a missing target first, then handle non-numeric
        # labels — pd.to_numeric(coerce) would silently turn string labels (iris
        # species, diagnosis M/B, ...) into NaN and crash downstream CV guards.
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
