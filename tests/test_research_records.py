"""Tests for research-record leaderboard, reproduce, validate, and tracked-task eval."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from safety_auto_research.benchmark_tasks import registry
from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.service import ControlPlaneService


class _EnvMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._files = []
        for _ in range(3):
            t = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            t.close()
            os.remove(t.name)
            self._files.append(t.name)
        self._old = {
            k: os.environ.get(k)
            for k in ("CUSTOM_TASKS_STORE", "CONTROL_PLANE_STORE", "RESEARCH_STATE_DB")
        }
        os.environ["CUSTOM_TASKS_STORE"] = self._files[0]
        os.environ["CONTROL_PLANE_STORE"] = self._files[1]
        os.environ["RESEARCH_STATE_DB"] = self._files[2]
        self.svc = ControlPlaneService()
        self.client = TestClient(create_app(self.svc))

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for f in self._files:
            if os.path.exists(f):
                os.remove(f)


class ValidateEndpointTest(_EnvMixin):
    def test_valid_form(self) -> None:
        r = self.client.post(
            "/benchmark-tasks/validate",
            json={
                "task_type": "tabular_classification",
                "values": {
                    "name": "demo", "eval_metric": "accuracy", "direction": "higher",
                    "data_dir": "/tmp/x", "target_col": "y",
                },
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["valid"])

    def test_invalid_metric_rejected(self) -> None:
        r = self.client.post(
            "/benchmark-tasks/validate",
            json={
                "task_type": "tabular_classification",
                "values": {
                    "name": "demo", "eval_metric": "mse", "direction": "higher",
                    "data_dir": "/tmp/x", "target_col": "y",
                },
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["valid"])
        self.assertTrue(any("mse" in e for e in r.json()["errors"]))

    def test_validate_does_not_persist(self) -> None:
        self.client.post(
            "/benchmark-tasks/validate",
            json={"task_type": "llm_sft", "values": {"name": "x", "eval_metric": "eval_loss",
                                                      "direction": "lower", "llm_modality": "text",
                                                      "base_model": "q", "dataset_path": "/tmp/d",
                                                      "dataset_format": "messages", "train_type": "lora"}},
        )
        # validate must not create a persisted custom task
        custom = [t for t in self.client.get("/benchmark-tasks").json()
                  if t.get("task_id", "").startswith("custom.")]
        self.assertEqual(custom, [])


class ResearchRecordTest(_EnvMixin):
    def _register_tracked(self, task_type: str, **extra) -> str:
        base = {
            "llm_sft": {"name": "sft", "eval_metric": "eval_loss", "direction": "lower",
                        "llm_modality": "text", "base_model": "q", "dataset_path": "/tmp/d",
                        "dataset_format": "messages", "train_type": "lora"},
            "embedding_contrastive": {"name": "emb", "eval_metric": "recall_at_10", "direction": "higher",
                                      "modality_pair": "text-image", "method": "clip",
                                      "dataset_path": "/tmp/p", "text_encoder": "bge",
                                      "vision_encoder": "vit", "data_format": "pairs",
                                      "eval_protocol": "retrieval_recall"},
        }[task_type]
        base.update(extra)
        r = self.client.post("/benchmark-tasks/register", json={"task_type": task_type, "values": base})
        self.assertEqual(r.status_code, 201, r.text)
        task_id = r.json()["task_id"]
        r2 = self.client.post(f"/benchmark-tasks/{task_id}/launch", json={})
        self.assertEqual(r2.status_code, 200)
        return r2.json()["run_id"]

    def test_report_metric_enters_leaderboard(self) -> None:
        run_id = self._register_tracked("llm_sft")
        r = self.client.post(
            f"/workflow-runs/{run_id}/report-metric",
            json={"metric_name": "eval_loss", "direction": "lower", "score": 0.85},
        )
        self.assertEqual(r.status_code, 200, r.text)
        rec = r.json()
        self.assertTrue(rec["task_id"].startswith("custom."))
        # per-task list + top3
        lst = self.client.get("/research-records").json()
        self.assertEqual(len(lst), 1)
        self.assertTrue(lst[0]["is_top3"])
        # global leaderboard
        board = self.client.get("/research-records/leaderboard").json()
        self.assertEqual(len(board), 1)

    def test_top3_is_marked_for_many_runs(self) -> None:
        run_id = self._register_tracked("llm_sft")
        scores = [1.2, 0.9, 0.7, 0.5, 0.3]  # lower is better
        for s in scores:
            self.client.post(
                f"/workflow-runs/{run_id}/report-metric",
                json={"metric_name": "eval_loss", "direction": "lower", "score": s},
            )
        lst = self.client.get("/research-records").json()
        self.assertEqual(len(lst), 5)
        top3 = [r for r in lst if r["is_top3"]]
        self.assertEqual(len(top3), 3)
        # best 3 (lowest) are flagged
        flagged = sorted(r["score"] for r in top3)
        self.assertEqual(flagged, [0.3, 0.5, 0.7])

    def test_reproduce_creates_new_run(self) -> None:
        run_id = self._register_tracked("llm_sft")
        self.client.post(
            f"/workflow-runs/{run_id}/report-metric",
            json={"metric_name": "eval_loss", "direction": "lower", "score": 0.85},
        )
        rec_id = self.client.get("/research-records").json()[0]["record_id"]
        r = self.client.post(f"/research-records/{rec_id}/reproduce")
        self.assertEqual(r.status_code, 200, r.text)
        new_run = r.json()["run_id"]
        self.assertNotEqual(new_run, run_id)
        # new run exists
        self.assertEqual(self.client.get(f"/workflow-runs/{new_run}").status_code, 200)

    def test_evaluate_llm_judge(self) -> None:
        run_id = self._register_tracked("llm_sft")
        ev = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
        pr = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
        json.dump({"prompt": "What is 2+2?", "reference": "4"}, ev)
        json.dump({"prompt": "What is 2+2?", "output": "4"}, pr)
        ev.close(); pr.close()
        r = self.client.post(
            f"/workflow-runs/{run_id}/evaluate",
            json={"eval_dataset_path": ev.name, "predictions_path": pr.name},
        )
        self.assertEqual(r.status_code, 200, r.text)
        rec = r.json()
        self.assertEqual(rec["metric_name"], "f1")
        self.assertGreater(rec["score"], 0.0)
        os.remove(ev.name); os.remove(pr.name)

    def test_evaluate_embedding_recall(self) -> None:
        run_id = self._register_tracked("embedding_contrastive")
        rl = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
        rl.write(json.dumps({"query": "q1", "gold": "d1", "ranked": ["d1", "d2", "d3"]}) + "\n")
        rl.write(json.dumps({"query": "q2", "gold": "d2", "ranked": ["d3", "d2", "d1"]}) + "\n")
        rl.close()
        r = self.client.post(
            f"/workflow-runs/{run_id}/evaluate",
            json={"ranked_lists_path": rl.name},
        )
        self.assertEqual(r.status_code, 200, r.text)
        rec = r.json()
        self.assertEqual(rec["metric_name"], "recall_at_10")
        self.assertAlmostEqual(rec["score"], 1.0, places=3)
        os.remove(rl.name)

    def test_capture_run_record_direct(self) -> None:
        # Build a run whose objective declares accuracy, then inject a metric and capture.
        from safety_auto_research.platform_contracts.enums import RunType
        from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest

        req = CreateWorkflowRunRequest(
            program_id="benchmark",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research",
            target_id="platform.titanic",
            objective_snapshot={
                "benchmark_task_id": "platform.titanic",
                "name": "titanic", "eval_metric": "accuracy", "direction": "higher",
                "config": {"inner_loop": {"model": "gbm"}},
            },
            requested_outcomes=["x"],
        )
        run = self.svc.create_workflow_run(req)
        self.svc._repo.record_metric(run.run_id, "eval.accuracy", 0.81)
        rec = self.svc.capture_run_record(run.run_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["metric_name"], "accuracy")
        self.assertAlmostEqual(rec["score"], 0.81)
        self.assertTrue(self.client.get("/research-records").json()[0]["is_top3"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
