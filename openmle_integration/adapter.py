"""OpenMLETaskAdapter -- align safety_auto_research's verifiable tabular env with dojo's Task.

This is the Phase A seam: it implements the local ``Task`` contract (mirroring
``dojo.core.tasks.base.Task``) by wrapping safety's existing tabular sklearn pipeline
(the same logic family as ``execution_plane/capabilities/kaggle_eval_executor.py``), so a
titanic run can flow through ``prepare -> step_task -> evaluate_fitness`` exactly as the
OpenMLE-Evo solver would drive it.

Two action modes are supported (both faithful to dojo's "action = agent program" model):
  * ``action`` is a **str**  -> executed as a python program via ``PythonInterpreter``;
    the program must write ``submission.csv`` (columns: ``sample_id``, ``<target>``).
  * ``action`` is a **dict** -> a built-in sklearn pipeline is run directly (no LLM),
    useful for deterministic interface-alignment tests and as the cheap config-evolution
    path that already exists in ``control_plane/evolution.py``.

The task holds out an eval split at ``prepare`` time and scores predictions against the
true labels -- a genuine *verifiable task environment*, the local analogue of dojo's
``MLEBenchTask.step_task`` + ``evaluate_submission``.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler, PolynomialFeatures
from sklearn.impute import SimpleImputer

from .contracts import Task, TEST_FITNESS, VALID_SOLUTION, VALID_SOLUTION_FEEDBACK, AUX_EVAL_INFO, MetricValue
from .interpreter import PythonInterpreter


from dataclasses import dataclass, field  # noqa: E402


@dataclass
class OpenMLETaskConfig:
    """Lightweight analogue of dojo's ``TaskConfig`` for the adapter (no omegaconf)."""

    name: str = "openmle-task"
    benchmark: str = "tabular"
    data_dir: str = ""
    target: str = "Survived"
    id_col: str = "PassengerId"
    eval_metric: str = "accuracy"
    direction: str = "higher"  # higher | lower
    threshold: float = 0.8
    cv_folds: int = 5
    test_size: float = 0.2
    random_state: int = 42


_MODEL_REGISTRY = {
    "logreg": LogisticRegression,
    "rf": RandomForestClassifier,
    "gbm": GradientBoostingClassifier,
}


