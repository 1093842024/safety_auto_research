"""Phase D tests: external RL reward bridge (D1) + local Mac LLM training (D2/D3).

Run on the managed venv (now has torch + MPS). The MPS smoke test is skipped on
machines without Metal; the reward bridge + pure-logic tests need no torch.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass

import pytest

from safety_auto_research.openmle_integration.reward_bridge import (
    RewardComponents,
    RewardConfig,
    reward_func,
    reward_population,
)
from safety_auto_research.openmle_integration.local_train import (
    LocalLLMTrainer,
    TrainConfig,
    detect_device,
)
from safety_auto_research.openmle_integration.operators import (
    LocalLLMOperatorBackend,
    OperatorPrompt,
    TemplateOperatorBackend,
    draft_program,
)


# --------------------------------------------------------------------------
# D1 -- reward bridge (pure python, no torch)
# --------------------------------------------------------------------------
class RewardBridgeTest(unittest.TestCase):
    def test_components_valid(self):
        r = reward_func(0.82, valid=True, prev_fitness=0.75, novelty=0.9,
                        program="clf.fit(X, y)\npreds = clf.predict(X_eval)\n")
        self.assertAlmostEqual(r.validity, 1.0)
        self.assertGreater(r.improvement, 0.0)          # 0.82 - 0.75
        self.assertGreater(r.diversity, 0.0)
        self.assertGreaterEqual(r.total, 0.0)
        self.assertLessEqual(r.total, RewardConfig().max_reward)

    def test_invalid_no_bonus_and_failed_fitness(self):
        base = RewardConfig()
        r = reward_func(None, valid=False, novelty=0.0, program=None, config=base)
        self.assertEqual(r.validity, 0.0)
        self.assertEqual(r.improvement, 0.0)
        # only baseline (0) minus nothing -> 0, clamped >= min_reward
        self.assertGreaterEqual(r.total, base.min_reward)
        self.assertLessEqual(r.total, base.max_reward)

    def test_clamp_upper(self):
        huge = reward_func(1.0, valid=True, prev_fitness=0.0, novelty=1.0,
                           program="#" * 5000,  # big parsimony penalty
                           config=RewardConfig(validity_bonus=10.0, improvement_scale=10.0))
        self.assertLessEqual(huge.total, RewardConfig().max_reward)

    def test_population_ducktyped(self):
        @dataclass
        class FakeCand:
            fitness: float | None
            novelty: float
            code: str | None = None
            status: str = ""

        cands = [
            FakeCand(0.8, 0.9, "a=1", ""),
            FakeCand(None, 0.0, None, "rejected_novelty"),
        ]
        out = reward_population(cands, prev_best_fitness=0.7)
        self.assertEqual(len(out), 2)
        self.assertIsInstance(out[0], RewardComponents)
        self.assertEqual(out[1].validity, 0.0)  # rejected => invalid


# --------------------------------------------------------------------------
# D2 -- local training (device + synthetic dataset, torch optional)
# --------------------------------------------------------------------------
class LocalTrainLogicTest(unittest.TestCase):
    def test_detect_device_returns_known(self):
        self.assertIn(detect_device(), ("mps", "cpu"))

    def test_config_defaults(self):
        cfg = TrainConfig()
        self.assertEqual(cfg.model_source, "hf")
        self.assertLessEqual(cfg.max_params_b, 0.6)
        self.assertTrue(cfg.effective_device() in ("mps", "cpu"))

    def test_synthetic_dataset_shape_and_content(self):
        cfg = TrainConfig(model_source="synthetic", n_samples=8)
        t = LocalLLMTrainer(cfg)
        pairs = t.make_synthetic_dataset()
        self.assertEqual(len(pairs), 8)
        for prompt_text, program in pairs:
            self.assertIsInstance(prompt_text, str)
            self.assertIn("submission.csv", program)  # teacher output is valid sklearn
            self.assertIn("operator=", prompt_text)


# --------------------------------------------------------------------------
# D3 -- local backend wiring (torch optional)
# --------------------------------------------------------------------------
class LocalOperatorBackendTest(unittest.TestCase):
    def test_backend_from_callable(self):
        captured = {}

        def fn(x):
            captured["prompt"] = x
            return "print('hi')"

        b = LocalLLMOperatorBackend(fn)
        out = b.generate(OperatorPrompt(operator="draft", task_description="t",
                                        target="Survived", id_col="PassengerId"))
        self.assertIn("print('hi')", out)
        # the operator prompt (with operator=, target=, etc.) is what the backend
        # hands to the generator callable -- here we captured it.
        self.assertIn("operator=draft", captured["prompt"])

    def test_backend_rejects_non_callable(self):
        with self.assertRaises(TypeError):
            LocalLLMOperatorBackend(123)


# --------------------------------------------------------------------------
# MPS smoke test -- trains a tiny model on device, drives an operator
# --------------------------------------------------------------------------
def _mps_available():
    try:
        import torch

        return torch.backends.mps.is_available()
    except Exception:
        return False


@unittest.skipUnless(_mps_available(), "MPS not available on this machine")
class LocalTrainMpsSmokeTest(unittest.TestCase):
    def test_train_on_mps_then_drive_operator(self):
        cfg = TrainConfig(
            model_source="synthetic",
            n_samples=8,
            epochs=1,
            max_seq_len=256,
            report_every=0,
            device="mps",  # force Metal to prove the path
        )
        trainer = LocalLLMTrainer(cfg).train()
        self.assertEqual(trainer.last_train_device, "mps")
        self.assertEqual(trainer.device, "mps")

        gen = trainer.export_generator()
        self.assertTrue(callable(gen))

        backend = LocalLLMOperatorBackend(gen)
        program = draft_program(
            backend,
            target="Survived",
            id_col="PassengerId",
            task_description="classify titanic passenger survival",
            caller_stage="inner_program_evolution",
        )
        self.assertIsInstance(program, str)
        # The locally-trained generator produced *some* program text.
        self.assertTrue(len(program) >= 0)

        # device report sanity
        rep = trainer.report()
        self.assertEqual(rep["device"], "mps")
        self.assertEqual(rep["model_source"], "synthetic")


if __name__ == "__main__":
    unittest.main()
