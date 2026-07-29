"""Cumulative research-state store — HypothesisTree + ExperienceBank + StrategyArchive.

This is the persistence foundation for the dual loop (Phase 0/3). It is **opt-in and
backward compatible**: the existing in-memory ``Repository`` is untouched, so all 48
existing tests stay green. A dual-loop run attaches one of these stores (memory by default,
or SQLite-backed) so that:

  * the HypothesisTree links hypotheses ↔ artifacts ↔ evidence ↔ insights across rounds
    (Arbor-style), with failed branches explicitly retained as "stepping stones";
  * the ExperienceBank holds training-free pass/fail lessons reinjected into later runs
    (Contextual Experience Replay);
  * the StrategyArchive records mechanism-carrier snapshots with rollback ids, so the
    recursive-improvement meta-loop can revert a degrading change (validate-and-revert).

The AREX-style ``compact()`` method produces an ``improvement_state`` that preserves
verified findings, unresolved constraints, **and rejected candidates** — the key to
escaping local optima.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Any

from ..platform_contracts.objects import ExperienceEntry
from ..platform_contracts.objects import HypothesisNode


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class HypoTreeStore:
    """Arbor-style hypothesis tree persisted across rounds (memory or SQLite)."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._nodes: dict[str, HypothesisNode] = {}
        if db_path:
            self._init_db()

    # ------------------------------------------------------------------ db
    def _init_db(self) -> None:
        assert self.db_path is not None
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hypo_nodes ("
                "node_id TEXT PRIMARY KEY, parent_id TEXT, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS experiences ("
                "entry_id TEXT PRIMARY KEY, kind TEXT, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS strategies ("
                "rollback_id TEXT PRIMARY KEY, data TEXT)"
            )
            conn.commit()
        self._load_memory()

    def _load_memory(self) -> None:
        assert self.db_path is not None
        with sqlite3.connect(self.db_path) as conn:
            for nid, _pid, data in conn.execute("SELECT node_id, parent_id, data FROM hypo_nodes"):
                self._nodes[nid] = HypothesisNode.model_validate_json(data)

    def _persist_node(self, node: HypothesisNode) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hypo_nodes VALUES (?,?,?)",
                (node.node_id, node.parent_id, node.model_dump_json()),
            )
            conn.commit()

    # ------------------------------------------------------------------ tree ops
    def observe(
        self,
        hypothesis: str,
        evidence_refs: list[str] | None = None,
        parent_id: str | None = None,
        artifact_ref: str | None = None,
        branch: str = "main",
        score: float = 0.0,
        run_id: str | None = None,
    ) -> HypothesisNode:
        """Add a hypothesis node (Arbor ``observe`` / ``ideate``)."""

        node = HypothesisNode(
            node_id=_new_id("node"),
            parent_id=parent_id,
            hypothesis=hypothesis,
            evidence_refs=evidence_refs or [],
            artifact_ref=artifact_ref,
            branch=branch,
            score=score,
            status="active",
            run_id=run_id,
        )
        self._nodes[node.node_id] = node
        self._persist_node(node)
        return node

    def backpropagate(self, node_id: str, insight: str, score: float | None = None) -> None:
        """Attach an insight and propagate the score up the parent chain (Arbor ``decide``).

        Failed branches are NOT deleted — they are marked ``pruned`` (stepping stones),
        preserving diversity for the meta-loop's archive.
        """

        node = self._nodes.get(node_id)
        if node is None:
            return
        if insight:
            node.insight = insight
        if score is not None:
            node.score = max(0.0, min(1.0, score))
        self._persist_node(node)
        # climb the chain, smoothing scores upward
        cur = node
        while cur.parent_id and cur.parent_id in self._nodes:
            parent = self._nodes[cur.parent_id]
            parent.score = round(max(parent.score, cur.score * 0.9), 4)
            self._persist_node(parent)
            cur = parent

    def mark_pruned(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.status = "pruned"
        self._persist_node(node)

    def mark_merged(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.status = "merged"
        self._persist_node(node)

    def get(self, node_id: str) -> HypothesisNode | None:
        return self._nodes.get(node_id)

    def list_nodes(self, branch: str | None = None, run_id: str | None = None) -> list[HypothesisNode]:
        out = list(self._nodes.values())
        if branch is not None:
            out = [n for n in out if n.branch == branch]
        if run_id is not None:
            out = [n for n in out if n.run_id == run_id]
        return out

    def snapshot(self, run_id: str | None = None) -> dict[str, Any]:
        nodes = [n for n in self._nodes.values() if run_id is None or n.run_id == run_id]
        return {
            "nodes": [n.model_dump(mode="json") for n in nodes],
        }

    # ------------------------------------------------------------------ AREX compact
    def compact(
        self,
        keep: list[str] | None = None,
        unresolved: list[str] | None = None,
        rejected_candidates: list[str] | None = None,
        next_plan: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Build the ``improvement_state`` at a turning point (AREX ``update_context``)."""

        keep = keep or ["verified", "unresolved", "rejected_candidates", "next_plan"]
        nodes = [n for n in self._nodes.values() if run_id is None or n.run_id == run_id]
        verified = [
            {"node_id": n.node_id, "hypothesis": n.hypothesis, "score": n.score, "insight": n.insight}
            for n in nodes
            if n.status == "active" and n.score >= 0.6
        ]
        stepping_stones = [
            {"node_id": n.node_id, "hypothesis": n.hypothesis, "insight": n.insight}
            for n in nodes
            if n.status == "pruned"
        ]
        state: dict[str, Any] = {}
        if "verified" in keep:
            state["verified"] = verified
        if "unresolved" in keep and unresolved is not None:
            state["unresolved"] = unresolved
        if "rejected_candidates" in keep and rejected_candidates is not None:
            state["rejected_candidates"] = rejected_candidates
        if "next_plan" in keep:
            state["next_plan"] = next_plan
        # always preserve stepping stones (the anti-local-optima safeguard)
        state["stepping_stones"] = stepping_stones
        return state


class ExperienceBank:
    """Training-free experience bank (Contextual Experience Replay), cross-run reusable."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._entries: dict[str, ExperienceEntry] = {}
        if db_path:
            self._init_db()

    def _init_db(self) -> None:
        assert self.db_path is not None
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS experiences (entry_id TEXT PRIMARY KEY, kind TEXT, data TEXT)"
            )
            conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            for eid, _kind, data in conn.execute("SELECT entry_id, kind, data FROM experiences"):
                self._entries[eid] = ExperienceEntry.model_validate_json(data)

    def _persist(self, e: ExperienceEntry) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiences VALUES (?,?,?)",
                (e.entry_id, e.kind, e.model_dump_json()),
            )
            conn.commit()

    def add(
        self,
        kind: str,
        context: str,
        lesson: str,
        applicable_stages: list[str] | None = None,
        confidence: float = 0.5,
    ) -> ExperienceEntry:
        e = ExperienceEntry(
            entry_id=_new_id("exp"),
            kind=kind,
            context=context,
            lesson=lesson,
            applicable_stages=applicable_stages or [],
            confidence=confidence,
        )
        self._entries[e.entry_id] = e
        self._persist(e)
        return e

    def query(self, stage: str | None = None, k: int = 5) -> list[ExperienceEntry]:
        """Retrieve up to ``k`` experiences, optionally filtered by applicable stage."""

        out = list(self._entries.values())
        if stage is not None:
            out = [e for e in out if stage in e.applicable_stages]
        out.sort(key=lambda e: e.confidence, reverse=True)
        return out[:k]

    def list_all(self) -> list[ExperienceEntry]:
        return list(self._entries.values())


class StrategyArchive:
    """Mechanism-carrier history with rollback ids (Darwin Gödel Machine-style archive)."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._strategies: dict[str, dict[str, Any]] = {}
        if db_path:
            self._init_db()

    def _init_db(self) -> None:
        assert self.db_path is not None
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS strategies (rollback_id TEXT PRIMARY KEY, data TEXT)"
            )
            conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            for rid, data in conn.execute("SELECT rollback_id, data FROM strategies"):
                self._strategies[rid] = json.loads(data)

    def _persist(self, rid: str, data: dict[str, Any]) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO strategies VALUES (?,?)", (rid, json.dumps(data))
            )
            conn.commit()

    def commit(self, snapshot: dict[str, Any]) -> str:
        """Store a mechanism snapshot; return its rollback id."""

        rid = _new_id("rb")
        self._strategies[rid] = {"rollback_id": rid, **snapshot}
        self._persist(rid, self._strategies[rid])
        return rid

    def get(self, rollback_id: str) -> dict[str, Any] | None:
        return self._strategies.get(rollback_id)

    def rollback(self, rollback_id: str) -> dict[str, Any] | None:
        return self._strategies.get(rollback_id)


class ResearchStateStore:
    """Convenience bundle of the three stores, sharing one SQLite path (or all in-memory)."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self.hypo_tree = HypoTreeStore(db_path)
        self.experience_bank = ExperienceBank(db_path)
        self.strategy_archive = StrategyArchive(db_path)
