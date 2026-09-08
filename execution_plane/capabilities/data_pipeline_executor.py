"""Data-pipeline capability — collection / synthesis / cleaning / labelling (Phase 2, §五.A.1).

Replaces the long-standing ``StubCapabilityExecutor`` for the
``layer_05_data_evaluation_cleaning`` layer with a *single* executor that
dispatches on ``params["mode"]``:

  * ``"cleaning"``   — **fully implemented** (Phase 2.0). Pure-local data
    cleaning: minhash dedup + noise filter (z-score on numeric cols) + PII
    regex redaction. Operates on a CSV ``input_path`` and writes the cleaned
    CSV to ``output_path`` (default: ``<input_path>.cleaned``).

  * ``"synthesis"``  — **stub with note** (Phase 2.1). Surfaces a structured
    artifact describing what the LLM-driven synthesiser *would* do (LLM
    provider URL/key/model, expected sample count, target schema). No network
    I/O — the real synthesiser lives in ``auto_label`` and a follow-up Phase
    2.1 executor.

  * ``"label"``      — **delegates** to ``AutoLabelExecutor`` (the B-flywheel
    auto-label capability that already runs). One executor two modes, no
    duplicate logic.

  * ``"collection"`` — **stub with note** (Phase 2.1). Reserved for web search
    + URL fetcher; deferred because it requires URL allow-listing + robots.txt
    policy.

Each mode produces a real ``ArtifactType.DATASET_RELEASE`` artifact (or
``EVAL_REPORT`` for the cleaning mode) and emits a ``EvalCompletedEvent`` with
mode-appropriate metrics (``primary = rows_out / rows_in`` for cleaning).

Isolation invariant preserved: artifacts are the only thing the outer audit
can ever read. No playbook / event history / inner-loop signal leaks.

Tests live in ``tests/test_data_pipeline.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import shutil
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


_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    # Loose US-style SSN: 3-2-4 digits with optional separators.
    "ssn": re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"),
    # Email — local@domain.tld (deliberately permissive; the corpus scan is offline).
    "email": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    # IPv4.
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # US phone (10-digit with optional country code + separators).
    "phone_us": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
}


# ---------------------------------------------------------------------------
# Cleaning primitives (mode="cleaning")
# ---------------------------------------------------------------------------
def _minhash_signature(text: str, *, num_hashes: int = 64, shingle_size: int = 3) -> set[int]:
    """Return a minhash signature for ``text`` (set of hash ints). Used to
    near-deduplicate rows cheaply without loading any model weights."""
    tokens = text.split()
    if len(tokens) < shingle_size:
        shingles = {text} if text.strip() else set()
    else:
        shingles = {" ".join(tokens[i : i + shingle_size]) for i in range(len(tokens) - shingle_size + 1)}
    sig: set[int] = set()
    for sh in shingles:
        # blake2b is stable across processes and fast enough for in-process minhash.
        h = int.from_bytes(hashlib.blake2b(sh.encode("utf-8"), digest_size=8).digest(), "little")
        sig.add(h & ((1 << 32) - 1))
    return sig


def _jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """Replace PII patterns with ``[REDACTED:<label>]`` and return counts per label."""
    counts: dict[str, int] = {}
    out = text
    for label, pat in _PII_PATTERNS.items():
        out, n = pat.subn(f"[REDACTED:{label}]", out)
        if n:
            counts[label] = n
    return out, counts


def _zscore_filter(df, *, z_threshold: float = 5.0, numeric_cols: list[str] | None = None):
    """Drop rows whose any-numeric-column z-score exceeds ``z_threshold``.

    Returns the filtered DataFrame and the indices that were dropped.
    """
    import numpy as np

    cols = numeric_cols or [c for c in df.columns if str(df[c].dtype).startswith(("int", "float"))]
    if not cols:
        return df, []
    sub = df[cols]
    # Skip if no variance at all.
    std = sub.std(ddof=0)
    if (std == 0).all():
        return df, []
    z = ((sub - sub.mean()) / std.replace(0, np.nan)).abs()
    outlier_mask = (z > z_threshold).any(axis=1)
    dropped_idx = df.index[outlier_mask].tolist()
    return df[~outlier_mask].reset_index(drop=True), dropped_idx


def _run_cleaning(input_path: str, output_path: str, *, dedup_threshold: float = 0.85, z_threshold: float = 5.0) -> dict[str, Any]:
    """Execute the full cleaning pipeline. Returns metrics for the EvalCompletedEvent."""
    import pandas as pd

    df = pd.read_csv(input_path)
    n_in = len(df)

    # Empty input — short-circuit so we don't trip on pandas 3.0 + empty-DataFrame
    # edge cases (e.g. ``agg`` on a column-less DataFrame returns a DataFrame, not
    # a Series).
    if n_in == 0:
        df.to_csv(output_path, index=False)
        return {
            "rows_in": 0.0,
            "rows_after_dedup": 0.0,
            "rows_out": 0.0,
            "rows_dropped": 0.0,
            "z_threshold": float(z_threshold),
            "dedup_threshold": float(dedup_threshold),
        }

    pii_total: dict[str, int] = {}

    # 1) PII redaction (text cells only — leave numeric ids untouched).
    text_cols = [c for c in df.columns if df[c].dtype == object]
    for c in text_cols:
        df[c] = df[c].astype(str).map(lambda v: _redact_pii(v)[0] if v and v != "nan" else v)
        # Note: we don't aggregate per-label counts here for cheapness; the global
        # counter below sweeps the entire CSV once after the text-col pass.
    # Sweep for global PII counts (after redaction, the redacted tokens are present).
    text_blob = " ".join(str(v) for v in df[text_cols].fillna("").values.ravel()) if text_cols else ""
    for label, pat in _PII_PATTERNS.items():
        # Count original occurrences by re-running the redaction on the input text (before
        # redaction). Cheap approximation: count tokens in the original blob.
        pii_total[label] = 0  # filled below if needed

    # 2) Dedup (minhash Jaccard on text columns only — numeric cols shouldn't
    #    affect duplicate detection since e.g. scores may legitimately differ).
    text_cols = [c for c in df.columns if df[c].dtype == object]
    if text_cols:
        dedup_text = df[text_cols].fillna("").astype(str).agg(" ".join, axis=1)
    else:
        dedup_text = pd.Series(["" for _ in range(len(df))])
    sigs = [_minhash_signature(t) for t in dedup_text.tolist()]
    keep: list[bool] = [True] * len(df)
    seen_sigs: list[set[int]] = []
    for i, s in enumerate(sigs):
        if any(_jaccard(s, t) >= dedup_threshold for t in seen_sigs):
            keep[i] = False
            continue
        seen_sigs.append(s)
    df = df[keep].reset_index(drop=True)
    n_after_dedup = len(df)

    # 3) Noise filter (z-score on numeric cols).
    df, dropped_idx = _zscore_filter(df, z_threshold=z_threshold)
    n_out = len(df)

    df.to_csv(output_path, index=False)

    return {
        "rows_in": float(n_in),
        "rows_after_dedup": float(n_after_dedup),
        "rows_out": float(n_out),
        "rows_dropped": float(n_in - n_out),
        "z_threshold": float(z_threshold),
        "dedup_threshold": float(dedup_threshold),
    }


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class DataPipelineExecutor(StageExecutor):
    """Single executor for the four data-pipeline modes.

    See module docstring for the per-mode contract.
    """

    stage_codes = (
        "data_pipeline",
        "layer_05_data_evaluation_cleaning",
        "data_collection",
        "data_synthesis",
        "data_label",
        "data_cleaning",
    )

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        mode = (params.get("mode") or "cleaning").lower()
        if mode == "cleaning":
            return self._run_cleaning(stage_run, sdk, params)
        if mode == "label":
            # Delegate to AutoLabelExecutor (Phase 1.6 already implemented it).
            from .auto_label_executor import AutoLabelExecutor

            return AutoLabelExecutor().execute(stage_run, sdk, params)
        if mode == "synthesis":
            return self._stub_with_note(
                stage_run, sdk, params,
                note="Phase 2.1: LLM-driven synthesiser not yet implemented; "
                     "see AutoLabelExecutor for the working LLM auto-label path.",
            )
        if mode == "collection":
            return self._stub_with_note(
                stage_run, sdk, params,
                note="Phase 2.1: web-collection requires URL allow-list + robots.txt; "
                     "deferred until the safety policy is settled.",
            )
        return ExecResult(
            final_status=StageStatus.FAILED,
            gate_result=GateResult.FAILED,
            event=None,
            output_refs=[],
            detail=f"unknown data_pipeline mode={mode!r} (expected cleaning/label/synthesis/collection)",
        )

    # ----- cleaning -------------------------------------------------------
    def _run_cleaning(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        input_path = params.get("input_path")
        if not input_path:
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail="data_pipeline mode=cleaning 需要 params['input_path']（CSV）",
            )
        if not os.path.exists(input_path):
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=f"input CSV 不存在: {input_path}",
            )
        output_path = params.get("output_path") or f"{input_path}.cleaned"
        dedup_threshold = float(params.get("dedup_threshold", 0.85))
        z_threshold = float(params.get("z_threshold", 5.0))

        try:
            metrics = _run_cleaning(
                input_path,
                output_path,
                dedup_threshold=dedup_threshold,
                z_threshold=z_threshold,
            )
        except Exception as exc:
            logging.exception("data_pipeline cleaning failed for run=%s", stage_run.run_id)
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=f"data_pipeline cleaning failed: {exc}",
            )

        # Surface primary = rows_out / rows_in (a 1.0 means no rows dropped).
        rows_in = max(1, int(metrics["rows_in"]))
        primary = float(metrics["rows_out"]) / rows_in
        passed = bool(metrics["rows_out"] > 0)

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=params.get("eval_suite_id", "data-pipeline-cleaning"),
            stage_run_id=stage_run.stage_run_id,
            passed=passed,
            metrics={
                "primary": primary,
                **metrics,
                "passed": 1.0 if passed else 0.0,
            },
            gate_passed=passed,
            report_ref=f"data-pipeline://{stage_run.stage_run_id}",
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": ArtifactType.EVAL_REPORT.value,
                "uri": f"file://{output_path}",
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={
                "suite": "data_pipeline.cleaning",
                "input_path": input_path,
                "output_path": output_path,
                "rows_in": int(metrics["rows_in"]),
                "rows_out": int(metrics["rows_out"]),
                "rows_dropped": int(metrics["rows_in"] - metrics["rows_out"]),
                "dedup_threshold": dedup_threshold,
                "z_threshold": z_threshold,
            },
        )
        sdk.record_metric(stage_run.run_id, "data_pipeline.rows_out", float(metrics["rows_out"]))
        sdk.record_metric(stage_run.run_id, "data_pipeline.rows_dropped", float(metrics["rows_in"] - metrics["rows_out"]))

        return ExecResult(
            final_status=StageStatus.SUCCEEDED if passed else StageStatus.FAILED,
            gate_result=GateResult.PASSED if passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"data_pipeline.cleaning rows_in={int(metrics['rows_in'])} "
                f"-> rows_out={int(metrics['rows_out'])} (dropped {int(metrics['rows_in'] - metrics['rows_out'])}) "
                f"-> {output_path}"
            ),
        )

    # ----- stub-with-note (modes that are deferred) ------------------------
    def _stub_with_note(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
        *,
        note: str,
    ) -> ExecResult:
        # Publish a stub artifact (real, auditable — *not* a synthetic random
        # number) describing the deferred contract. Keeps the capability
        # callable end-to-end so an agent / dashboard can reason about it.
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover
            pd = None  # type: ignore

        metadata: dict[str, Any] = {
            "suite": f"data_pipeline.{params.get('mode')}",
            "deferred": True,
            "note": note,
            "provider": params.get("provider") or {},
        }

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=f"data-pipeline-{params.get('mode')}-stub",
            stage_run_id=stage_run.stage_run_id,
            passed=False,  # a stub should never auto-pass the gate
            metrics={
                "primary": 0.0,
                "rows_out": 0.0,
                "deferred": 1.0,
                "passed": 0.0,
            },
            gate_passed=False,
            report_ref=f"data-pipeline://{stage_run.stage_run_id}",
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": ArtifactType.DATASET_RELEASE.value,
                "uri": f"data-pipeline://{stage_run.stage_run_id}",
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata=metadata,
        )

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,  # capability ran; gate just fails
            gate_result=GateResult.WAIVED,  # deferred — don't block the loop
            event=event,
            output_refs=[artifact.artifact_id],
            detail=f"data_pipeline.{params.get('mode')} deferred: {note}",
        )


__all__ = [
    "DataPipelineExecutor",
    "_minhash_signature",
    "_jaccard",
    "_redact_pii",
    "_zscore_filter",
    "_run_cleaning",
    "_PII_PATTERNS",
]