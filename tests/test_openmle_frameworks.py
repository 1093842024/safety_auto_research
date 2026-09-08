"""Tests for the open-domain framework extension of TemplateOperatorBackend (Phase A-2).

The offline operator backend is no longer sklearn-only: its Draft model space now spans
xgboost / lightgbm / torch (in addition to the curated sklearn templates), with each
open-domain program wrapped in an ``ImportError`` fallback to an sklearn RandomForest so
the seed population stays runnable in any sandbox. These tests cover:

  * pool structure (sklearn templates always lead, ``variant=0`` == RF baseline);
  * probe-driven availability (missing frameworks are skipped at generation time);
  * auto-degradation in the generated source (``except ImportError`` -> sklearn);
  * end-to-end: each open-domain template executes to a valid submission.

Deterministic assertions do NOT depend on which frameworks happen to be installed; the
probe is monkeypatched where a fixed environment is required.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from safety_auto_research.openmle_integration.adapter import (
    OpenMLETaskAdapter,
    OpenMLETaskConfig,
)
from safety_auto_research.openmle_integration.contracts import (
    TEST_FITNESS,
    VALID_SOLUTION,
    VALID_SOLUTION_FEEDBACK,
)
from safety_auto_research.openmle_integration.operators import (
    TemplateOperatorBackend,
    _draft_pool,
    _lightgbm_draft,
    _probe_frameworks,
    _torch_draft,
    _xgboost_draft,
    draft_program,
    draft_template_count,
)


def _find_titanic() -> str:
    here = os.path.abspath(__file__)
    cur = here
    for _ in range(8):
        cand = os.path.join(os.path.dirname(cur), "data", "kaggle", "titanic")
        if os.path.isdir(cand):
            return cand
        cur = os.path.dirname(cur)
    raise FileNotFoundError("titanic dataset not found")


_DATA = _find_titanic()
_BACKEND = TemplateOperatorBackend()
_CFG = OpenMLETaskConfig(name="titanic", data_dir=_DATA, target="Survived", id_col="PassengerId")


class PoolStructureTest(unittest.TestCase):
    def test_probe_returns_only_importable_frameworks(self) -> None:
        avail = _probe_frameworks()
        self.assertIsInstance(avail, frozenset)
        # whatever the env, the probe must never claim a framework that can't be imported.
        import importlib.util

        for key in avail:
            mod = {
                "torch": "torch",
                "xgboost": "xgboost",
                "lightgbm": "lightgbm",
            }[key]
            self.assertIsNotNone(importlib.util.find_spec(mod), f"{key} claimed but not importable")

    def test_sklearn_templates_always_lead(self) -> None:
        pool = _draft_pool()
        keys = [k for k, _ in pool]
        self.assertEqual(
            keys[:4],
            ["sklearn_rf", "sklearn_gbm", "sklearn_rf_shallow", "sklearn_logreg"],
        )
        self.assertGreaterEqual(len(pool), 4)

    def test_variant_zero_is_rf_baseline(self) -> None:
        code = draft_program(
            _BACKEND, target="Survived", id_col="PassengerId", task_description="x",
            variant=0, caller_stage="inner_program_evolution",
        )
        self.assertIn("RandomForestClassifier(n_estimators=200, random_state=42)", code)
        self.assertNotIn("import torch", code)
        self.assertNotIn("import xgboost", code)
        self.assertNotIn("import lightgbm", code)

    def test_draft_template_count_matches_pool(self) -> None:
        self.assertEqual(draft_template_count(), len(_draft_pool()))

    def test_all_frameworks_added_when_available(self) -> None:
        # Force the probe to report every framework available -> pool grows accordingly.
        with mock.patch(
            "safety_auto_research.openmle_integration.operators._probe_frameworks",
            return_value=frozenset({"torch", "xgboost", "lightgbm"}),
        ):
            pool = _draft_pool()
            keys = {k for k, _ in pool}
            self.assertTrue({"torch_mlp", "xgboost", "lightgbm"}.issubset(keys))
            self.assertEqual(draft_template_count(), 7)


class AutoDegradationTest(unittest.TestCase):
    def test_each_open_domain_template_degrades_to_sklearn(self) -> None:
        # The generated source must carry a literal ImportError fallback to sklearn so the
        # program stays runnable even when the framework import fails at run time.
        for fn, import_line in (
            (_xgboost_draft, "import xgboost as xgb"),
            (_lightgbm_draft, "import lightgbm as lgb"),
            (_torch_draft, "import torch"),
        ):
            code = fn("Survived", "PassengerId")
            self.assertIn(import_line, code)
            self.assertIn("except ImportError:", code)
            self.assertIn("RandomForestClassifier(n_estimators=200, random_state=42)", code)

    def test_open_domain_templates_run_to_valid_submission(self) -> None:
        # End-to-end: whether the framework is installed (runs the framework) or absent
        # (falls back to sklearn), each template must still produce a valid submission
        # with fitness above the titanic baseline.
        adapter = OpenMLETaskAdapter(_CFG)
        state, _info = adapter.prepare()
        try:
            for fn in (_xgboost_draft, _lightgbm_draft, _torch_draft):
                code = fn("Survived", "PassengerId")
                _s, outcome = adapter.step_task(state, code)
                self.assertTrue(
                    outcome[VALID_SOLUTION],
                    f"{fn.__name__} should produce a valid submission: "
                    f"{outcome.get(VALID_SOLUTION_FEEDBACK)}",
                )
                self.assertGreater(outcome[TEST_FITNESS], 0.5)
        finally:
            adapter.close(state)


if __name__ == "__main__":
    unittest.main()
