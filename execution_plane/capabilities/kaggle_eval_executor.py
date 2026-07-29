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
        from sklearn.metrics import f1_score, make_scorer
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

        train_path = os.path.join(data_dir, "train.csv")
        if not os.path.exists(train_path):
            raise FileNotFoundError(
                f"Kaggle eval needs train.csv at {train_path} "
                f"(pass params['data_dir'] or download the competition data)"
            )
        df = pd.read_csv(train_path)

        fe = (params.get("fe") or "basic").lower()
        X, y = self._build_xy(df, target, preset, drop_cols, fe)

        # --- auto split numeric / categorical columns ---
        num_features = [c for c in X.columns if pd.api.types.is_numeric_dtype(X[c])]
        cat_features = [c for c in X.columns if not pd.api.types.is_numeric_dtype(X[c])]

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

        scores = cross_val_score(model, X, y, cv=cv_folds, scoring="accuracy", n_jobs=-1)
        accuracy = float(np.mean(scores))
        std = float(np.std(scores))
        f1 = float(
            np.mean(
                cross_val_score(
                    model,
                    X,
                    y,
                    cv=cv_folds,
                    scoring=make_scorer(f1_score, average="macro"),
                    n_jobs=-1,
                )
            )
        )

        run = sdk.load_object(f"run:{stage_run.run_id}")
        obj = run.objective_snapshot or {}
        threshold = float(
            params.get("threshold")
            if params.get("threshold") is not None
            else obj.get("target_threshold", preset_cfg.get("threshold", 0.82))
        )
        op = params.get("op") or obj.get("op", "ge")
        passed = (accuracy <= threshold) if op == "le" else (accuracy >= threshold)
        gate_passed = passed

        metrics = {
            "accuracy": round(accuracy, 4),
            "accuracy_std": round(std, 4),
            "f1_macro": round(f1, 4),
            "cv_folds": cv_folds,
        }

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
                f"Kaggle eval [{preset}/{model_name}]: accuracy={accuracy:.4f}±{std:.4f}, "
                f"f1={f1:.4f} vs {op} {threshold} -> {'PASS' if passed else 'FAIL'}"
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

        y = df[target]
        if y.dtype == bool:
            y = y.astype(int)
        else:
            y = pd.to_numeric(y, errors="coerce")
        X = df.drop(columns=[target])
        return X, y
