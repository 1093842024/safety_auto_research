"""Literature-research capability — real arXiv (and optional GitHub) search (Phase 2, §五.A.2).

Replaces the long-standing ``StubCapabilityExecutor`` for the
``layer_01_literature_research`` layer. Behaviour:

* ``params["query"]`` — free-text query. arXiv's API interprets it the same way as
  the website search (title / abstract / author tokens). When absent, it is DERIVED
  from the research goal actually in scope (``params["goal"]`` / ``["objective"]`` /
  ``["open_goal"]``, then the run's ``objective_snapshot``) — see :func:`derive_query`.
  This is what lets an orchestrated caller (the MEA loop, an agent) invoke the layer
  without restating the topic it is already working on. Still fail-closed: when no
  research goal is reachable at all, the stage FAILS instead of searching for junk.
* ``params["max_results"]`` (default 20) — cap on returned papers.
* ``params["sources"]`` (default ``["arxiv"]``) — sub-list of ``arxiv`` /
  ``github``. GitHub requires ``GITHUB_TOKEN`` (falls back to ``arxiv`` only
  when missing).
* ``params["since"]`` (optional ISO date ``YYYY-MM-DD``) — only arXiv entries
  submitted after this date are returned.
* ``params["cache_dir"]`` (optional) — overrides the default cache directory
  ``data/literature``.

Outputs:
* ``ArtifactType.PAPER_SET`` artifact containing one entry per paper:
  ``{title, authors, year, url, pdf_url, abstract, source, tags}``.
* An ``EvalCompletedEvent`` carrying ``primary = hit_count / max_results`` so the
  outer audit can sanity-check "did the search return anything?".

Isolation invariant preserved: the artifact is the *only* side effect the outer
audit could ever read; no playbook / event history / inner-loop signal leaks.

Tests live in ``tests/test_literature_research.py``; they monkey-patch
``urllib.request.urlopen`` so the suite runs without network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from ...platform_contracts.enums import ArtifactType
from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import EvalCompletedEvent
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK


_ARXIV_API = "http://export.arxiv.org/api/query"
_ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_GITHUB_API = "https://api.github.com/search/repositories"
_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "literature",
)
_TIMEOUT = 20.0

# Orchestrator-injected blocks that are NOT part of the research topic. A goal string
# travelling through the loop accumulates playbook text, replayed experience, refine
# directives and prior-audit trends; feeding any of it to arXiv produces noise, so the
# derived query is cut at the first marker.
_SCAFFOLD_MARKERS = (
    "[PLAYBOOK",
    "[EXPERIENCE",
    "[REFINE]",
    "[AVOID]",
    "[INNER-LOOP INSTRUCTIONS]",
    "[prior outer-loop audits",
    "[RUBRIC",
)
# The MEA decomposition appends a scaffold line ("子任务 literature_search") to every
# subtask goal. It names the pipeline step, not the research topic.
_SUBTASK_LINE = re.compile(r"^\s*(子任务|subtask)\s*[:：]?\s*\S+\s*$", re.IGNORECASE)
# arXiv's free-text search degrades badly on very long strings.
_MAX_QUERY_CHARS = 200

# Fields (in priority order) that may legitimately stand in for an explicit query.
# ``name`` / ``dataset_desc`` are deliberately EXCLUDED: a display label or a data
# description is not a research topic, and silently searching on one would replace a
# caller's mistake with a plausible-looking but wrong literature set.
_QUERY_PARAM_KEYS = ("goal", "objective", "open_goal")
_QUERY_SNAPSHOT_KEYS = ("goal", "objective")


def derive_query(text: str, max_chars: int = _MAX_QUERY_CHARS) -> str:
    """Reduce a research-goal string to a usable free-text search query.

    Strips orchestrator scaffolding (playbook / experience / refine / audit blocks and
    MEA subtask marker lines), collapses whitespace and truncates. Returns ``""`` when
    nothing topical survives, so callers can keep failing closed.
    """

    s = str(text or "")
    for marker in _SCAFFOLD_MARKERS:
        idx = s.find(marker)
        if idx >= 0:
            s = s[:idx]
    lines = [ln for ln in s.splitlines() if not _SUBTASK_LINE.match(ln)]
    q = " ".join(" ".join(lines).split())
    if len(q) > max_chars:
        # Cut on a word boundary when there is one, so the query stays readable.
        cut = q[:max_chars]
        sp = cut.rfind(" ")
        q = cut[:sp] if sp > max_chars // 2 else cut
    return q.strip()


def resolve_query(params: dict[str, Any], objective_snapshot: dict[str, Any] | None) -> tuple[str, str]:
    """Resolve the effective search query. Returns ``(query, provenance)``.

    ``provenance`` is ``"explicit"`` when the caller passed ``query``, otherwise
    ``"derived:<source>"`` — recorded in the artifact + detail so a reader can always
    tell whether the search topic was stated or inferred. ``("", "")`` means no research
    goal was reachable (the caller must fail closed).
    """

    explicit = str(params.get("query") or "").strip()
    if explicit:
        return explicit, "explicit"
    for key in _QUERY_PARAM_KEYS:
        cand = derive_query(params.get(key, ""))
        if cand:
            return cand, f"derived:params.{key}"
    for key in _QUERY_SNAPSHOT_KEYS:
        cand = derive_query((objective_snapshot or {}).get(key, ""))
        if cand:
            return cand, f"derived:objective_snapshot.{key}"
    return "", ""


@dataclass
class Paper:
    title: str
    authors: list[str]
    year: int
    url: str
    pdf_url: str | None
    abstract: str
    source: str  # "arxiv" | "github"
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "abstract": self.abstract,
            "source": self.source,
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# HTTP helpers (zero-deps, mockable via ``urllib.request.urlopen`` patch)
# ---------------------------------------------------------------------------
def _http_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = _TIMEOUT) -> bytes:
    """Fetch ``url`` and return raw bytes. Raises ``RuntimeError`` on any
    transport / HTTP error so the executor can surface it as a clean failure."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json, application/xml, */*", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        body = b""
        try:
            body = exc.read()
        except Exception:  # pragma: no cover - best effort
            pass
        raise RuntimeError(
            f"literature_search HTTP {exc.code}: {body[:300]!r} (url={url})"
        ) from exc
    except urllib.error.URLError as exc:  # DNS / connection refused
        raise RuntimeError(
            f"literature_search connection failed: {exc.reason} (url={url})"
        ) from exc


