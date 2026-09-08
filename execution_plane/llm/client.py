"""OpenAI-compatible LLM client (zero external dependencies).

Wraps the chat/completions protocol so the platform can call any vendor that speaks
OpenAI (Venus proxy is the company default, but the URL is fully overridable). Kept
deliberately small — no streaming, no tool calls, no vision — so the auto-label layer
can depend on a stable, auditable surface and a unit test that monkey-patches
:func:`urllib.request.urlopen`.

Error model: a single ``LLMClientError`` carries the vendor's status / body verbatim;
the auto-label executor surfaces it as a regular ExecResult.failure so a label
provider outage never crashes a flywheel iteration.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .providers import ProviderConfig


class LLMClientError(Exception):
    """Raised when the LLM provider is unreachable, returns a non-2xx, or the body is
    unparseable. Carries the wire status / body so callers can log + debug."""


@dataclass
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResult:
    text: str
    model: str
    raw: dict[str, Any]


class LLMClient:
    """Thin chat/completions client. Stateless; safe to share across threads."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or ProviderConfig.from_env_or_request()

    # ----- core ---------------------------------------------------------------
    def chat(
        self,
        messages: list[ChatMessage] | list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_sec: float | None = None,
    ) -> ChatResult:
        """Send a chat-completions request. Returns the first choice's text + raw body.

        Raises :class:`LLMClientError` on any transport/parse/validation failure.
        """
        cfg = self.config
        body = json.dumps(
            {
                "model": model or cfg.model,
                "messages": [
                    m if isinstance(m, dict) else m.to_dict() for m in messages
                ],
                "temperature": float(temperature if temperature is not None else cfg.temperature),
                "max_tokens": int(max_tokens if max_tokens is not None else cfg.max_tokens),
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        url = cfg.chat_url()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec or cfg.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise LLMClientError(
                f"LLM 调用失败 HTTP {exc.code}: {body[:500] or exc.reason} (url={url})"
            ) from exc
        except Exception as exc:  # transport-level: timeout, DNS, refused, ...
            raise LLMClientError(f"LLM 调用失败: {exc} (url={url})") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMClientError(
                f"LLM 响应非合法 JSON (status={status}): {raw[:500]}"
            ) from exc

        try:
            text = str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMClientError(
                f"LLM 响应缺少 choices[0].message.content: {raw[:500]}"
            ) from exc
        return ChatResult(text=text, model=str(data.get("model", model or cfg.model)), raw=data)


def quick_chat(
    system: str,
    user: str,
    *,
    config: ProviderConfig | None = None,
    model: str | None = None,
) -> str:
    """One-liner: build a 2-message chat and return the text. For tests + ad-hoc calls."""
    client = LLMClient(config=config or ProviderConfig.from_env_or_request())
    return client.chat(
        [ChatMessage("system", system), ChatMessage("user", user)],
        model=model,
    ).text


# Re-export so callers can do ``from execution_plane.llm import ChatMessage``.
__all__ = [
    "ChatMessage",
    "ChatResult",
    "LLMClient",
    "LLMClientError",
    "ProviderConfig",
    "quick_chat",
]
