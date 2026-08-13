"""tmeoa LLM client — drop-in substitute for the GPT-4o calls used by OSS benchmarks.

The endpoint `https://ai-rec.tmeoa.com/llmproxy/chat/completions` exposes
`deepseek-v4-flash-official`, a *reasoning* model: it streams `reasoning_content`
before the final `content`. We therefore request a generous `max_tokens` and read
`choices[0].message.content` for the actual answer.

This module is the single place that knows the auth header / model name, so the
rest of the validation harness can call `chat(...)` the same way it would call an
OpenAI GPT-4o client.
"""
from __future__ import annotations

import json
import os
import urllib.request

TMEOA_URL = "https://ai-rec.tmeoa.com/llmproxy/chat/completions"
TMEOA_TOKEN = os.environ.get(
    "TMEOA_TOKEN", "ynavvi42pCCJ26ISQuWSlwld@3897"
)
MODEL = os.environ.get("TMEOA_MODEL", "deepseek-v4-flash-official")

# Reasoning models burn tokens before emitting `content`; keep headroom.
DEFAULT_MAX_TOKENS = 2000


def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
    timeout: int = 120,
) -> dict:
    """Minimal OpenAI-compatible chat completion.

    Returns the parsed JSON response dict (so callers can read
    ``resp["choices"][0]["message"]["content"]``).
    """
    payload = {
        "model": model or MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        TMEOA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TMEOA_TOKEN}",
            "TmeOpenApi": "true",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat_text(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Convenience wrapper returning just the assistant text content."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = chat(messages, temperature=temperature, max_tokens=max_tokens)
    return resp["choices"][0]["message"].get("content", "") or ""


if __name__ == "__main__":
    out = chat_text("Reply with the single word: OK")
    print("tmeoa says:", repr(out))