# ---------------------------------------------------------------------------
# arXiv Atom XML parser — tolerates missing <arxiv:doi>, mixed author layouts.
# ---------------------------------------------------------------------------
def _parse_arxiv_entries(xml_blob: bytes, max_results: int) -> list[Paper]:
    try:
        root = ET.fromstring(xml_blob)
    except ET.ParseError as exc:
        raise RuntimeError(f"arxiv returned non-XML: {exc}") from exc

    out: list[Paper] = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        if len(out) >= max_results:
            break
        title_el = entry.find("atom:title", _ARXIV_NS)
        if title_el is None or not (title_el.text or "").strip():
            continue
        title = " ".join((title_el.text or "").split())  # collapse whitespace

        authors: list[str] = []
        for a in entry.findall("atom:author", _ARXIV_NS):
            n = a.find("atom:name", _ARXIV_NS)
            if n is not None and (n.text or "").strip():
                authors.append(n.text.strip())

        url_el = entry.find("atom:id", _ARXIV_NS)
        url = (url_el.text or "").strip() if url_el is not None else ""

        abstract_el = entry.find("atom:summary", _ARXIV_NS)
        abstract = " ".join((abstract_el.text or "").split()) if abstract_el is not None else ""

        published_el = entry.find("atom:published", _ARXIV_NS)
        year = 0
        if published_el is not None and (published_el.text or "").strip():
            try:
                year = int((published_el.text or "").strip()[:4])
            except ValueError:
                year = 0

        # Tags: best-effort arxiv primary category + any free-form <category>.
        tags: list[str] = []
        for cat in entry.findall("atom:category", _ARXIV_NS):
            term = cat.attrib.get("term")
            if term and term not in tags:
                tags.append(term)

        # arXiv id → pdf_url. Convention: arXiv/<id> in URL path.
        pdf_url: str | None = None
        if url:
            # e.g. http://export.arxiv.org/abs/2506.12345v1
            tail = url.rsplit("/", 1)[-1]
            arxiv_id = tail.split("v")[0] if "v" in tail else tail
            if arxiv_id:
                pdf_url = f"http://arxiv.org/pdf/{arxiv_id}"

        out.append(
            Paper(
                title=title,
                authors=authors,
                year=year,
                url=url,
                pdf_url=pdf_url,
                abstract=abstract,
                source="arxiv",
                tags=tags,
            )
        )
    return out


