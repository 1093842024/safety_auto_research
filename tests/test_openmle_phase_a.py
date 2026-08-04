"""Phase A integration test: OpenMLETaskAdapter aligns safety_auto_research's tabular
env with the dojo ``Task`` contract, using the local titanic dataset.

Run from the workspace root (so ``safety_auto_research`` is importable as a package):
    python -m pytest safety_auto_research/tests/test_openmle_phase_a.py -q
"""

import os
import sys
import unittest

# Make the ``safety_auto_research`` package importable from this test regardless of CWD,
# by walking up to the workspace ROOT (the dir that contains ``safety_auto_research/``)
# and inserting it on sys.path. This avoids both directory-depth math and stale submodule
# bindings that arise when the package directory itself is added to sys.path.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_workspace_root(start: str) -> str:
    cur = start
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "safety_auto_research", "data", "kaggle", "titanic")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.dirname(os.path.dirname(_HERE))  # best-effort fallback


_ROOT = _find_workspace_root(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from safety_auto_research.openmle_integration.adapter import OpenMLETaskAdapter, OpenMLETaskConfig  # noqa: E402
from safety_auto_research.openmle_integration.contracts import (  # noqa: E402
    AUX_EVAL_INFO,
    TEST_FITNESS,
    VALID_SOLUTION,
    Task,
)

# Locate the titanic dataset robustly by walking up from this file.
_TITANIC_DIR = os.path.join(_ROOT, "safety_auto_research", "data", "kaggle", "titanic")



# A reference sklearn program (the kind an "operator" would emit). It trains on
# train.csv (fit split) and predicts eval.csv (held-out rows), writing submission.csv
# with id_col + target -- exactly the dojo agent contract.
_REFERENCE_PROGRAM = '''
import os
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.ensemble import RandomForestClassifier

wd = os.environ["OPENMLE_WORKDIR"]
df = pd.read_csv(os.path.join(wd, "train.csv"))
target = "Survived"
id_col = "PassengerId"
y = df[target]
X = df.drop(columns=[target, id_col])
num = X.select_dtypes(include=[np.number]).columns.tolist()
cat = X.select_dtypes(exclude=[np.number]).columns.tolist()
pre = ColumnTransformer([
    ("n", Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]), num),
    ("c", Pipeline([("imp", SimpleImputer(strategy="most_frequent")),
                    ("oh", OneHotEncoder(handle_unknown="ignore"))]), cat),
])
clf = Pipeline([("pre", pre), ("clf", RandomForestClassifier(n_estimators=200, random_state=42))])
clf.fit(X, y)
ev = pd.read_csv(os.path.join(wd, "eval.csv"))
Xev = ev.drop(columns=[id_col]) if id_col in ev.columns else ev
preds = clf.predict(Xev)
sub = pd.DataFrame({id_col: ev[id_col].values, target: preds.astype(int)})
sub.to_csv(os.path.join(wd, "submission.csv"), index=False)
print("done")
'''


@unittest.skipUnless(os.path.isdir(_TITANIC_DIR), "titanic dataset not present")
class OpenMLEPhaseATest(unittest.TestCase):
    def _make_task(self) -> OpenMLETaskAdapter:
        cfg = OpenMLETaskConfig(
            name="titanic",
            data_dir=_TITANIC_DIR,
            target="Survived",
            id_col="PassengerId",
            eval_metric="accuracy",
            direction="higher",
            threshold=0.8,
        )
        return OpenMLETaskAdapter(cfg)

    def test_task_is_subclass_of_dojo_task(self) -> None:
        # The adapter must satisfy the dojo Task contract (interface alignment).
        self.assertTrue(issubclass(OpenMLETaskAdapter, Task))

    def test_prepare_returns_state_and_task_info(self) -> None:
        task = self._make_task()
        state, info = task.prepare()
        self.assertIn("TASK_DESCRIPTION", info)
        self.assertIn("lower_is_better", info)
        self.assertFalse(info["lower_is_better"])  # accuracy: higher is better
        self.assertIn("eval_truth", state)
        self.assertEqual(len(state["eval_truth"]), int(round(0.2 * _count_train(_TITANIC_DIR))))
        task.close(state)

    def test_step_task_code_string_runs_end_to_end(self) -> None:
        task = self._make_task()
        state, info = task.prepare()
        new_state, outcome = task.step_task(state, _REFERENCE_PROGRAM)
        self.assertTrue(outcome[VALID_SOLUTION])
        score = outcome[TEST_FITNESS]
        self.assertIsNotNone(score)
        # RandomForest on titanic should clearly beat the 0.5 majority baseline.
        self.assertGreater(score, 0.5)
        # evaluate_fitness returns the same outcome for the state.
        fit = task.evaluate_fitness(state=new_state)
        self.assertEqual(fit[TEST_FITNESS], score)
        task.close(new_state)

    def test_step_task_config_dict_runs_builtin_pipeline(self) -> None:
        task = self._make_task()
        state, info = task.prepare()
        new_state, outcome = task.step_task(state, {"model": "rf", "fe": "basic", "cv_folds": 5})
        self.assertTrue(outcome[VALID_SOLUTION])
        self.assertIsNotNone(outcome[TEST_FITNESS])
        self.assertGreater(outcome[TEST_FITNESS], 0.5)
        aux = outcome.get(AUX_EVAL_INFO, {})
        self.assertIn("cv_accuracy", aux)
        task.close(new_state)

    def test_invalid_program_yields_valid_solution_false(self) -> None:
        task = self._make_task()
        state, info = task.prepare()
        bad_code = "raise RuntimeError('boom')"
        new_state, outcome = task.step_task(state, bad_code)
        self.assertFalse(outcome[VALID_SOLUTION])
        self.assertIsNone(outcome[TEST_FITNESS])
        task.close(new_state)


def _count_train(titanic_dir: str) -> int:
    import pandas as pd

    return len(pd.read_csv(os.path.join(titanic_dir, "train.csv")))


if __name__ == "__main__":
    unittest.main(verbose=2)
