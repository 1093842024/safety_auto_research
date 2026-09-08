"""Auto-label capability (B-flywheel pipeline step 2).

Companion to :class:`BadcaseRetrainExecutor`: when the badcase source has no ground
truth (online eval failures / user feedback / red-team inputs), this capability
calls the configured LLM (Venus proxy by default, any OpenAI-compatible endpoint
works) to predict the target column. The result is a labeled CSV that the flywheel
``badcase_retrain`` step can consume unchanged.

Inputs (params, all overridable from the request / ``FlywheelRequest.auto_label``):
  * ``input_path``      — CSV of unlabeled rows (must NOT already have ``target_col``).
  * ``output_path``     — where the labeled CSV is written (default: ``<input_path>.labeled``).
  * ``target_col``      — the column to add / predict (default: ``"label"``).
  * ``drop_cols``       — columns to drop from the row sent to the LLM (e.g. PII / ids).
  * ``system_prompt``   — override the default system prompt.
  * ``user_template``   — override the default user template (``{row}`` + ``{columns}``).
  * ``provider``        — ``ProviderConfig`` dict (base_url / api_key / model / ...).
  * ``max_rows``        — safety cap (default 500) to avoid runaway cost on big uploads.
  * ``batch_size``      — rows per chat call (1 = one call per row).

Emits a real ``EvalCompletedEvent`` (the B-flywheel pipeline shape: it pairs with
``badcase_retrain``) and writes the labeled CSV as an artifact. Provider outages
become a regular ``StageStatus.FAILED`` so the outer flywheel loop can roll back
cleanly instead of silently accepting empty labels.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import EvalCompletedEvent
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..llm import LLMClient
from ..llm import LLMClientError
from ..llm import ProviderConfig
from ..llm.auto_label import DEFAULT_SYSTEM_PROMPT
from ..llm.auto_label import DEFAULT_USER_TEMPLATE
from ..llm.auto_label import label_rows
from ..sdk import PlatformSDK


class AutoLabelExecutor(StageExecutor):
    """Label an unlabeled badcase CSV via the LLM, producing a supervised retrain input."""

    stage_codes = ("auto_label", "flywheel_auto_label", "llm_label")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        import pandas as pd

        input_path = params.get("input_path")
        if not input_path:
            raise ValueError("auto_label 需要 params['input_path']（待标注 CSV）")
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"待标注 CSV 不存在: {input_path}")

        target_col = params.get("target_col", "label")
        drop_cols = list(params.get("drop_cols") or [])
        output_path = params.get("output_path") or f"{input_path}.labeled"
        max_rows = int(params.get("max_rows", 500))
        batch_size = int(params.get("batch_size", 1))

        provider_dict = dict(params.get("provider") or {})
        config = ProviderConfig.from_env_or_request(
            base_url=provider_dict.get("base_url"),
            api_key=provider_dict.get("api_key"),
            model=provider_dict.get("model"),
            temperature=provider_dict.get("temperature"),
            max_tokens=provider_dict.get("max_tokens"),
            timeout_sec=provider_dict.get("timeout_sec"),
        )

        system_prompt = str(params.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
        user_template = str(params.get("user_template") or DEFAULT_USER_TEMPLATE)

        df = pd.read_csv(input_path)
        if len(df) > max_rows:
            df = df.head(max_rows).copy()

        rows: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            row = {c: r[c] for c in df.columns if c not in drop_cols and c != target_col}
            rows.append({str(k): (None if pd.isna(v) else v) for k, v in row.items()})

        client = LLMClient(config)
        try:
            results = label_rows(
                rows,
                system_prompt=system_prompt,
                user_template=user_template,
                config=config,
                client=client,
                batch_size=batch_size,
            )
        except LLMClientError as exc:
            logging.exception("auto_label provider failure for run=%s", stage_run.run_id)
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=None,
                output_refs=[],
                detail=f"LLM 提供方调用失败: {exc}",
            )

        n_ok = sum(1 for r in results if r.error is None and r.label != "")
        n_err = sum(1 for r in results if r.error is not None or r.label == "")

        # Write the labeled CSV. Rows the LLM failed to label keep an empty target
        # so a downstream human-review step can spot them.
        out_df = df.copy()
        if target_col in out_df.columns:
            existing = out_df[target_col].tolist()
        else:
            existing = [None] * len(out_df)
        new_labels = [r.label for r in results]
        merged: list[Any] = []
        for i in range(len(out_df)):
            new = new_labels[i]
            old = existing[i]
            if new:
                merged.append(new)
            elif old is not None and old != "":
                merged.append(old)
            else:
                merged.append(new)  # empty string -> downstream sees a blank target
        out_df[target_col] = merged
        out_df.to_csv(output_path, index=False)

        metrics: dict[str, float] = {
            "primary": float(n_ok) / max(1, len(results)),
            "labeled_count": float(n_ok),
            "error_count": float(n_err),
            "total_count": float(len(results)),
            "passed": 1.0 if n_err == 0 else 0.0,
        }

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=params.get("eval_suite_id", f"auto-label-{target_col}"),
            stage_run_id=stage_run.stage_run_id,
            passed=(n_err == 0),
            metrics=metrics,
            gate_passed=(n_err == 0),
            report_ref=f"auto-label://{stage_run.stage_run_id}",
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": "eval_report",
                "uri": f"file://{output_path}",
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={
                "suite": "auto_label",
                "provider": config.to_dict(),
                "target_col": target_col,
                "labeled_csv": output_path,
                "labeled_count": n_ok,
                "error_count": n_err,
            },
        )
        sdk.record_metric(stage_run.run_id, "auto_label.labeled_count", float(n_ok), tags={"suite": "auto_label"})
        sdk.record_metric(stage_run.run_id, "auto_label.error_count", float(n_err), tags={"suite": "auto_label"})

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if n_err == 0 else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"Auto-label [{config.model}]: {n_ok}/{len(results)} labeled, "
                f"{n_err} errors -> {output_path}"
            ),
        )