def _search_arxiv(query: str, max_results: int, since: str | None) -> list[Paper]:
    # arXiv caps max_results at 100 per call; we clamp to 50 for sanity.
    n = max(1, min(max_results, 50))
    # Build the query: wrap free-text with ``all:`` so arXiv searches the full
    # record, not just the title.
    url = f"{_ARXIV_API}?search_query=all:{_q(query)}&start=0&max_results={n}"
    if since:
        # arXiv uses YYYYMMDDHHMM submittedDate filter.
        url += f"&sortBy=submittedDate&sortOrder=descending"
        url += f"&startDate={since.replace('-', '')}000000"
    blob = _http_get(url)
    return _parse_arxiv_entries(blob, n)


def _q(text: str) -> str:
    """URL-encode a free-text query (spaces, quotes). Quotes around the entire
    query force arXiv to treat it as a phrase."""
    from urllib.parse import quote_plus

    return quote_plus(text.strip())


# ---------------------------------------------------------------------------
# GitHub search — requires ``GITHUB_TOKEN``; falls back silently otherwise.
# ---------------------------------------------------------------------------
def _search_github(query: str, max_results: int, token: str | None) -> list[Paper]:
    if not token:
        return []
    n = max(1, min(max_results, 30))
    url = f"{_GITHUB_API}?q={_q(query)}+in:name,description,readme&per_page={n}&sort=stars&order=desc"
    blob = _http_get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    obj = json.loads(blob)
    items = obj.get("items") or []
    out: list[Paper] = []
    for it in items:
        out.append(
            Paper(
                title=str(it.get("full_name") or it.get("name") or ""),
                authors=[str(it.get("owner", {}).get("login") or "")],
                year=int(str(it.get("created_at") or "0")[:4]) if it.get("created_at") else 0,
                url=str(it.get("html_url") or ""),
                pdf_url=None,
                abstract=str(it.get("description") or ""),
                source="github",
                tags=[t for t in (it.get("topics") or []) if isinstance(t, str)],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Cache: cheap dedup so repeated queries don't hit the network.
# ---------------------------------------------------------------------------
def _cache_key(query: str, sources: list[str], since: str | None) -> str:
    raw = json.dumps({"q": query, "src": sorted(sources), "since": since}, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_read(cache_dir: str, key: str) -> list[dict[str, Any]] | None:
    path = os.path.join(cache_dir, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(cache_dir: str, key: str, papers: list[dict[str, Any]]) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, f"{key}.json"), "w", encoding="utf-8") as fh:
            json.dump(papers, fh, ensure_ascii=False)
    except OSError as exc:  # pragma: no cover - non-fatal
        logging.warning("literature_search cache write failed: %s", exc)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class LiteratureResearchExecutor(StageExecutor):
    """Search arXiv (and optionally GitHub) for the requested query.

    Surfaces every paper as a structured ``Paper`` (title/authors/year/url/pdf/
    abstract/source/tags). Cached to ``data/literature/{query_hash}.json`` so
    repeated queries are free.
    """

    stage_codes = (
        "literature_research",
        "layer_01_literature_research",
        "arxiv_search",
    )

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        # An orchestrated caller (MEA subtask, agent) is already working on a research
        # goal and should not have to restate it; fall back to that goal, but only to a
        # field that genuinely IS the research topic (see ``_QUERY_PARAM_KEYS``).
        snapshot: dict[str, Any] = {}
        if not str(params.get("query") or "").strip():
            try:
                run = sdk.load_object(f"run:{stage_run.run_id}")
                snapshot = getattr(run, "objective_snapshot", None) or {}
            except Exception as exc:  # run lookup must never crash the stage
                logging.warning("literature_search could not read the run objective: %s", exc)
        query, provenance = resolve_query(params, snapshot)
        if not query:
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=(
                    "literature_research 需要 params['query']（非空）；"
                    "也未能从 params['goal'/'objective'/'open_goal'] 或 run 的研究目标推导出检索词"
                ),
            )

        max_results = int(params.get("max_results") or 20)
        sources = [str(s).lower() for s in (params.get("sources") or ["arxiv"])]
        since = params.get("since")
        cache_dir = params.get("cache_dir") or _DEFAULT_CACHE_DIR

        cache_key = _cache_key(query, sources, since)
        cached = _cache_read(cache_dir, cache_key)
        if cached is not None:
            paper_dicts = cached
            source_tag = "cache"
        else:
            papers: list[Paper] = []
            if "arxiv" in sources:
                try:
                    papers.extend(_search_arxiv(query, max_results, since))
                except Exception as exc:
                    logging.warning("arxiv search failed: %s", exc)
            if "github" in sources:
                gh_token = os.environ.get("GITHUB_TOKEN")
                try:
                    papers.extend(_search_github(query, max_results, gh_token))
                except Exception as exc:
                    logging.warning("github search failed: %s", exc)
            # Trim to max_results across all sources.
            papers = papers[:max_results]
            paper_dicts = [p.to_dict() for p in papers]
            _cache_write(cache_dir, cache_key, paper_dicts)
            source_tag = "+".join(sources) if sources else "none"

        n_hits = len(paper_dicts)
        metrics = {
            "primary": float(n_hits) / max(1, max_results),
            "hit_count": float(n_hits),
            "max_results": float(max_results),
            "query_length": float(len(query)),
            "passed": 1.0 if n_hits > 0 else 0.0,
        }

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=params.get("eval_suite_id", f"literature-{len(query)}-chars"),
            stage_run_id=stage_run.stage_run_id,
            passed=(n_hits > 0),
            metrics=metrics,
            gate_passed=(n_hits > 0),
            report_ref=f"literature://{stage_run.stage_run_id}",
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": ArtifactType.PAPER_SET.value,
                "uri": f"literature://{stage_run.stage_run_id}",
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={
                "suite": "literature_research",
                "query": query,
                # Whether the topic was stated by the caller or inferred from the goal —
                # a reader of the paper set must be able to tell.
                "query_provenance": provenance,
                "sources": sources,
                "since": since,
                "source_tag": source_tag,
                "hit_count": n_hits,
                "papers": paper_dicts,
            },
        )
        sdk.record_metric(
            stage_run.run_id, "literature.hit_count", float(n_hits), tags={"suite": "literature_research"}
        )

        return ExecResult(
            final_status=StageStatus.SUCCEEDED if n_hits > 0 else StageStatus.SUCCEEDED,  # 0 hits ≠ hard failure
            gate_result=GateResult.PASSED if n_hits > 0 else GateResult.WAIVED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"literature_search [{source_tag}] q={query!r} ({provenance}) "
                f"→ {n_hits}/{max_results} hits"
            ),
        )


__all__ = ["LiteratureResearchExecutor", "Paper", "derive_query", "resolve_query"]