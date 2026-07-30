"""Tests for the external benchmark-suite integration (ScienceAgentBench /
MLE-bench, from Lilian Weng's harness post appendix):

  A. suite registry + manifest integrity (102 SAB tasks, 75 MLE-bench comps);
  B. official baselines present with the cited headline numbers;
  C. suite-level entries appear in the curated catalog;
  D. control-plane API endpoints (/benchmark-suites*).
"""

from __future__ import annotations

import unittest

from safety_auto_research.benchmark_tasks import get_catalog
from safety_auto_research.benchmark_tasks import get_task
from safety_auto_research.benchmark_tasks import suites
from safety_auto_research.benchmark_tasks.suites import suite_headline_baseline


class SuiteRegistryTest(unittest.TestCase):
    def test_both_suites_registered(self):
        ids = {s.suite_id for s in suites.list_suites()}
        self.assertEqual(ids, {"science_agent_bench", "mle_bench"})

    def test_unknown_suite_returns_none(self):
        self.assertIsNone(suites.get_suite("nope"))


class ScienceAgentBenchTest(unittest.TestCase):
    def setUp(self):
        self.suite = suites.get_suite("science_agent_bench")

    def test_manifest_has_102_tasks_with_required_fields(self):
        rows = self.suite.load_manifest()
        self.assertEqual(len(rows), 102)
        required = {"instance_id", "domain", "task_inst", "gold_program_name",
                    "output_fname", "eval_script_name"}
        for row in rows:
            self.assertTrue(required.issubset(row.keys()))

    def test_discipline_breakdown_matches_manifest(self):
        rows = self.suite.load_manifest()
        from collections import Counter

        counts = Counter(r["domain"] for r in rows)
        self.assertEqual(dict(counts), self.suite.disciplines)
        self.assertEqual(sum(counts.values()), self.suite.task_count)

    def test_headline_baseline_is_o1_self_debug_42_2(self):
        head = suite_headline_baseline(self.suite)
        self.assertEqual(head.agent, "self-debug")
        self.assertIn("o1", head.model)
        self.assertAlmostEqual(head.score, 42.2)

    def test_claude_self_debug_paper_numbers(self):
        rows = [b for b in self.suite.baselines
                if b.agent == "self-debug" and "Claude-3.5" in b.model]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].score, 32.4)
        self.assertAlmostEqual(rows[0].extra["sr_with_knowledge"], 34.3)


class MLEBenchTest(unittest.TestCase):
    def setUp(self):
        self.suite = suites.get_suite("mle_bench")

    def test_manifest_has_75_competitions_split_22_38_15(self):
        rows = self.suite.load_manifest()
        self.assertEqual(len(rows), 75)
        from collections import Counter

        counts = Counter(r["complexity_split"] for r in rows)
        self.assertEqual(counts["low"], 22)
        self.assertEqual(counts["medium"], 38)
        self.assertEqual(counts["high"], 15)
        # lite == low split, every lite comp carries category + size metadata
        lite = [r for r in rows if r["in_lite"]]
        self.assertEqual(len(lite), 22)
        self.assertTrue(all(r["category"] and r["dataset_size_gb"] is not None for r in lite))

    def test_contamination_flags_present(self):
        rows = self.suite.load_manifest()
        flagged = [r for r in rows if r.get("known_issue")]
        self.assertGreaterEqual(len(flagged), 10)
        self.assertIn("random-acts-of-pizza", {r["competition_id"] for r in flagged})

    def test_exclusion_policy_marks_10_and_retains_3_noisy(self):
        # default manifest still lists all 75 (include_excluded defaults True)
        self.assertEqual(len(self.suite.load_manifest()), 75)
        # usable set after excluding leakage/prep-defect competitions
        usable = self.suite.usable_tasks()
        excluded = self.suite.excluded_tasks()
        self.assertEqual(len(usable), 65)
        self.assertEqual(len(excluded), 10)
        self.assertIn("random-acts-of-pizza", {t["competition_id"] for t in excluded})
        # the 3 crowded-leaderboard competitions are RETAINED but flagged noisy
        noisy = [t["competition_id"] for t in self.suite.load_manifest() if t.get("noisy_leaderboard")]
        self.assertEqual(len(noisy), 3)
        self.assertIn("jigsaw-toxic-comment-classification-challenge", noisy)
        # none of the noisy ones are excluded
        self.assertEqual({t["competition_id"] for t in excluded} & set(noisy), set())

    def test_headline_baseline_aide_o1_16_9(self):
        head = suite_headline_baseline(self.suite)
        self.assertEqual(head.agent, "AIDE")
        self.assertEqual(head.model, "o1-preview")
        self.assertAlmostEqual(head.score, 16.9)
        self.assertAlmostEqual(head.extra["leaderboard_all"], 17.12)

    def test_analyses_cover_resource_scaling_and_contamination(self):
        self.assertIn("resource_scaling", self.suite.analyses)
        self.assertIn("contamination", self.suite.analyses)