class OpenMLETaskAdapter(Task):
    """Verifiable tabular task implementing the dojo ``Task`` contract."""

    def __init__(self, cfg: OpenMLETaskConfig, interpreter: Optional[PythonInterpreter] = None) -> None:
        super().__init__(cfg)
        self.cfg = cfg
        self._interpreter = interpreter or PythonInterpreter()
        self._state: Dict[str, Any] = {}

    # -- prepare -----------------------------------------------------------
    def prepare(self, **task_args: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        data_dir = self.cfg.data_dir or task_args.get("data_dir", "")
        train_path = os.path.join(data_dir, "train.csv")
        df = pd.read_csv(train_path)
        target = self.cfg.target
        id_col = self.cfg.id_col

        # P2-4 fix: numeric target labels pass through unchanged; string / object
        # (categorical) labels are label-encoded into stable ints so the sklearn
        # pipelines and the int-based submission comparison work. The encoded target
        # is also written into train.csv, so operator programs train/predict in the
        # same code space (previously `astype(int)` on object dtype raised ValueError).
        if not pd.api.types.is_numeric_dtype(df[target]):
            from sklearn.preprocessing import LabelEncoder

            le = LabelEncoder()
            df = df.assign(**{target: le.fit_transform(df[target].astype(str))})

        # Build an 80/20 fit/eval split; eval ground truth is held by the task.
        rng = np.random.default_rng(self.cfg.random_state)
        idx = np.arange(len(df))
        rng.shuffle(idx)
        n_eval = max(1, int(len(df) * self.cfg.test_size))
        eval_idx = idx[:n_eval]
        fit_idx = idx[n_eval:]

        fit_df = df.iloc[fit_idx].reset_index(drop=True)
        eval_df = df.iloc[eval_idx].reset_index(drop=True)

        # Write the FIT portion as the program's input train.csv, and the EVAL
        # portion (features only, no target) as eval.csv so an operator program can
        # predict the held-out rows -- mirroring dojo's train/public vs private split.
        work_train = os.path.join(self._interpreter.workdir, "train.csv")
        fit_df.to_csv(work_train, index=False)
        work_eval = os.path.join(self._interpreter.workdir, "eval.csv")
        eval_df.drop(columns=[target]).to_csv(work_eval, index=False)

        lower_is_better = self.cfg.direction == "lower"
        task_info = {
            "TASK_DESCRIPTION": (
                f"Train a classifier predicting `{target}` from the provided tabular data. "
                f"Write predictions to `submission.csv` with columns `{id_col}` and `{target}`. "
                f"Metric: {self.cfg.eval_metric} ({'higher is better' if not lower_is_better else 'lower is better'})."
            ),
            "lower_is_better": lower_is_better,
            "eval_metric": self.cfg.eval_metric,
            "target": target,
            "id_col": id_col,
            "feature_cols": [c for c in df.columns if c not in (target, id_col)],
            "work_train": work_train,
        }

        self._state = {
            "data_dir": data_dir,
            "work_train": work_train,
            "eval_df": eval_df,
            "eval_truth": eval_df[target].astype(int if df[target].dtype != float else float).values,
            "target": target,
            "id_col": id_col,
            "last_eval": None,
        }
        return self._state, task_info

    # -- step_task ---------------------------------------------------------
    def step_task(self, state: Dict[str, Any], action: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if isinstance(action, str):
            return self._step_code(state, action)
        if isinstance(action, dict):
            return self._step_config(state, action)
        raise TypeError(f"action must be str (program) or dict (config), got {type(action)}")

    def _step_code(self, state: Dict[str, Any], code: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        result = self._interpreter.run(code, file_name="solution.py")
        sub_path = os.path.join(self._interpreter.workdir, "submission.csv")
        if not os.path.exists(sub_path):
            outcome = {
                TEST_FITNESS: None,
                VALID_SOLUTION: False,
                VALID_SOLUTION_FEEDBACK: (
                    f"no submission.csv produced (exit_code={result.exit_code}); "
                    f"stderr tail: {result.term_out[-500:]}"
                ),
                AUX_EVAL_INFO: {"exit_code": result.exit_code, "timed_out": result.timed_out},
            }
            state["last_eval"] = outcome
            return state, outcome
        return self._score_submission(state, sub_path, exit_code=result.exit_code)

    def _step_config(self, state: Dict[str, Any], config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Run the built-in sklearn pipeline directly (no LLM) -- cheap config-evolution path."""
        model_name = config.get("model", "rf")
        if model_name not in _MODEL_REGISTRY:
            raise ValueError(
                f"unknown model {model_name!r}; expected one of {sorted(_MODEL_REGISTRY)}"
            )
        fe = config.get("fe", "basic")
        cv_folds = int(config.get("cv_folds", self.cfg.cv_folds))
        fit_df = pd.read_csv(state["work_train"])
        target = state["target"]
        y = fit_df[target]
        X = fit_df.drop(columns=[target, state["id_col"]]) if state["id_col"] in fit_df.columns else fit_df.drop(columns=[target])
        pre = self._build_preprocessor(X, fe=fe)
        model = _MODEL_REGISTRY.get(model_name, RandomForestClassifier)(
            random_state=self.cfg.random_state, n_estimators=200
        ) if model_name in ("rf", "gbm") else _MODEL_REGISTRY.get(model_name, LogisticRegression)(max_iter=1000)
        pipe = Pipeline([("pre", pre), ("clf", model)])
        scores = cross_val_score(pipe, X, y, cv=min(cv_folds, max(2, len(X) // 3)), scoring="accuracy")
        primary = float(scores.mean())
        # Produce a submission on the eval split for a held-out check.
        pipe.fit(X, y)
        eval_df = state["eval_df"]
        X_eval = eval_df.drop(columns=[target]) if target in eval_df.columns else eval_df
        preds = pipe.predict(X_eval)
        sub_path = os.path.join(self._interpreter.workdir, "submission.csv")
        pd.DataFrame({state["id_col"]: eval_df[state["id_col"]].values, target: preds}).to_csv(sub_path, index=False)
        state["cv_accuracy"] = primary
        return self._score_submission(state, sub_path, cv_accuracy=primary)

    # -- evaluate_fitness --------------------------------------------------
    def evaluate_fitness(
        self,
        solution: Optional[Any] = None,
        state: Optional[Dict[str, Any]] = None,
        interpreter: Optional[Any] = None,
        aux_info: Optional[Dict[str, Any]] = None,
    ) -> Any:
        st = state if state is not None else self._state
        if solution is not None and isinstance(solution, str):
            _, outcome = self._step_code(st, solution)
            return outcome
        if st.get("last_eval") is not None:
            return st["last_eval"]
        # Fallback: score the current submission.csv if present.
        sub_path = os.path.join(self._interpreter.workdir, "submission.csv")
        if os.path.exists(sub_path):
            return self._score_submission(st, sub_path)
        return {TEST_FITNESS: None, VALID_SOLUTION: False,
                VALID_SOLUTION_FEEDBACK: "no solution evaluated yet"}

    # -- close -------------------------------------------------------------
    def close(self, state: Dict[str, Any]) -> None:
        try:
            self._interpreter.cleanup()
        except Exception:
            pass

    # -- helpers -----------------------------------------------------------
    def _score_submission(
        self, state: Dict[str, Any], sub_path: str, exit_code: int = 0, cv_accuracy: Optional[float] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        try:
            sub = pd.read_csv(sub_path)
        except Exception as exc:  # noqa: BLE001
            outcome = {
                TEST_FITNESS: None, VALID_SOLUTION: False,
                VALID_SOLUTION_FEEDBACK: f"cannot read submission.csv: {exc}",
                AUX_EVAL_INFO: {"exit_code": exit_code},
            }
            state["last_eval"] = outcome
            return state, outcome

        target = state["target"]
        id_col = state["id_col"]
        eval_df = state["eval_df"]
        truth = state["eval_truth"]
        # Align on the eval-row id column.
        outcome = None
        if id_col in sub.columns and id_col in eval_df.columns:
            merged = eval_df[[id_col, target]].merge(sub[[id_col, target]], on=id_col, suffixes=("", "_pred"))
            if len(merged) == 0:
                outcome = {
                    TEST_FITNESS: None, VALID_SOLUTION: False,
                    VALID_SOLUTION_FEEDBACK: "no matching ids between submission and eval set",
                    AUX_EVAL_INFO: {"exit_code": exit_code},
                }
            else:
                try:
                    preds = merged[f"{target}_pred"].astype(int).values
                except (ValueError, TypeError):
                    outcome = {
                        TEST_FITNESS: None,
                        VALID_SOLUTION: False,
                        VALID_SOLUTION_FEEDBACK: "cannot convert id-aligned predictions to int (missing/NaN preds)",
                        AUX_EVAL_INFO: {"exit_code": exit_code},
                    }
        else:
            # Fallback: positional alignment — validate shape before comparing
            if target not in sub.columns:
                outcome = {
                    TEST_FITNESS: None, VALID_SOLUTION: False,
                    VALID_SOLUTION_FEEDBACK: f"submission.csv missing target column {target!r}",
                    AUX_EVAL_INFO: {"exit_code": exit_code},
                }
            else:
                raw_preds = sub[target].values
                if len(raw_preds) != len(truth):
                    outcome = {
                        TEST_FITNESS: None, VALID_SOLUTION: False,
                        VALID_SOLUTION_FEEDBACK:
                            f"row count mismatch: {len(raw_preds)} preds vs {len(truth)} truths",
                        AUX_EVAL_INFO: {"exit_code": exit_code},
                    }
                else:
                    try:
                        preds = raw_preds.astype(int)
                    except (ValueError, TypeError):
                        outcome = {
                            TEST_FITNESS: None, VALID_SOLUTION: False,
                            VALID_SOLUTION_FEEDBACK: "cannot convert predictions to int",
                            AUX_EVAL_INFO: {"exit_code": exit_code},
                        }
        if outcome is not None:
            state["last_eval"] = outcome
            return state, outcome
        preds = np.asarray(preds).reshape(-1)
        correct = int(np.sum(preds == truth))
        acc = correct / len(truth) if len(truth) else 0.0
        # R20 fix: the fitness used to be hard-wired to accuracy while
        # ``cfg.eval_metric`` was only echoed into AUX_EVAL_INFO — a task declared
        # on f1_macro / error_rate was in fact optimized on accuracy, and with
        # direction="lower" the adapter even reported MetricValue(accuracy,
        # maximize=False), i.e. "minimize accuracy". Compute the declared metric
        # when it is derivable from hard labels, and be explicit when it is not.
        score, metric_used, fallback, effective_dir = self._score_declared_metric(preds, truth, acc)
        declared_dir = str(self.cfg.direction or "higher").strip().lower()
        feedback = (
            f"{metric_used}={score:.4f} (accuracy={acc:.4f}, {correct}/{len(truth)})"
        )
        if fallback:
            feedback += (
                f" [declared metric {self.cfg.eval_metric!r} cannot be recomputed from"
                f" hard labels in submission.csv — proxied by {metric_used}]"
            )
        outcome = {
            TEST_FITNESS: score,
            VALID_SOLUTION: True,
            VALID_SOLUTION_FEEDBACK: feedback,
            AUX_EVAL_INFO: {
                "exit_code": exit_code,
                "cv_accuracy": cv_accuracy,
                "accuracy": acc,
                "metric": metric_used,
                "declared_metric": self.cfg.eval_metric,
                "metric_fallback": fallback,
                "direction": effective_dir,
                "declared_direction": declared_dir,
            },
        }
        state["last_eval"] = outcome
        state["last_metric"] = MetricValue(value=score, maximize=effective_dir != "lower")
        return state, outcome

    def _score_declared_metric(
        self, preds: "np.ndarray", truth: "np.ndarray", acc: float
    ) -> Tuple[float, str, bool, str]:
        """Score ``cfg.eval_metric`` from hard labels.

        Returns ``(score, metric_used, is_proxy, direction_of_score)``.

        Metrics that need probabilities/scores (roc_auc, log_loss, ...) cannot be
        recomputed from a submission file of hard labels. Rather than silently
        reporting accuracy under the declared metric's name, we return an honest
        proxy and flag it: ``error_rate`` for lower-is-better objectives (so the
        value really is lower-is-better and downstream ``op == "le"`` negation
        stays correct) and ``accuracy`` for higher-is-better ones.
        """
        metric = str(self.cfg.eval_metric or "accuracy").strip().lower()
        declared_dir = str(self.cfg.direction or "higher").strip().lower()

        def _proxy() -> Tuple[float, str, bool, str]:
            if declared_dir == "lower":
                return 1.0 - acc, "error_rate", True, "lower"
            return acc, "accuracy", True, "higher"

        if metric in ("", "accuracy", "acc", "cv_accuracy", "top1_accuracy"):
            return acc, "accuracy", False, "higher"
        if metric in ("error_rate", "error", "misclassification_rate"):
            return 1.0 - acc, "error_rate", False, "lower"
        try:
            from sklearn.metrics import balanced_accuracy_score
            from sklearn.metrics import f1_score
            from sklearn.metrics import precision_score
            from sklearn.metrics import recall_score

            if metric in ("f1", "f1_binary"):
                return float(f1_score(truth, preds, average="binary")), "f1", False, "higher"
            if metric in ("f1_macro", "f1_micro", "f1_weighted"):
                avg = metric.split("_", 1)[1]
                return float(f1_score(truth, preds, average=avg)), metric, False, "higher"
            if metric == "precision":
                return (
                    float(precision_score(truth, preds, average="macro", zero_division=0)),
                    metric, False, "higher",
                )
            if metric == "recall":
                return (
                    float(recall_score(truth, preds, average="macro", zero_division=0)),
                    metric, False, "higher",
                )
            if metric == "balanced_accuracy":
                return float(balanced_accuracy_score(truth, preds)), metric, False, "higher"
        except Exception:
            # sklearn edge cases (single-class truth, unseen labels) -> proxy.
            return _proxy()
        return _proxy()

    @staticmethod
    def _build_preprocessor(X: pd.DataFrame, fe: str = "basic") -> ColumnTransformer:
        fe = (fe or "basic").lower()
        num = X.select_dtypes(include=[np.number]).columns.tolist()
        cat = X.select_dtypes(exclude=[np.number]).columns.tolist()
        transformers = []
        # Impute missing values (titanic has NaNs in Age/Embarked) then scale/encode.
        # "rich" FE adds degree-2 polynomial/interaction terms on the numeric block (M5).
        if num:
            if fe == "rich":
                num_steps = [
                    ("imp", SimpleImputer(strategy="median")),
                    ("poly", PolynomialFeatures(degree=2, include_bias=False)),
                    ("sc", StandardScaler()),
                ]
            else:
                num_steps = [
                    ("imp", SimpleImputer(strategy="median")),
                    ("sc", StandardScaler()),
                ]
            transformers.append(("num", Pipeline(num_steps), num))
        if cat:
            transformers.append(
                ("cat", Pipeline([("imp", SimpleImputer(strategy="most_frequent")),
                                  ("oh", OneHotEncoder(handle_unknown="ignore"))]), cat)
            )
        return ColumnTransformer(transformers)
