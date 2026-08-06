"""ACE-style evolving playbook — itemized, deterministically-merged research context.

Inspired by ACE (Agentic Context Engineering, Zhang et al. 2025, cf. Lilian Weng's
harness-engineering survey): the context an inner loop runs with should be a *curated
playbook* of itemized bullets — not an ever-growing prompt and not a lossy full rewrite.

Design invariants (mirroring ACE, adapted to a deterministic, no-LLM reflector):

  * **Itemized entries** — every bullet has a stable id, a section
    (``strategy`` / ``failure`` / ``fact``), helpful/harmful counters, and provenance
    (``run_id`` + outer-iteration) so later verification can tag it.
  * **Deterministic curator** — merging dedups on a normalized (scope, section, text)
    key; a repeated observation *increments counters* instead of appending a near-
    duplicate bullet. This is the anti-brevity-bias / anti-context-collapse safeguard.
  * **Scoped isolation** — entries are scoped per benchmark task (cross-run learning,
    like the ExperienceBank) and are injected ONLY into the inner loop
    (``inner_params["playbook_context"]`` / the agent goal). They must never flow into
    the curated outer-audit input (dual-loop isolation invariant #1).

The reflector here is deliberately rule-based: given a platform run's curated signals
(inner metrics, audit verdict, unresolved claims, rejected candidates, mined failure
modes) it emits candidate bullets; an LLM reflector can later replace ``reflect_round``
without touching the store or the orchestrator wiring.

THREAD-SAFETY (缺陷3 fix): the playbook is a shared, mutable dict accessed from multiple
concurrent background runs, so every dict-touching method is guarded by a reentrant lock.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _norm_key(text: str) -> str:
    """Normalized dedup key: lowercase, collapse non-alphanumeric runs."""

    return re.sub(r"[^a-z0-9一-鿿]+", " ", text.lower()).strip()


@dataclass
class PlaybookEntry:
    entry_id: str
    scope: str
    section: str  # "strategy" | "failure" | "fact"
    content: str
    helpful: int = 0
    harmful: int = 0
    run_id: str | None = None
    iter_no: int = 0

    @property
    def score(self) -> int:
        return self.helpful - self.harmful


class PlaybookStore:
    """Itemized playbook with deterministic dedup-merge (memory or SQLite-backed)."""

    def __init__(self, db_path: str | None = None) -> None:
        self._lock = threading.RLock()
        self.db_path = db_path
        self._entries: dict[str, PlaybookEntry] = {}
        if db_path:
            self._init_db()

    # ------------------------------------------------------------------ db
    def _init_db(self) -> None:
        assert self.db_path is not None
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS playbook_entries ("
                "entry_id TEXT PRIMARY KEY, scope TEXT, data TEXT)"
            )
            conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            for eid, _scope, data in conn.execute(
                "SELECT entry_id, scope, data FROM playbook_entries"
            ):
                self._entries[eid] = PlaybookEntry(**json.loads(data))

    def _persist(self, entry: PlaybookEntry) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO playbook_entries VALUES (?,?,?)",
                (entry.entry_id, entry.scope, json.dumps(asdict(entry), ensure_ascii=False)),
            )
            conn.commit()

    # ------------------------------------------------------------------ curator
    def merge(
        self,
        candidates: list[dict[str, Any]],
        *,
        scope: str,
        run_id: str | None = None,
        iter_no: int = 0,
    ) -> list[PlaybookEntry]:
        """Deterministically merge candidate bullets into the playbook.

        Dedup key = (scope, section, normalized content). A candidate that matches an
        existing entry only increments that entry's helpful/harmful counters — the
        playbook grows by *evidence*, not by near-duplicate text.
        """

        with self._lock:
            index = {
                (e.scope, e.section, _norm_key(e.content)): e for e in self._entries.values()
            }
            touched: list[PlaybookEntry] = []
            for cand in candidates:
                content = str(cand.get("content", "")).strip()
                if not content:
                    continue
                section = str(cand.get("section", "fact"))
                key = (scope, section, _norm_key(content))
                existing = index.get(key)
                if existing is not None:
                    existing.helpful += int(cand.get("helpful", 0))
                    existing.harmful += int(cand.get("harmful", 0))
                    existing.iter_no = iter_no  # last-reinforced marker
                    self._persist(existing)
                    touched.append(existing)
                    continue
                entry = PlaybookEntry(
                    entry_id=_new_id("pb"),
                    scope=scope,
                    section=section,
                    content=content,
                    helpful=int(cand.get("helpful", 0)),
                    harmful=int(cand.get("harmful", 0)),
                    run_id=run_id,
                    iter_no=iter_no,
                )
                self._entries[entry.entry_id] = entry
                index[key] = entry
                self._persist(entry)
                touched.append(entry)
            return touched

    def tag(self, entry_id: str, *, helpful: bool) -> None:
        """Post-hoc verification feedback: mark a bullet as having (not) paid off."""

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return
            if helpful:
                entry.helpful += 1
            else:
                entry.harmful += 1
            self._persist(entry)

    # ------------------------------------------------------------------ retrieval
    def list_entries(self, scope: str | None = None) -> list[PlaybookEntry]:
        with self._lock:
            out = list(self._entries.values())
            if scope is not None:
                out = [e for e in out if e.scope == scope]
            return out

    def render(self, scope: str, k: int = 6) -> str:
        """Render the top-k bullets for prompt/param injection (inner loop only)."""

        entries = self.list_entries(scope)
        entries.sort(key=lambda e: (e.score, e.helpful, e.iter_no), reverse=True)
        lines = []
        for e in entries[:k]:
            tag = {"strategy": "策略", "failure": "避坑", "fact": "事实"}.get(e.section, e.section)
            lines.append(f"- [{tag}|{e.entry_id}] {e.content} (+{e.helpful}/-{e.harmful})")
        return "\n".join(lines)


# ---------------------------------------------------------------------- reflector
def reflect_round(
    *,
    capability: str,
    inner_params: dict[str, Any],
    metrics: dict[str, Any],
    decision: str,
    confidence: float | None = None,
    unresolved: list[str] | None = None,
    rejected: list[str] | None = None,
    failure_modes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Rule-based Reflector: distill one dual-loop round into candidate bullets.

    ``decision`` is the router verdict value (``exit_success`` / ``revisit`` / ...).
    Positive evidence (accepted) and negative evidence (refine/restart, rejected
    candidates, recurring failure modes) are separated into sections so the curator
    can count them independently.
    """

    cands: list[dict[str, Any]] = []
    model = inner_params.get("model", "?")
    fe = inner_params.get("fe", "?")
    acc = metrics.get("accuracy")
    if isinstance(acc, (int, float)) and acc > 0:
        if decision == "exit_success":
            verdict, helpful, harmful = "外审计通过", 1, 0
        elif decision == "revisit":
            verdict, helpful, harmful = "被要求改进", 0, 1
        else:
            verdict, helpful, harmful = f"裁决={decision}", 0, 1
        cands.append(
            {
                "section": "strategy",
                "content": (
                    f"{capability}(model={model}, fe={fe}) → acc={acc:.4f}，{verdict}"
                ),
                "helpful": helpful,
                "harmful": harmful,
            }
        )
    for r in rejected or []:
        cands.append(
            {
                "section": "failure",
                "content": f"已被外审计否决，勿重复该方向：{r}",
                "harmful": 1,
            }
        )
    for fm in failure_modes or []:
        count = fm.get("count", 1)
        if count >= 2:  # only recurring patterns are worth a bullet
            cands.append(
                {
                    "section": "failure",
                    "content": f"反复出现的失败模式（{count} 次）：{fm.get('mode')}",
                    "harmful": 1,
                }
            )
    for u in unresolved or []:
        cands.append({"section": "fact", "content": f"待解决约束：{u}"})
    return cands