class CatalogIntegrationTest(unittest.TestCase):
    def test_suite_entries_in_catalog(self):
        ids = {t.task_id for t in get_catalog()}
        self.assertIn("suite.science_agent_bench", ids)
        self.assertIn("suite.mle_bench", ids)

    def test_suite_entries_are_tracked_only_with_baselines(self):
        sab = get_task("suite.science_agent_bench")
        mle = get_task("suite.mle_bench")
        for t in (sab, mle):
            self.assertFalse(t.supported_by_platform)
            self.assertEqual(t.harness, "manual")
        self.assertAlmostEqual(sab.baseline, 32.4)
        self.assertAlmostEqual(mle.baseline, 16.9)


class SuiteApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        cls.client = TestClient(create_app())

    def test_list_suites(self):
        resp = self.client.get("/benchmark-suites")
        self.assertEqual(resp.status_code, 200)
        ids = {s["suite_id"] for s in resp.json()}
        self.assertEqual(ids, {"science_agent_bench", "mle_bench"})

    def test_suite_detail_and_404(self):
        resp = self.client.get("/benchmark-suites/mle_bench")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # usable count after excluding 10 known-issue competitions
        self.assertEqual(body["task_count"], 65)
        self.assertEqual(body["official_task_count"], 75)
        self.assertEqual(body["excluded_task_count"], 10)
        self.assertIn("random-acts-of-pizza", body["excluded_task_ids"])
        self.assertIn("resource_scaling", body["analyses"])
        self.assertEqual(self.client.get("/benchmark-suites/nope").status_code, 404)

    def test_suite_tasks_filtering(self):
        resp = self.client.get(
            "/benchmark-suites/science_agent_bench/tasks",
            params={"domain": "Bioinformatics", "limit": 500},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 27)
        # default mle_bench tasks exclude the 10 known-issue competitions -> high split 14 (not 15)
        resp2 = self.client.get("/benchmark-suites/mle_bench/tasks", params={"split": "high"})
        self.assertEqual(resp2.json()["total"], 14)

    def test_tasks_endpoint_default_excludes_known_issues(self):
        # default (include_excluded omitted) hides the 10 excluded competitions
        default = self.client.get("/benchmark-suites/mle_bench/tasks")
        self.assertEqual(default.status_code, 200)
        self.assertEqual(default.json()["total"], 65)
        default_ids = {t["competition_id"] for t in default.json()["tasks"]}
        self.assertNotIn("random-acts-of-pizza", default_ids)
        # opt-in include_excluded=true restores the full 75
        all_ = self.client.get("/benchmark-suites/mle_bench/tasks", params={"include_excluded": True})
        self.assertEqual(all_.json()["total"], 75)
        all_ids = {t["competition_id"] for t in all_.json()["tasks"]}
        self.assertIn("random-acts-of-pizza", all_ids)

    def test_suite_baselines_endpoint(self):
        resp = self.client.get("/benchmark-suites/mle_bench/baselines")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["headline"]["agent"], "AIDE")
        self.assertAlmostEqual(body["headline"]["score"], 16.9)
        self.assertGreaterEqual(len(body["baselines"]), 6)


if __name__ == "__main__":
    unittest.main()
