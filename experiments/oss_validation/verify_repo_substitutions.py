"""End-to-end verification that the three OSS research repos now route their LLM
traffic through the tmeoa gateway instead of their original vendor APIs/weights.

For each repo we exercise the *actual* client entry point the repo uses, just
redirected at tmeoa:

* AutoResearchClaw  -> researchclaw.llm.client.LLMClient (OpenAI-compatible;
                       TmeOpenApi header injected via extra_headers)
* ARA               -> anthropic.Anthropic().messages.create (via anthropic_shim)
* Auto-claude       -> tools/meta_opt/trigger_eval.run_probe with LLM_BACKEND=tmeoa

Each check records a real completion so we know the substitution is live, not
just syntactically wired.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path("/Users/glennge/work/github/AI_research")
OSS = REPO_ROOT / "safety_auto_research" / "experiments" / "oss_validation"
ARC_ROOT = REPO_ROOT / "AutoResearchClaw"
AUTOC_ROOT = REPO_ROOT / "Auto-claude-code-research-in-sleep"

sys.path.insert(0, str(OSS))  # tmeoa package
sys.path.insert(0, str(ARC_ROOT))  # researchclaw package

from tmeoa import client as tmeoa  # noqa: E402

TOKEN = tmeoa.TOKEN_TEXT
TMEOA_BASE = "https://ai-rec.tmeoa.com/llmproxy"
PRIMARY = "deepseek-v4-flash-official"
ALT = "qwen3.5-397b-a17b"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_arc() -> dict:
    from researchclaw.llm.client import LLMClient, LLMConfig

    cfg = LLMConfig(
        base_url=TMEOA_BASE,
        api_key=TOKEN,
        wire_api="chat_completions",
        primary_model=PRIMARY,
        fallback_models=["deepseek-v4-pro-official", "qwen3.6-35b-a3b", ALT],
        extra_headers={"TmeOpenApi": "true"},
        max_tokens=2000,
    )
    client = LLMClient(cfg)
    ok, msg = client.preflight()
    resp = client.chat(
        [{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=2000,
        temperature=0,
    )
    # Also prove an explicit alternate model works through the same client.
    resp2 = client.chat(
        [{"role": "user", "content": "Reply with exactly: PONG"}],
        model=ALT,
        max_tokens=2000,
        temperature=0,
    )
    return {
        "repo": "AutoResearchClaw",
        "entry": "researchclaw.llm.client.LLMClient",
        "preflight_ok": ok,
        "preflight_msg": msg,
        "primary_model": PRIMARY,
        "primary_content_nonempty": bool(resp.content.strip()),
        "alt_model": ALT,
        "alt_content_nonempty": bool(resp2.content.strip()),
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
    }


def check_ara() -> dict:
    from tmeoa.anthropic_shim import install

    install()  # monkey-patch anthropic.Anthropic -> tmeoa shim
    import anthropic  # stubbed module now carries our shim

    client = anthropic.Anthropic()  # type: ignore[attr-defined]
    resp = client.messages.create(
        model=PRIMARY,
        max_tokens=2000,
        temperature=0,
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
    )
    text = resp.content[0].text
    return {
        "repo": "Agent-Native-Research-Artifact",
        "entry": "anthropic.Anthropic().messages.create (via anthropic_shim)",
        "mapped_model": resp.model,
        "content": text.strip()[:80],
        "content_nonempty": bool(text.strip()),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


def check_autoclaude() -> dict:
    os.environ["LLM_BACKEND"] = "tmeoa"
    os.environ["TMEOA_CLIENT_DIR"] = str(OSS)
    os.environ.setdefault("LLM_MODEL", PRIMARY)

    mod = _load_module(
        "trigger_eval_tmeoa",
        AUTOC_ROOT / "tools" / "meta_opt" / "trigger_eval.py",
    )
    stream = mod.run_probe("Summarise the idea of gradient descent in one sentence.", None, 120, "/tmp")
    # The tmeoa backend wraps the reply as a single assistant text event.
    parsed = json.loads(stream.strip().splitlines()[0])
    text = parsed["message"]["content"][0]["text"]
    return {
        "repo": "Auto-claude-code-research-in-sleep",
        "entry": "tools/meta_opt/trigger_eval.run_probe (LLM_BACKEND=tmeoa)",
        "model": os.environ["LLM_MODEL"],
        "content_nonempty": bool(text.strip()),
        "content_preview": text.strip()[:80],
    }


def main() -> None:
    results: dict = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gateway": f"{TMEOA_BASE}/chat/completions",
        "checks": [],
    }
    for name, fn in [
        ("AutoResearchClaw", check_arc),
        ("ARA", check_ara),
        ("Auto-claude", check_autoclaude),
    ]:
        try:
            r = fn()
            r["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - record and continue
            r = {"repo": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        results["checks"].append(r)
        print(f"[{r['status']}] {name}: {r.get('entry', '')}")

    out = OSS / "repo_substitution_results.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
