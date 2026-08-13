"""Anthropic Messages API -> tmeoa (OpenAI chat) translation shim.

ARA's ``seal`` pipeline (``docs/the-ara-of-ara/src/seal/seal.py`` and
``seal/execution_agent.py``) instantiates ``anthropic.Anthropic()`` and calls
``client.messages.create(...)``. Pointing ``base_url`` at tmeoa is not enough:
the two APIs differ on the wire, not just in address.

=========================  ==============================  ============================
concern                    Anthropic Messages              OpenAI chat completions
=========================  ==============================  ============================
system prompt              top-level ``system=`` kwarg      a ``{"role": "system"}`` msg
response text              ``resp.content[0].text``         ``choices[0].message.content``
stop reason                ``stop_reason`` (``end_turn``)   ``finish_reason`` (``stop``)
token counts               ``usage.input_tokens``           ``usage.prompt_tokens``
max tokens                 required                         optional
image part                 ``{"type": "image", "source":    ``{"type": "image_url",
                            {"type": "base64", ...}}``       "image_url": {"url": ...}}``
=========================  ==============================  ============================

:class:`AnthropicShim` is a duck-typed stand-in exposing ``.messages.create``,
so ARA code works unchanged after swapping the constructor. It returns an object
whose attribute access mimics the Anthropic response, including ``.content[0].text``.

Model names are mapped from Claude identifiers onto the four mandated tmeoa
models; an unrecognised name falls back to the default rather than failing, since
ARA passes model strings through from config in several places.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import DEFAULT_TEXT_MODEL, TEXT_MODELS, chat

# Claude tiers map onto tmeoa models of comparable role: the "fast/small" tier
# goes to the flash model, the "frontier" tier to the largest available.
MODEL_MAP: dict[str, str] = {
    "haiku": "deepseek-v4-flash-official",
    "sonnet": "deepseek-v4-pro-official",
    "opus": "qwen3.5-397b-a17b",
}


def map_model(name: str | None) -> str:
    """Translate a Claude-style model id to a tmeoa model id."""
    if not name:
        return DEFAULT_TEXT_MODEL
    if name in TEXT_MODELS:
        return name
    lowered = name.lower()
    for tier, target in MODEL_MAP.items():
        if tier in lowered:
            return target
    return DEFAULT_TEXT_MODEL


@dataclass
class _TextBlock:
    """Mimics ``anthropic.types.TextBlock``."""

    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Message:
    """Mimics ``anthropic.types.Message`` closely enough for ARA's call sites."""

    content: list[_TextBlock]
    model: str
    stop_reason: str = "end_turn"
    role: str = "assistant"
    type: str = "message"
    usage: _Usage = field(default_factory=_Usage)

    @property
    def text(self) -> str:
        """Convenience accessor — concatenates every text block."""
        return "".join(block.text for block in self.content)


def _convert_content(content: Any) -> Any:
    """Convert Anthropic content blocks to OpenAI content parts."""
    if isinstance(content, str):
        return content

    parts: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({"type": "text", "text": str(block)})
            continue

        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                mime = source.get("media_type", "image/png")
                data = source.get("data", "")
                url = f"data:{mime};base64,{data}"
            else:
                url = source.get("url", "")
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_result":
            # Flatten tool results to text; tmeoa has no tool-result role.
            parts.append({"type": "text", "text": str(block.get("content", ""))})
        else:
            parts.append({"type": "text", "text": str(block)})

    # A single text part is more widely accepted as a bare string.
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "stop_sequence",
}


class _Messages:
    """The ``client.messages`` namespace."""

    def __init__(self, shim: AnthropicShim) -> None:
        self._shim = shim

    def create(
        self,
        *,
        model: str | None = None,
        messages: list[dict] | None = None,
        system: Any = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> _Message:
        """Anthropic-signature ``messages.create`` backed by tmeoa.

        Unsupported Anthropic-only kwargs (``tools``, ``stop_sequences``,
        ``metadata``, ...) are accepted and ignored rather than raising, so ARA's
        richer call sites still return a usable completion. ``tools`` is the one
        that changes semantics, so it is surfaced on the returned object via
        ``stop_reason`` staying ``end_turn`` and no tool_use block appearing —
        callers that require tool calling must not use this shim.
        """
        oai_messages: list[dict] = []

        if system:
            # Anthropic allows system to be a string or a list of blocks.
            sys_text = (
                system
                if isinstance(system, str)
                else "".join(
                    b.get("text", "") for b in system if isinstance(b, dict)
                )
            )
            if sys_text:
                oai_messages.append({"role": "system", "content": sys_text})

        for msg in messages or []:
            oai_messages.append(
                {
                    "role": msg.get("role", "user"),
                    "content": _convert_content(msg.get("content", "")),
                }
            )

        vision = any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for m in oai_messages
        )

        resp = chat(
            oai_messages,
            model=map_model(model),
            temperature=temperature,
            max_tokens=max(int(max_tokens), 2000),
            vision=vision,
        )
        choice = resp["choices"][0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = resp.get("usage") or {}

        return _Message(
            content=[_TextBlock(text=text)],
            model=resp.get("model", map_model(model)),
            stop_reason=_STOP_REASON.get(choice.get("finish_reason", "stop"), "end_turn"),
            usage=_Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )


class AnthropicShim:
    """Drop-in replacement for ``anthropic.Anthropic()``.

    Usage inside ARA (one-line change at each construction site)::

        # client = anthropic.Anthropic()
        from tmeoa.anthropic_shim import AnthropicShim
        client = AnthropicShim()

    ``api_key``/``base_url`` are accepted and ignored so existing keyword
    arguments do not need removing.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, **_: Any) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.messages = _Messages(self)


def install() -> None:
    """Monkey-patch the installed ``anthropic`` module to route through tmeoa.

    This is the zero-source-edit option for ARA: import this module before ARA's
    ``seal`` code constructs its client. If the real ``anthropic`` package is not
    installed, a stub module is registered so ``import anthropic`` still works.
    """
    import sys
    import types

    try:
        import anthropic  # type: ignore
    except ImportError:
        anthropic = types.ModuleType("anthropic")
        sys.modules["anthropic"] = anthropic

    anthropic.Anthropic = AnthropicShim  # type: ignore[attr-defined]
    anthropic.Client = AnthropicShim  # type: ignore[attr-defined]
