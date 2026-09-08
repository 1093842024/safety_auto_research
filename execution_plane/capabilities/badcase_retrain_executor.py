"""B Flywheel minimal closed loop — one badcase-retrain iteration with a regression gate.

Frozen-architecture data flywheel. The model *family* is frozen (the "冻结方案"); only the
training data changes between iterations. One iteration of the loop:

  1. collect         — read a badcase CSV (already-labeled hard negatives from online eval /
                       red-team / human feedback; *auto-labeling* is a Phase-2 refinement).
  2. replay retrain  — concatenate the badcase with a replayed slice of the original data
                       (adaptive ``badcase : original`` ratio, to resist catastrophic
                       forgetting) and refit the SAME frozen model family.
  3. regression gate — score the retrained model on a FROZEN original eval split; the
                       primary metric must not degrade beyond ``regression_tol`` (the hard
                       constraint). Badcase recall improvement is the objective (reward).

Emits a real ``EvalCompletedEvent`` whose ``metrics`` carry baseline vs. retrained values
plus ``regression_passed`` / ``badcase_improved`` (0.0/1.0), so the outer loop can drive
"accept new model / reject + rollback". ``passed`` = regression_passed AND badcase_improved.

This is the Phase-1 deliverable from ``doc/auto_research_task_taxonomy.md`` §七.1 — the
minimal closed loop for the B 飞轮型 task. It reuses ``KaggleEvalExecutor._build_xy`` for
feature engineering and ``derive_gate_op``'s direction convention (higher/lower) for the
regression gate.
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
from .kaggle_eval_executor import KaggleEvalExecutor
from .kaggle_eval_executor import PRESETS

# Default badcase fraction of the retrain set. Badcase are weighted up to fix failures, but
# a pure-badcase retrain forgets the original distribution — so we always replay a slice of
# the original data too. Cap at 0.9 (never retrain on badcase alone).
_DEFAULT_BADCASE_RATIO = 0.3
# Default regression tolerance (primary-metric units). 0.0 = the frozen original eval set
# must not degrade at all; a small positive epsilon tolerates CV noise.
_DEFAULT_REGRESSION_TOL = 0.0
# Only a small, deterministic metric set is supported for the regression gate. Everything
# else falls back to accuracy.
_REGRESSION_METRICS = {"accuracy", "f1_macro"}


def _make_classifier(model_name: str) -> Any:
    """Frozen classifier factory — the same families ``KaggleEvalExecutor`` offers."""
    if model_name in ("hgb", "gbm-strong", "histgb"):
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(random_state=42)
    if model_name in ("rf", "randomforest"):
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)
    if model_name in ("logreg", "lr", "logistic"):
        from sklearn.linear_model import LogisticRegression

        return LogisticRegression(max_iter=1000, random_state=42)
    from sklearn.ensemble import GradientBoostingClassifier

    return GradientBoostingClassifier(random_state=42)


def _make_pipeline(X: Any, model_name: str) -> Any:
    """Frozen preprocessing + classifier pipeline (mirrors ``kaggle_eval``)."""
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    num_features = [c for c in X.columns if pd.api.types.is_numeric_dtype(X[c])]
    cat_features = [c for c in X.columns if not pd.api.types.is_numeric_dtype(X[c])]
    num_pipe = Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())])
    _dense = model_name in ("hgb", "gbm-strong", "histgb")
    cat_pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="most_frequent")),
            ("oh", OneHotEncoder(handle_unknown="ignore", sparse_output=not _dense)),
        ]
    )
    pre = ColumnTransformer([("n", num_pipe, num_features), ("c", cat_pipe, cat_features)])
    return Pipeline([("pre", pre), ("clf", _make_classifier(model_name))])


def _score_set(model: Any, X: Any, y_true: Any) -> dict[str, float]:
    """Score a fitted model on a labeled set (accuracy + macro-F1)."""
    from sklearn.metrics import accuracy_score, f1_score

    y_pred = model.predict(X)
    acc = float(accuracy_score(y_true, y_pred))
    try:
        f1 = float(f1_score(y_true, y_pred, average="macro", zero_division=0))
    except ValueError:
        f1 = acc
    return {"accuracy": acc, "f1_macro": f1}


class BadcaseRetrainExecutor(StageExecutor):
    """One flywheel iteration: collect badcase → replay retrain → regression gate."""

    stage_codes = ("badcase_retrain", "flywheel", "flywheel_retrain")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        import numpy as np
        import pandas as pd
        from sklearn.model_selection import train_test_split

        preset = (params.get("preset") or "titanic").lower()
        preset_cfg = PRESETS.get(preset, {})
        target = params.get("target") or preset_cfg.get("target", "Survived")
        model_name = (params.get("model") or "gbm").lower()
        data_dir = params.get("data_dir") or os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "kaggle", "titanic"
        )
        drop_cols = params.get("drop_cols") or []
        fe_raw = params.get("fe")
        fe = "rich" if isinstance(fe_raw, bool) and fe_raw else str(fe_raw or "basic").lower()
        badcase_ratio = min(max(float(params.get("badcase_ratio", _DEFAULT_BADCASE_RATIO)), 0.05), 0.9)
        regression_tol = float(params.get("regression_tol", _DEFAULT_REGRESSION_TOL))
        eval_metric = (params.get("eval_metric") or "accuracy").lower()
        if eval_metric not in _REGRESSION_METRICS:
            eval_metric = "accuracy"
        heldout_frac = min(max(float(params.get("heldout_frac", 0.3)), 0.1), 0.5)
        heldout_seed = int(params.get("heldout_seed", 42))

        badcase_path = params.get("badcase_path")
        if not badcase_path:
            raise ValueError("飞轮重训需要 params['badcase_path']（已标注的 badcase CSV）")
        train_path = os.path.join(data_dir, "train.csv")
        if not os.path.exists(train_path):
            raise FileNotFoundError(
                f"飞轮重训需要原始训练集 train.csv 位于 {train_path}（pass params['data_dir']）"
            )
        if not os.path.exists(badcase_path):
            raise FileNotFoundError(f"badcase CSV 不存在: {badcase_path}")

        df = pd.read_csv(train_path)
        bc = pd.read_csv(badcase_path)

        # Feature engineering is shared with the tuning path — the "frozen 方案" inherits
        # the exact same fe so the retrained model is comparable to the frozen baseline.
        X, y = KaggleEvalExecutor._build_xy(df, target, preset, drop_cols, fe)
        Xb, yb = KaggleEvalExecutor._build_xy(bc, target, preset, drop_cols, fe)
        # Align badcase columns to the original schema (fill missing fe cols with NaN,
        # drop any extra cols the badcase file may carry).
        Xb = Xb.reindex(columns=X.columns)
        yb = yb.loc[Xb.index]

        if len(Xb) == 0:
            raise ValueError("badcase CSV 为空，无法触发飞轮重训")

        # Frozen original eval split — the regression gate's reference set. This never
        # changes across iterations, so "not degrade" is measured on a stable basis.
        _strat = y if pd.Series(y).value_counts().min() >= 2 else None
        X_fit, X_eval, y_fit, y_eval = train_test_split(
            X, y, test_size=heldout_frac, random_state=heldout_seed, stratify=_strat
        )

        # Objective snapshot for direction-aware gate (higher vs. lower is better).
        run = sdk.load_object(f"run:{stage_run.run_id}")
        obj = run.objective_snapshot or {}
        direction = str(obj.get("direction") or params.get("direction") or "higher").strip().lower()

        # ---- baseline (frozen model on original data only) ----
        baseline = _make_pipeline(X_fit, model_name)
        baseline.fit(X_fit, y_fit)
        base_orig = _score_set(baseline, X_eval, y_eval)
        base_bc_acc = _score_set(baseline, Xb, yb)["accuracy"]

        # ---- replay retrain: badcase + adaptive original replay ----
        n_bc = len(Xb)
        n_replay = int(n_bc * (1.0 - badcase_ratio) / badcase_ratio)
        if n_replay > 0:
            replace = n_replay > len(X_fit)
            idx = X_fit.sample(n=n_replay, replace=replace, random_state=heldout_seed).index
            Xr = pd.concat([Xb, X_fit.loc[idx]], axis=0)
            yr = pd.concat([yb, y_fit.loc[idx]], axis=0)
        else:
            Xr, yr = Xb, yb

        retrained = _make_pipeline(Xr, model_name)
        retrained.fit(Xr, yr)
        ret_orig = _score_set(retrained, X_eval, y_eval)
        ret_bc_acc = _score_set(retrained, Xb, yb)["accuracy"]

        base_primary = base_orig[eval_metric]
        ret_primary = ret_orig[eval_metric]
        regression_passed = self._regression_ok(base_primary, ret_primary, regression_tol, direction)
        badcase_improved = bool(ret_bc_acc > base_bc_acc + 1e-9)
        passed = bool(regression_passed and badcase_improved)

        metrics = {
            "primary": round(ret_primary, 4),
            "baseline_primary": round(base_primary, 4),
            "retrained_primary": round(ret_primary, 4),
            "baseline_badcase_acc": round(base_bc_acc, 4),
            "retrained_badcase_acc": round(ret_bc_acc, 4),
            "badcase_count": float(n_bc),
            "badcase_ratio": round(badcase_ratio, 4),
            "regression_passed": 1.0 if regression_passed else 0.0,
            "badcase_improved": 1.0 if badcase_improved else 0.0,
        }

        eval_suite_id = params.get("eval_suite_id", f"flywheel-{preset}-{target}")
        report_ref = f"flywheel://{stage_run.stage_run_id}"
        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=eval_suite_id,
            stage_run_id=stage_run.stage_run_id,
            passed=passed,
            metrics=metrics,
            gate_passed=passed,
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
                "suite": "flywheel",
                "preset": preset,
                "model": model_name,
                "eval_metric": eval_metric,
                "regression_passed": regression_passed,
                "badcase_improved": badcase_improved,
                "baseline_primary": round(base_primary, 4),
                "retrained_primary": round(ret_primary, 4),
                "baseline_badcase_acc": round(base_bc_acc, 4),
                "retrained_badcase_acc": round(ret_bc_acc, 4),
            },
        )
        sdk.record_metric(stage_run.run_id, "flywheel.badcase_acc", round(ret_bc_acc, 4), tags={"suite": "flywheel"})
        sdk.record_metric(stage_run.run_id, f"flywheel.original_{eval_metric}", round(ret_primary, 4), tags={"suite": "flywheel"})

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"Flywheel [{preset}/{model_name}]: badcase={n_bc} "
                f"badcase_acc {base_bc_acc:.4f}->{ret_bc_acc:.4f}, "
                f"{eval_metric} {base_primary:.4f}->{ret_primary:.4f}, "
                f"regression={'OK' if regression_passed else 'DEGRADED'} "
                f"-> {'ACCEPT' if passed else 'REJECT'}"
            ),
        )

    @staticmethod
    def _regression_ok(baseline: float, retrained: float, tol: float, direction: str) -> bool:
        """Hard constraint: the frozen original eval set must not degrade.

        ``direction == "lower"`` means lower is better (e.g. log_loss) — degradation is
        ``retrained > baseline + tol``. Otherwise higher is better — degradation is
        ``retrained < baseline - tol``.
        """
        if direction == "lower":
            return retrained <= baseline + tol
        return retrained >= baseline - tol
