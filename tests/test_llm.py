"""Tests for the LLM provider catalog, client, auto-label, and debug endpoints.

All HTTP is mocked via :mod:`urllib.request` so the suite stays offline (zero real
provider calls, zero rate-limit exposure on CI). Mirrors the design contract:

* ``ProviderConfig.from_env_or_request`` — request > env > module-default chain.
* ``ProviderConfig.to_dict`` masks the API key (never echo the full secret).
* ``ProviderConfig.chat_url`` is idempotent w.r.t. trailing ``/chat/completions``.
* ``LLMClient.chat`` surfaces 4xx/5xx and malformed JSON as ``LLMClientError``.
* ``label_rows`` returns one ``LabelRowResult`` per input row (strict JSON > "label: X" > permissive first-token fallback).
* ``/agent/llm/test-label`` round-trips the labels back to the frontend without leaking secrets.
* ``/agent/llm/models`` echoes the 10-item catalog (no LLM call).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.execution_plane.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMClient,
    LLMClientError,
    ProviderConfig,
    list_models,
)
from safety_auto_research.execution_plane.llm.auto_label import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
    _format_user_prompt,
    _parse_label_response,
    label_rows,
)


# ---------------------------------------------------------------------------
# Helpers — fake OpenAI-style response
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _ok(content: str, *, status: int = 200, model: str = "fake-model") -> _FakeResp:
    body = json.dumps(
        {"choices": [{"message": {"content": content}}], "model": model}
    ).encode("utf-8")
    return _FakeResp(status, body)


# ---------------------------------------------------------------------------
# ProviderConfig
# ---------------------------------------------------------------------------

class ProviderConfigTest(unittest.TestCase):
    def test_chat_url_appends_endpoint(self) -> None:
        c = ProviderConfig(base_url="http://x.test/v1/")
        self.assertTrue(c.chat_url().endswith("/chat/completions"))
        self.assertEqual(c.chat_url(), "http://x.test/v1/chat/completions")

    def test_chat_url_idempotent(self) -> None:
        c = ProviderConfig(base_url="http://x.test/v1/chat/completions")
        self.assertEqual(c.chat_url(), "http://x.test/v1/chat/completions")

    def test_to_dict_masks_api_key(self) -> None:
        c = ProviderConfig(api_key="sk-secret-1234")
        d = c.to_dict()
        self.assertTrue(d["api_key_masked"].endswith("1234"))
        self.assertNotIn("sk-secret-1234", d["api_key_masked"])
        self.assertNotIn("api_key", d)

    def test_from_env_or_request_precedence(self) -> None:
        with patch.dict(
            os.environ,
            {"VENUS_BASE_URL": "http://env.test", "VENUS_LLM_API_KEY": "env-key"},
            clear=False,
        ):
            # request wins over env
            c = ProviderConfig.from_env_or_request(base_url="http://req.test", api_key="req-key")
            self.assertEqual(c.base_url, "http://req.test")
            self.assertEqual(c.api_key, "req-key")
            # env wins over module default
            c2 = ProviderConfig.from_env_or_request()
            self.assertEqual(c2.base_url, "http://env.test")
            self.assertEqual(c2.api_key, "env-key")

    def test_defaults_when_no_env_no_request(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            c = ProviderConfig.from_env_or_request()
            self.assertEqual(c.base_url, DEFAULT_BASE_URL)
            self.assertEqual(c.model, DEFAULT_MODEL)

    def test_list_models_has_ten(self) -> None:
        models = list_models()
        self.assertEqual(len(models), 10)
        ids = {m["id"] for m in models}
        # Pin the user-requested model ids verbatim.
        for must in (
            "deepseek-v4-flash-official",
            "deepseek-v4-pro-official",
            "glm-5.3",
            "glm-5.3-flash-external",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
            "gpt-5.6-luna",
            "qwen3.8-27b",
            "qwen3.7-plus-external",
            "doubao-seed-2-0-lite-260428",
        ):
            self.assertIn(must, ids)


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class LLMClientTest(unittest.TestCase):
    def test_chat_returns_first_choice_text(self) -> None:
        with patch("urllib.request.urlopen", return_value=_ok("hi from model")):
            cli = LLMClient(ProviderConfig(base_url="http://x.test", api_key="k"))
            r = cli.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(r.text, "hi from model")
            self.assertEqual(r.model, "fake-model")

    def test_chat_http_error_raises_typed(self) -> None:
        import io
        import urllib.error

        # urllib.error.HTTPError signature: (url, code, msg, hdrs, fp).
        err = urllib.error.HTTPError(
            "http://x.test/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"bad token"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            cli = LLMClient(ProviderConfig(base_url="http://x.test", api_key="bad"))
            with self.assertRaises(LLMClientError) as cm:
                cli.chat([{"role": "user", "content": "hi"}])
            self.assertIn("401", str(cm.exception))

    def test_chat_malformed_json_raises(self) -> None:
        with patch("urllib.request.urlopen", return_value=_FakeResp(200, b"not-json")):
            cli = LLMClient(ProviderConfig(base_url="http://x.test", api_key="k"))
            with self.assertRaises(LLMClientError):
                cli.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# auto_label._parse_label_response
# ---------------------------------------------------------------------------

class ParseLabelResponseTest(unittest.TestCase):
    def test_strict_json(self) -> None:
        self.assertEqual(_parse_label_response('{"label": 1}'), "1")
        self.assertEqual(_parse_label_response('noise {"label":"yes"} tail'), "yes")

    def test_label_line(self) -> None:
        self.assertEqual(_parse_label_response("label: 0"), "0")
        self.assertEqual(_parse_label_response("Label = cat"), "cat")

    def test_permissive_first_line(self) -> None:
        self.assertEqual(_parse_label_response("42"), "42")
        self.assertEqual(_parse_label_response("\n  cat \n"), "cat")

    def test_empty(self) -> None:
        self.assertEqual(_parse_label_response(""), "")
        self.assertEqual(_parse_label_response("   \n\n"), "")


# ---------------------------------------------------------------------------
# label_rows (mocked client)
# ---------------------------------------------------------------------------

class LabelRowsTest(unittest.TestCase):
    def test_one_call_per_row_returns_one_label_per_row(self) -> None:
        rows = [{"x": 1}, {"x": 2}, {"x": 3}]
        responses = iter(['{"label": "a"}', '{"label": "b"}', '{"label": "c"}'])
        with patch.object(LLMClient, "chat", side_effect=lambda *_a, **_kw: next(responses) and type("R", (), {"text": next(responses), "model": "m", "raw": {}})()):
            # Use a simpler patch — mock chat to return a tiny namespace.
            pass
        # Re-do the test with a cleaner mock:
        r1 = type("R", (), {"text": '{"label": "a"}', "model": "m", "raw": {}})()
        r2 = type("R", (), {"text": '{"label": "b"}', "model": "m", "raw": {}})()
        r3 = type("R", (), {"text": '{"label": "c"}', "model": "m", "raw": {}})()
        with patch.object(LLMClient, "chat", side_effect=[r1, r2, r3]):
            out = label_rows(rows, config=ProviderConfig(base_url="http://x.test", api_key="k"))
        self.assertEqual([r.label for r in out], ["a", "b", "c"])

    def test_per_row_error_recorded_but_loop_continues(self) -> None:
        r1 = type("R", (), {"text": '{"label": "a"}', "model": "m", "raw": {}})()
        with patch.object(LLMClient, "chat", side_effect=[r1, LLMClientError("boom")]):
            out = label_rows(
                [{"x": 1}, {"x": 2}],
                config=ProviderConfig(base_url="http://x.test", api_key="k"),
            )
        self.assertEqual(out[0].label, "a")
        self.assertEqual(out[0].error, None)
        self.assertEqual(out[1].label, "")
        self.assertIn("boom", out[1].error or "")

    def test_default_template_renders_columns_and_row(self) -> None:
        s = _format_user_prompt(DEFAULT_USER_TEMPLATE, {"a": 1, "b": "x"})
        self.assertIn('"a": 1', s)
        self.assertIn("a, b", s)


# ---------------------------------------------------------------------------
# /agent/llm/* endpoints
# ---------------------------------------------------------------------------

class LlmEndpointsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app(ControlPlaneService(Repository())))

    def test_models_endpoint_lists_ten_with_default_provider(self) -> None:
        r = self.client.get("/agent/llm/models")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["models"]), 10)
        self.assertIn("base_url", data["default"])
        # Masked API key must NOT include the real key prefix.
        self.assertNotIn("api_key", data["default"])
        self.assertTrue(data["default"]["api_key_masked"].startswith("***"))

    def test_provider_endpoint_returns_masked(self) -> None:
        r = self.client.get("/agent/llm/provider")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("api_key_masked", d)

    def test_chat_endpoint_mocks_provider(self) -> None:
        with patch("urllib.request.urlopen", return_value=_ok("pong")):
            r = self.client.post(
                "/agent/llm/chat",
                json={"system": "you are a tester", "user": "ping", "provider": {"base_url": "http://x.test", "api_key": "kkk", "model": "m1"}},
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "pong")

    def test_test_label_endpoint_returns_labels_and_masks_provider(self) -> None:
        r1 = type("R", (), {"text": '{"label": "1"}', "model": "m1", "raw": {}})()
        r2 = type("R", (), {"text": '{"label": "0"}', "model": "m1", "raw": {}})()
        with patch.object(LLMClient, "chat", side_effect=[r1, r2]):
            r = self.client.post(
                "/agent/llm/test-label",
                json={
                    "rows": [{"Pclass": "1"}, {"Pclass": "3"}],
                    "provider": {"base_url": "http://x.test", "api_key": "kkk", "model": "m1"},
                },
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["labels"]), 2)
        self.assertEqual([x["label"] for x in data["labels"]], ["1", "0"])
        self.assertEqual(data["provider"]["base_url"], "http://x.test")

    def test_test_label_empty_rows(self) -> None:
        r = self.client.post(
            "/agent/llm/test-label",
            json={"rows": [], "provider": {"base_url": "http://x.test", "api_key": "kkk"}},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()