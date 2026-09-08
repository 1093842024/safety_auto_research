"""Tests for the layer_01 literature-research capability (Phase 2).

All HTTP is mocked via ``urllib.request.urlopen`` so the suite stays offline.
Pins the contract:

* ``LiteratureResearchExecutor`` is bound to the ``layer_01_literature_research``
  capability (replacing the long-standing stub).
* Query is required; empty query → ``StageStatus.FAILED``.
* arXiv Atom XML is parsed correctly (title / authors / year / pdf_url).
* Cached hits short-circuit network I/O on repeat queries.
* GitHub source falls back silently when ``GITHUB_TOKEN`` is missing.
* ``ArtifactType.PAPER_SET`` is published and ``EvalCompletedEvent`` is emitted
  with ``primary = hit_count / max_results``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from typing import Any
from unittest.mock import patch

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.capabilities.literature_research_executor import (
    LiteratureResearchExecutor,
    Paper,
    _http_get,
    _parse_arxiv_entries,
    _q,
)
from safety_auto_research.platform_contracts.enums import RunType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _arxiv_atom_xml(entries: list[dict[str, Any]]) -> bytes:
    """Build a minimal arXiv Atom XML response with the given entries."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom">',
    ]
    for e in entries:
        authors_xml = "".join(
            f"<author><name>{a}</name></author>" for a in e.get("authors", [])
        )
        cats = "".join(
            f'<category term="{t}" />' for t in e.get("tags", [])
        )
        parts.append(
            f'<entry>'
            f'<id>{e.get("url", "http://export.arxiv.org/abs/0000.00000")}</id>'
            f'<title>{e.get("title", "untitled")}</title>'
            f'<summary>{e.get("abstract", "")}</summary>'
            f'<published>{e.get("published", "2024-01-01T00:00:00Z")}</published>'
            f'{authors_xml}{cats}'
            f'</entry>'
        )
    parts.append("</feed>")
    return "".join(parts).encode("utf-8")


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# arXiv parser (pure-function unit tests)
# ---------------------------------------------------------------------------
class ArxivParserTest(unittest.TestCase):
    def test_parses_minimal_entry(self) -> None:
        xml = _arxiv_atom_xml([
            {
                "title": "A study of cats",
                "authors": ["Alice", "Bob"],
                "abstract": "We study cats.",
                "published": "2024-03-15T12:34:56Z",
                "url": "http://export.arxiv.org/abs/2403.12345v1",
                "tags": ["cs.LG"],
            }
        ])
        papers = _parse_arxiv_entries(xml, max_results=5)
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p.title, "A study of cats")
        self.assertEqual(p.authors, ["Alice", "Bob"])
        self.assertEqual(p.year, 2024)
        self.assertEqual(p.abstract, "We study cats.")
        self.assertEqual(p.source, "arxiv")
        self.assertEqual(p.tags, ["cs.LG"])
        self.assertEqual(p.pdf_url, "http://arxiv.org/pdf/2403.12345")

    def test_respects_max_results(self) -> None:
        xml = _arxiv_atom_xml(
            [
                {"title": f"paper {i}", "authors": ["X"], "published": "2024-01-01T00:00:00Z"}
                for i in range(10)
            ]
        )
        self.assertEqual(len(_parse_arxiv_entries(xml, max_results=3)), 3)
        self.assertEqual(len(_parse_arxiv_entries(xml, max_results=10)), 10)

    def test_skips_entries_without_title(self) -> None:
        xml = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>x</id></entry>
          <entry><title>ok</title><id>y</id></entry>
        </feed>"""
        papers = _parse_arxiv_entries(xml, max_results=5)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "ok")

    def test_invalid_xml_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            _parse_arxiv_entries(b"not-xml", max_results=1)


# ---------------------------------------------------------------------------
# HTTP helper (mocked)
# ---------------------------------------------------------------------------
class HttpGetTest(unittest.TestCase):
    def test_returns_body(self) -> None:
        with patch("urllib.request.urlopen", return_value=_FakeResp(b"hello")):
            self.assertEqual(_http_get("http://x.test"), b"hello")

    def test_http_error_raises(self) -> None:
        # In Python 3.13 HTTPError's fp is auto-set; constructing one without a body
        # would raise TypeError. Use a dummy body via .read() returning bytes.
        class _ErrResp:
            def read(self) -> bytes:
                return b"rate-limited"

        err = urllib.error.HTTPError("http://x.test", 429, "Too Many", {}, _ErrResp())
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                _http_get("http://x.test")
            self.assertIn("429", str(cm.exception))


# ---------------------------------------------------------------------------
# Executor integration (mocked HTTP) — drives the real StageExecutor via the
# FastAPI capability-run endpoint so the audit + artifact contract is exercised.
# ---------------------------------------------------------------------------
class ExecutorIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        # Same pattern as test_badcase_retrain.BadcaseRetrainExecutorTest: direct
        # orchestrator wiring so we can call ``run_capability`` without an HTTP
        # socket and without a 409 ``stages can only be created for a running workflow``.
        self.tmpdir = tempfile.mkdtemp(prefix="lit-it-")
        self.orch = ClosedLoopOrchestrator(ControlPlaneService())

    def _start_run(self) -> str:
        run = self.orch.svc.create_workflow_run(
            CreateWorkflowRunRequest(
                program_id="discovery",
                run_type=RunType.DISCOVERY,
                entry_stage="literature_research",
                target_id="discovery-lit-test",
                objective_snapshot={"name": "literature discovery test"},
            )
        )
        self.orch.svc.start_workflow_run(run.run_id)
        return run.run_id

    def test_layer_01_capability_resolves_to_real_executor(self) -> None:
        # Make sure the layer is bound (not a stub) before driving it.
        from safety_auto_research.execution_plane.capabilities.registry import (
            default_capability_registry,
        )

        cap = default_capability_registry().resolve("layer_01_literature_research")
        self.assertIsNotNone(cap)
        self.assertIsInstance(cap.executor, LiteratureResearchExecutor)

    def test_executor_requires_query(self) -> None:
        run_id = self._start_run()
        # Manually drive via orchestrator; capability failure routes back as ExecResult.
        _, result = self.orch.run_capability(
            run_id, "layer_01_literature_research", {}
        )
        # Should be a clean failure (no crash), detail mentions the missing query.
        self.assertIn("query", (result.detail or "").lower())

    def test_arxiv_round_trip_via_mock(self) -> None:
        run_id = self._start_run()
        xml = _arxiv_atom_xml([
            {
                "title": "Robust safety via RS",
                "authors": ["Carol"],
                "abstract": "We propose robust safety.",
                "published": "2024-05-01T00:00:00Z",
                "url": "http://export.arxiv.org/abs/2405.00001v1",
                "tags": ["cs.AI", "cs.LG"],
            },
            {
                "title": "Jailbreak survey",
                "authors": ["Dave", "Eve"],
                "abstract": "A survey.",
                "published": "2024-02-01T00:00:00Z",
                "url": "http://export.arxiv.org/abs/2402.00002v1",
                "tags": ["cs.CR"],
            },
        ])
        cache_dir = os.path.join(self.tmpdir, "cache1")
        os.makedirs(cache_dir, exist_ok=True)
        with patch("urllib.request.urlopen", return_value=_FakeResp(xml)):
            _, result = self.orch.run_capability(
                run_id,
                "layer_01_literature_research",
                {"query": "robust safety", "max_results": 5, "cache_dir": cache_dir},
            )
        # Event must reflect 2 hits / 5 max.
        self.assertIsNotNone(result.event)
        metrics = result.event.metrics
        self.assertEqual(metrics["hit_count"], 2.0)
        self.assertEqual(metrics["primary"], 0.4)
        # Cache file was written.
        cache_files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
        self.assertGreaterEqual(len(cache_files), 1, "cache should have been written")
        with open(os.path.join(cache_dir, cache_files[0]), encoding="utf-8") as fh:
            cached = json.load(fh)
        self.assertEqual(len(cached), 2)
        titles = {p["title"] for p in cached}
        self.assertIn("Robust safety via RS", titles)

    def test_cache_short_circuits_second_call(self) -> None:
        run_id = self._start_run()
        cache_dir = os.path.join(self.tmpdir, "cache2")
        os.makedirs(cache_dir, exist_ok=True)
        # Pre-populate cache so no HTTP is touched.
        from safety_auto_research.execution_plane.capabilities.literature_research_executor import (
            _cache_key,
            _cache_write,
        )
        key = _cache_key("redteam", ["arxiv"], None)
        _cache_write(cache_dir, key, [{"title": "cached-paper", "authors": [], "year": 0,
                                      "url": "", "pdf_url": None, "abstract": "",
                                      "source": "arxiv", "tags": []}])
        # URLopen MUST NOT be called.
        def _fail(*_a, **_kw):
            raise AssertionError("urllib.request.urlopen called despite cache hit")

        with patch("urllib.request.urlopen", side_effect=_fail):
            _, result = self.orch.run_capability(
                run_id,
                "layer_01_literature_research",
                {"query": "redteam", "cache_dir": cache_dir},
            )
        # primary = 1/20 (default max_results) since cache had 1 hit.
        self.assertEqual(result.event.metrics["hit_count"], 1.0)

    def test_github_skipped_without_token(self) -> None:
        # Without GITHUB_TOKEN, requesting github source should yield 0 github papers.
        from safety_auto_research.execution_plane.capabilities.literature_research_executor import (
            _search_github,
        )

        with patch.dict(os.environ, {}, clear=True):
            papers = _search_github("anything", max_results=10, token=None)
        self.assertEqual(papers, [])


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
class HelperTest(unittest.TestCase):
    def test_q_encodes_special_chars(self) -> None:
        self.assertNotIn(" ", _q("a b"))
        self.assertIn("a", _q("a b"))

    def test_paper_to_dict_round_trip(self) -> None:
        p = Paper(title="t", authors=["a"], year=2024, url="u", pdf_url="p",
                 abstract="x", source="arxiv", tags=["t1"])
        d = p.to_dict()
        self.assertEqual(d["title"], "t")
        self.assertEqual(d["year"], 2024)
        # Round-trip: re-construct from the dict.
        p2 = Paper(**d)
        self.assertEqual(p2.title, p.title)


if __name__ == "__main__":
    unittest.main()