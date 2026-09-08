"""Provider catalog + config for the LLM client (``execution_plane.llm.client``).

A provider is an OpenAI-compatible chat-completions endpoint (Venus proxy is the
default, but any vendor that speaks the protocol works). The catalog is the canonical
list of model ids the auto-label layer advertises; the ``ProviderConfig`` is what the
client actually sends to the wire.

Everything here is data + tiny validators — no I/O, no SDK imports, so it stays cheap to
import in unit tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


# Venus proxy is the company-default OpenAI-compatible gateway. Callers append
# ``/chat/completions``; see :class:`LLMClient`.
DEFAULT_BASE_URL = "http://v2.open.venus.oa.com/llmproxy"
# Match the precedent set by ``experiments/oss_validation/tmeoa_client.py``: the API
# key is read from an env var with a project-default fallback. Auto-label tests can
# override ``api_key`` per request.
DEFAULT_API_KEY_ENV = "VENUS_LLM_API_KEY"
DEFAULT_API_KEY_FALLBACK = "ynavvi42pCCJ26ISQuWSlwld@3897"
DEFAULT_MODEL = "deepseek-v4-flash-official"
DEFAULT_TIMEOUT_SEC = 60.0


# The 10 models the auto-label layer advertises in the catalog. The ``id`` is the
# model name sent to the provider's chat/completions endpoint; the ``label`` is the
# human-friendly name shown in the UI. Families are a coarse grouping the frontend
# uses to colour-code the picker (cheap / flagship / external).
MODEL_CATALOG: list[dict[str, str]] = [
    {"id": "deepseek-v4-flash-official", "label": "DeepSeek V4 Flash（官方）", "family": "cheap"},
    {"id": "deepseek-v4-pro-official", "label": "DeepSeek V4 Pro（官方）", "family": "flagship"},
    {"id": "glm-5.3", "label": "GLM 5.3", "family": "flagship"},
    {"id": "glm-5.3-flash-external", "label": "GLM 5.3 Flash（外部）", "family": "cheap"},
    {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "family": "cheap"},
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "family": "cheap"},
    {"id": "gpt-5.6-luna", "label": "GPT 5.6 Luna", "family": "flagship"},
    {"id": "qwen3.8-27b", "label": "Qwen 3.8 27B", "family": "cheap"},
    {"id": "qwen3.7-plus-external", "label": "Qwen 3.7 Plus（外部）", "family": "flagship"},
    {"id": "doubao-seed-2-0-lite-260428", "label": "豆包 Seed 2.0 Lite", "family": "cheap"},
]


@dataclass
class ProviderConfig:
    """One provider configuration (URL + auth + model). All fields overridable per call."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 512
    timeout_sec: float = DEFAULT_TIMEOUT_SEC

    @classmethod
    def from_env_or_request(
        cls,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_sec: float | None = None,
    ) -> "ProviderConfig":
        """Resolve a config, letting the request win over env, env over the fallback.

        Order (highest to lowest priority):
          1. explicit request argument,
          2. matching env var (``VENUS_BASE_URL`` / ``VENUS_LLM_API_KEY`` / ``VENUS_LLM_MODEL``),
          3. module-level default.
        """
        return cls(
            base_url=base_url or os.environ.get("VENUS_BASE_URL", DEFAULT_BASE_URL),
            api_key=api_key
            or os.environ.get(DEFAULT_API_KEY_ENV)
            or DEFAULT_API_KEY_FALLBACK,
            model=model or os.environ.get("VENUS_LLM_MODEL", DEFAULT_MODEL),
            temperature=float(temperature if temperature is not None else 0.2),
            max_tokens=int(max_tokens if max_tokens is not None else 512),
            timeout_sec=float(timeout_sec if timeout_sec is not None else DEFAULT_TIMEOUT_SEC),
        )

    def to_dict(self) -> dict[str, Any]:
        # api_key is sensitive — only echo the last 4 chars, never the full key.
        masked = f"***{self.api_key[-4:]}" if self.api_key else ""
        return {
            "base_url": self.base_url,
            "api_key_masked": masked,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_sec": self.timeout_sec,
        }

    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        # Standard OpenAI layout: ``{base}/chat/completions``. If the base already
        # ends with ``/chat/completions`` we leave it alone (idempotent).
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


def list_models() -> list[dict[str, str]]:
    """Return the public model catalog (id + label + family)."""
    return list(MODEL_CATALOG)


def find_model(model_id: str) -> dict[str, str] | None:
    for m in MODEL_CATALOG:
        if m["id"] == model_id:
            return m
    return None
