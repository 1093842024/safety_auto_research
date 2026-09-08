"""LLM config + auto-label debug endpoints (B-flywheel pipeline step 2).

Lets a researcher browse the 10-model catalog, override the default Venus proxy with
their own OpenAI-compatible provider, and dry-run a few rows through the auto-label
prompt before committing to a full CSV. Mirrors the LLMClient / auto_label.toolbox in
``execution_plane.llm`` so the same code path powers both the debug surface and the
``auto_label`` capability.

All requests accept a per-call ``provider`` dict (``base_url`` / ``api_key`` /
``model`` / ``temperature`` / ``max_tokens`` / ``timeout_sec``); a request that
omits the key falls back to the env / module default. The response masks the
API key (last-4 only) so it can be round-tripped through the frontend without
leaking credentials.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from pydantic import Field

from ..deps import ControlPlaneDeps
from ...execution_plane.llm import LLMClient
from ...execution_plane.llm import LLMClientError
from ...execution_plane.llm import ProviderConfig
from ...execution_plane.llm import list_models
from ...execution_plane.llm.auto_label import DEFAULT_SYSTEM_PROMPT
from ...execution_plane.llm.auto_label import DEFAULT_USER_TEMPLATE
from ...execution_plane.llm.auto_label import label_rows


class ProviderRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_sec: float | None = None


class ChatRequest(BaseModel):
    system: str = DEFAULT_SYSTEM_PROMPT
    user: str
    provider: ProviderRequest | None = None
    model: str | None = None


class TestLabelRequest(BaseModel):
    rows: list[dict] = Field(default_factory=list)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = DEFAULT_USER_TEMPLATE
    provider: ProviderRequest | None = None
    model: str | None = None
    batch_size: int = 1


def build_llm_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the LLM debug router bound to *deps* (deps itself is unused for now but
    keeps the binding pattern consistent with every other router in the platform)."""

    router = APIRouter()

    @router.get(
        "/agent/llm/models",
        summary="List the auto-label model catalog (Venus proxy, OpenAI-compatible).",
    )
    def list_llm_models() -> dict:
        return {
            "models": list_models(),
            "default": ProviderConfig.from_env_or_request().to_dict(),
        }

    @router.get(
        "/agent/llm/provider",
        summary="Get the current default provider config (api_key masked).",
    )
    def get_provider() -> dict:
        return ProviderConfig.from_env_or_request().to_dict()

    @router.post(
        "/agent/llm/chat",
        summary="One-shot chat call (prompt debugging).",
    )
    def chat(req: ChatRequest) -> dict:
        cfg = ProviderConfig.from_env_or_request(
            base_url=(req.provider.base_url if req.provider else None),
            api_key=(req.provider.api_key if req.provider else None),
            model=(req.provider.model if req.provider else None) or req.model,
            temperature=(req.provider.temperature if req.provider else None),
            max_tokens=(req.provider.max_tokens if req.provider else None),
            timeout_sec=(req.provider.timeout_sec if req.provider else None),
        )
        client = LLMClient(cfg)
        try:
            result = client.chat(
                [
                    {"role": "system", "content": req.system},
                    {"role": "user", "content": req.user},
                ]
            )
        except LLMClientError as exc:
            logging.warning("LLM chat failure: %s", exc)
            return {"ok": False, "error": str(exc), "provider": cfg.to_dict()}
        return {
            "ok": True,
            "text": result.text,
            "model": result.model,
            "provider": cfg.to_dict(),
        }

    @router.post(
        "/agent/llm/test-label",
        summary="Dry-run auto-label on 1-N rows (returns labels + raw model output for prompt tuning).",
    )
    def test_label(req: TestLabelRequest) -> dict:
        if not req.rows:
            return {"ok": False, "error": "rows 不能为空", "labels": []}
        cfg = ProviderConfig.from_env_or_request(
            base_url=(req.provider.base_url if req.provider else None),
            api_key=(req.provider.api_key if req.provider else None),
            model=(req.provider.model if req.provider else None) or req.model,
            temperature=(req.provider.temperature if req.provider else None),
            max_tokens=(req.provider.max_tokens if req.provider else None),
            timeout_sec=(req.provider.timeout_sec if req.provider else None),
        )
        client = LLMClient(cfg)
        try:
            results = label_rows(
                req.rows,
                system_prompt=req.system_prompt,
                user_template=req.user_template,
                config=cfg,
                client=client,
                batch_size=max(1, req.batch_size),
            )
        except LLMClientError as exc:
            logging.warning("LLM test-label failure: %s", exc)
            return {"ok": False, "error": str(exc), "provider": cfg.to_dict(), "labels": []}
        return {
            "ok": True,
            "provider": cfg.to_dict(),
            "labels": [
                {"row": r.row, "label": r.label, "raw": r.raw_text, "error": r.error}
                for r in results
            ],
        }

    return router
