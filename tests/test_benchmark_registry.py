"""Tests for custom research-task registration (benchmark_tasks.registry + API)."""

from __future__ import annotations

import os
import unittest
import tempfile

from fastapi.testclient import TestClient

from safety_auto_research.benchmark_tasks import get_catalog
from safety_auto_research.benchmark_tasks import get_task
from safety_auto_research.benchmark_tasks import registry
from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.service import ControlPlaneService


class _TmpStoreMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.remove(self._tmp.name)  # start empty
        self._old = os.environ.get("CUSTOM_TASKS_STORE")
        os.environ["CUSTOM_TASKS_STORE"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("CUSTOM_TASKS_STORE", None)
        else:
            os.environ["CUSTOM_TASKS_STORE"] = self._old
        if os.path.exists(self._tmp.name):
            os.remove(self._tmp.name)


class TaskTypeSpecTest(unittest.TestCase):
    def test_all_eight_types_present(self) -> None:
        ids = {s["type_id"] for s in registry.get_task_type_specs()}
        self.assertEqual(
            ids,
            {
                "tabular_classification",
                "text_classification",
                "image_classification",
                "audio_classification",
                "llm_sft",
                "llm_rl",
                "llm_opd",
                "embedding_contrastive",
            },
        )

    def test_only_tabular_is_executable(self) -> None:
        for s in registry.get_task_type_specs():
            self.assertEqual(
                s["executable"], s["type_id"] == "tabular_classification", s["type_id"]
            )

    def test_specs_carry_common_fields(self) -> None:
        for s in registry.get_task_type_specs():
            keys = {f["key"] for f in s["common_fields"]}
            self.assertIn("name", keys)
            self.assertIn("eval_metric", keys)


class ValidationTest(_TmpStoreMixin):
    def test_missing_required_fields(self) -> None:
        errs = registry.validate_registration("llm_opd", {"name": "x"})
        joined = "；".join(errs)
        self.assertIn("student_model", joined)
        self.assertIn("teacher_model", joined)

    def test_unknown_type(self) -> None:
        errs = registry.validate_registration("nope", {})
        self.assertTrue(errs and "未知任务类型" in errs[0])

    def test_reward_model_requires_path(self) -> None:
        vals = {
            "name": "rl",
            "eval_metric": "acc",
            "direction": "higher",
            "algorithm": "grpo",
            "llm_modality": "text",
            "base_model": "m",
            "prompt_dataset": "/tmp/p.jsonl",
            "reward_type": "reward_model",
            "eval_benchmark": "GSM8K",
        }
        errs = registry.validate_registration("llm_rl", vals)
        self.assertTrue(any("reward_model_path" in e for e in errs))

    def test_embedding_requires_matching_encoder(self) -> None:
        vals = {
            "name": "emb",
            "eval_metric": "recall_at_10",
            "direction": "higher",
            "modality_pair": "text-image",
            "method": "clip",
            "data_format": "image_text_pairs",
            "dataset_path": "/tmp/pairs.jsonl",
            "eval_protocol": "retrieval_recall",
        }
        errs = registry.validate_registration("embedding_contrastive", vals)
        self.assertTrue(any("编码器" in e for e in errs))
        vals["text_encoder"] = "bge-base-zh-v1.5"
        self.assertEqual(registry.validate_registration("embedding_contrastive", vals), [])


class RegistrationRoundtripTest(_TmpStoreMixin):
    def _register_opd(self) -> dict:
        return registry.register_custom_task(
            "llm_opd",
            {
                "name": "OPD math distill",
                "eval_metric": "aime24_accuracy",
                "direction": "higher",
                "student_model": "Qwen3-8B-Base",
                "teacher_model": "Qwen3-32B",
                "prompt_dataset": "/tmp/prompts.jsonl",
                "eval_benchmark": "AIME24",
                "target_value": 0.7,
            },
        )

    def test_register_persists_and_merges_into_catalog(self) -> None:
        rec = self._register_opd()
        self.assertTrue(rec["task_id"].startswith("custom."))
        task = get_task(rec["task_id"])
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.category, "custom")
        self.assertEqual(task.task_type, "llm_opd")
        self.assertFalse(task.supported_by_platform)
        self.assertIn("gkd", task.run_command)
        self.assertIn(rec["task_id"], [t.task_id for t in get_catalog()])

    def test_duplicate_names_get_unique_ids(self) -> None:
        a = self._register_opd()
        b = self._register_opd()
        self.assertNotEqual(a["task_id"], b["task_id"])

    def test_delete(self) -> None:
        rec = self._register_opd()
        self.assertTrue(registry.delete_custom_task(rec["task_id"]))
        self.assertIsNone(get_task(rec["task_id"]))
        self.assertFalse(registry.delete_custom_task(rec["task_id"]))

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            registry.register_custom_task("llm_opd", {"name": "x"})


class ApiTest(_TmpStoreMixin):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(ControlPlaneService()))

    def test_task_types_endpoint(self) -> None:
        r = self.client.get("/benchmark-tasks/task-types")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 8)

    def test_register_endpoint_validates(self) -> None:
        r = self.client.post(
            "/benchmark-tasks/register", json={"task_type": "llm_sft", "values": {}}
        )
        self.assertEqual(r.status_code, 400)

    def test_register_launch_tracked(self) -> None:
        r = self.client.post(
            "/benchmark-tasks/register",
            json={
                "task_type": "llm_sft",
                "values": {
                    "name": "vlm sft",
                    "eval_metric": "eval_loss",
                    "direction": "lower",
                    "llm_modality": "vision-language",
                    "base_model": "Qwen2.5-VL-7B-Instruct",
                    "dataset_path": "/tmp/sft.jsonl",
                    "dataset_format": "messages",
                    "train_type": "lora",
                },
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        task_id = r.json()["task_id"]
        # tracked-only launch: creates a run in requested state, never auto-runs
        r2 = self.client.post(f"/benchmark-tasks/{task_id}/launch", json={})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["status"], "requested")
        self.assertFalse(r2.json()["supported_by_platform"])

    def test_custom_tabular_launch_maps_to_kaggle_eval(self) -> None:
        pkg_root = os.path.dirname(
            os.path.dirname(os.path.abspath(registry.__file__))
        )
        titanic_dir = os.path.join(pkg_root, "data", "kaggle", "titanic")
        if not os.path.exists(os.path.join(titanic_dir, "train.csv")):
            self.skipTest("titanic train.csv not available")
        r = self.client.post(
            "/benchmark-tasks/register",
            json={
                "task_type": "tabular_classification",
                "values": {
                    "name": "custom titanic",
                    "eval_metric": "cv_accuracy",
                    "direction": "higher",
                    "data_dir": titanic_dir,
                    "target_col": "Survived",
                    "id_col": "PassengerId",
                    "threshold": 0.78,
                    "cv_folds": 3,
                },
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertTrue(body["supported_by_platform"])
        # auto_run=False: only creates the run record; config must still be persisted.
        r2 = self.client.post(
            f"/benchmark-tasks/{body['task_id']}/launch?auto_run=false", json={}
        )
        self.assertEqual(r2.status_code, 200)
        run = self.client.get(f"/workflow-runs/{r2.json()['run_id']}").json()
        self.assertEqual(run["objective_snapshot"]["benchmark_task_id"], body["task_id"])

    def test_delete_curated_blocked(self) -> None:
        r = self.client.delete("/benchmark-tasks/platform.titanic")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
