"""Phase D-Ext test suite -- third-party API backend + island-index helper.

Covers Task 17 (ApiLLMOperatorBackend) and the island parsing used by the
frontend "island view" (Task 18). Network is mocked, so these run without any
LLM endpoint. Run on either Python (no torch needed):

    pytest safety_auto_research/tests/test_openmle_phase_d_ext.py -q
"""

import json
import os
import unittest
import urllib.error
from unittest import mock

from safety_auto_research.openmle_integration.operators import (
    ApiLLMConfig,
    ApiLLMOperatorBackend,
    make_api_backend,
    draft_program,
    OperatorPrompt,
)
from safety_auto_research.control_plane.evolution import parse_island_index


class _Resp:
    """Minimal urlopen context-manager stand-in returning a fixed payload."""

    def __init__(self, payload: str) -> None:
        self._bytes = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._bytes


class _UrlopenRecorder:
    """urlopen stand-in that records the request and returns a chat completion."""

    def __init__(self, content: str, rec: dict) -> None:
        self._content = content
        self._rec = rec

    def __call__(self, req, timeout=None):
        self._rec["url"] = req.full_url
        self._rec["body"] = json.loads(req.data.decode("utf-8"))
        self._rec["headers"] = dict(req.headers)
        self._rec["timeout"] = timeout
        # The real proxy returns an OpenAI-style chat/completions envelope; the
        # backend parses it and pulls out choices[0].message.content.
        envelope = json.dumps({"choices": [{"message": {"content": self._content}}]})
        return _Resp(envelope)


class ApiBackendTests(unittest.TestCase):
    def _backend(self, **kw) -> ApiLLMOperatorBackend:
        cfg = ApiLLMConfig(
            api_key="SECRET_KEY",
            base_url="https://test.example.com/v1/chat/completions",
            extra_headers={"TmeOpenApi": "true"},
            **kw,
        )
        return ApiLLMOperatorBackend(cfg)

    def test_generate_posts_openai_payload_and_strips_fences(self):
        rec: dict = {}
        backend = self._backend(
            model="deepseek-v4-flash-official",
            temperature=0.6,
            max_tokens=4000,
            top_p=0.7,
            frequency_penalty=0.0,
        )
        with mock.patch(
            "urllib.request.urlopen",
            _UrlopenRecorder("```python\nprint('hello world')\n```", rec),
        ):
            out = backend.generate(
                OperatorPrompt(operator="draft", task_description="t",
                               target="Survived", id_col="PassengerId")
            )
        self.assertEqual(out, "print('hello world')")
        # URL + auth + TME header
        self.assertEqual(rec["url"], "https://test.example.com/v1/chat/completions")
        self.assertEqual(rec["headers"]["Authorization"], "Bearer SECRET_KEY")
        # HTTP header names are case-insensitive; urllib normalizes to Title-Case.
        hdr = {k.lower(): v for k, v in rec["headers"].items()}
        self.assertEqual(hdr["tmeopenapi"], "true")
        # OpenAI chat/completions shape
        body = rec["body"]
        self.assertEqual(body["model"], "deepseek-v4-flash-official")
        self.assertEqual(body["temperature"], 0.6)
        self.assertEqual(body["max_tokens"], 4000)
        self.assertEqual(body["top_p"], 0.7)
        self.assertEqual(body["frequency_penalty"], 0.0)
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["role"], "user")
        self.assertIn("operator=draft", body["messages"][1]["content"])

    def test_generate_no_fences_passthrough(self):
        rec: dict = {}
        backend = self._backend()
        with mock.patch(
            "urllib.request.urlopen",
            _UrlopenRecorder("import os\nprint('raw')\n", rec),
        ):
            out = backend.generate(
                OperatorPrompt(operator="improve", task_description="t",
                               target="S", id_col="P", current_program="x",
                               feedback="fix shape")
            )
        self.assertEqual(out, "import os\nprint('raw')")
        # feedback + current_program surfaced to the model
        content = rec["body"]["messages"][1]["content"]
        self.assertIn("current_program", content)
        self.assertIn("fix shape", content)

    def test_generate_http_error_raises_runtime(self):
        backend = self._backend()
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        err.read = lambda: b"invalid token"  # type: ignore[assignment]
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError):
                backend.generate(
                    OperatorPrompt(operator="draft", task_description="t",
                                   target="S", id_col="P")
                )

    def test_generate_urlerror_raises_runtime(self):
        backend = self._backend()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(RuntimeError):
                backend.generate(
                    OperatorPrompt(operator="draft", task_description="t",
                                   target="S", id_col="P")
                )

    def test_draft_program_uses_api_backend(self):
        rec: dict = {}
        backend = self._backend()
        program = (
            "import os\n"
            "train = pd.read_csv('train.csv')\n"
            "print('done')"
        )
        with mock.patch(
            "urllib.request.urlopen", _UrlopenRecorder(program, rec)
        ):
            out = draft_program(
                backend, target="Survived", id_col="PassengerId",
                task_description="classify titanic",
            )
        self.assertEqual(out, program)
        # inner-loop guard is still enforced (no forbidden caller)
        self.assertIn("operator=draft", rec["body"]["messages"][1]["content"])

    def test_from_env_reads_defaults(self):
        env = {
            "OPENMLE_API_BASE_URL": "https://example.test/v1/chat/completions",
            "OPENMLE_API_API_KEY": "envkey",
            "OPENMLE_API_MODEL": "env-model",
            "OPENMLE_API_TME_OPEN": "true",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = ApiLLMConfig.from_env()
        self.assertEqual(cfg.base_url, "https://example.test/v1/chat/completions")
        self.assertEqual(cfg.api_key, "envkey")
        self.assertEqual(cfg.model, "env-model")
        self.assertEqual(cfg.extra_headers.get("TmeOpenApi"), "true")

    def test_make_api_backend_convenience(self):
        b = make_api_backend(api_key="X", tme_open=True, model="m1", temperature=0.9)
        self.assertEqual(b.cfg.api_key, "X")
        self.assertEqual(b.cfg.model, "m1")
        self.assertEqual(b.cfg.temperature, 0.9)
        self.assertEqual(b.cfg.extra_headers.get("TmeOpenApi"), "true")


class IslandIndexTests(unittest.TestCase):
    def test_parse_island_index(self):
        self.assertEqual(parse_island_index("gen0.isl2"), 2)
        self.assertEqual(parse_island_index("gen1.isl0"), 0)
        self.assertEqual(parse_island_index("gen3.isl11"), 11)
        self.assertEqual(parse_island_index("gen0"), 0)  # config-level, no island
        self.assertEqual(parse_island_index(""), 0)
        self.assertEqual(parse_island_index("main"), 0)


if __name__ == "__main__":
    unittest.main()
