"""External benchmark *suites* integrated into the catalog (Weng harness appendix).

A suite is a published multi-task benchmark (e.g. ScienceAgentBench, MLE-bench)
that we integrate at four levels:

1. **catalog entry** — one suite-level ``BenchmarkTask`` in the curated catalog
   (tracked-only; the dual loop cannot execute it natively yet);
2. **task manifest** — the full per-task/per-competition manifest bundled as JSON
   under ``suites/data/`` so the control plane can browse & filter sub-tasks;
3. **official baselines** — published baseline/leaderboard results so any run of
   ours can be compared against the literature;
4. **data acquisition** — the exact commands / URLs needed to fetch the real
   datasets + evaluation code (kept external; we never redistribute them).

Sub-task ids follow ``<suite_id>/<sub_id>``, e.g. ``science_agent_bench/42``
or ``mle_bench/spooky-author-identification``.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


@dataclass
class BaselineResult:
    """One published baseline / leaderboard row for a suite."""

    agent: str  # scaffold / framework, e.g. "AIDE", "self-debug"
    model: str  # backbone LLM
    metric: str  # headline metric name
    score: float  # headline metric value
    extra: dict[str, Any] = field(default_factory=dict)  # split scores, cost, ...
    source: str = ""  # paper / leaderboard provenance
    date: str = ""  # result date (YYYY-MM-DD)
    is_headline: bool = False  # the number cited in the Weng harness post


@dataclass
class BenchmarkSuite:
    suite_id: str
    name: str
    paper: str  # arXiv / publication reference
    homepage: str  # canonical repo url
    description: str
    task_count: int
    headline_metric: str
    direction: str  # higher | lower
    disciplines: dict[str, int]  # discipline/split -> task count (computed from usable tasks)
    evaluation: dict[str, str]  # metric name -> definition
    data_acquisition: dict[str, Any]  # how to fetch data/eval code (external)
    analyses: dict[str, Any] = field(default_factory=dict)  # resource-scaling / contamination
    license: str = ""
    manifest_file: str = ""  # JSON file under suites/data/
    baselines: list[BaselineResult] = field(default_factory=list)
    note: str = ""
    official_task_count: int | None = None  # published count before exclusions

    # ---- manifest access ------------------------------------------------ #
    def load_manifest(self, include_excluded: bool = True) -> list[dict[str, Any]]:
        if not self.manifest_file:
            return []
        path = os.path.join(_DATA_DIR, self.manifest_file)
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        if not include_excluded:
            rows = [r for r in rows if not r.get("excluded")]
        return rows

    def usable_tasks(self) -> list[dict[str, Any]]:
        """Tasks that survive the exclusion policy (no known leakage/prep defect)."""
        return self.load_manifest(include_excluded=False)

    def excluded_tasks(self) -> list[dict[str, Any]]:
        """Tasks removed by the exclusion policy (label/test leakage, prep defects)."""
        if not self.manifest_file:
            return []
        return [r for r in self.load_manifest(include_excluded=True) if r.get("excluded")]

    def to_dict(self, include_manifest: bool = False) -> dict[str, Any]:
        tasks = self.load_manifest(include_excluded=True)
        excluded = [t for t in tasks if t.get("excluded")]
        usable = [t for t in tasks if not t.get("excluded")]
        disciplines = self.disciplines
        if tasks:
            # recompute from usable tasks so counts reflect the exclusion policy
            disciplines = dict(
                Counter((t.get("complexity_split") or t.get("domain") or "unknown") for t in usable)
            )
        d: dict[str, Any] = {
            "suite_id": self.suite_id,
            "name": self.name,
            "paper": self.paper,
            "homepage": self.homepage,
            "description": self.description,
            "task_count": len(usable) if tasks else self.task_count,
            "official_task_count": self.official_task_count if self.official_task_count is not None else self.task_count,
            "headline_metric": self.headline_metric,
            "direction": self.direction,
            "disciplines": disciplines,
            "evaluation": self.evaluation,
            "data_acquisition": self.data_acquisition,
            "analyses": self.analyses,
            "license": self.license,
            "note": self.note,
            "baseline_count": len(self.baselines),
            "usable_task_count": len(usable),
            "excluded_task_count": len(excluded),
            "excluded_task_ids": [t["competition_id"] for t in excluded],
        }
        if include_manifest:
            d["tasks"] = tasks
        return d


@lru_cache(maxsize=None)
def _registry() -> dict[str, BenchmarkSuite]:
    from . import mle_bench, science_agent_bench

    suites = [science_agent_bench.SUITE, mle_bench.SUITE]
    return {s.suite_id: s for s in suites}


def list_suites() -> list[BenchmarkSuite]:
    return list(_registry().values())


def get_suite(suite_id: str) -> BenchmarkSuite | None:
    return _registry().get(suite_id)


def suite_headline_baseline(suite: BenchmarkSuite) -> BaselineResult | None:
    for b in suite.baselines:
        if b.is_headline:
            return b
    return suite.baselines[0] if suite.baselines else None
