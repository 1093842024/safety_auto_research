"""Transports carry agent<->platform protocol messages (see :mod:`protocol`).

A transport sends a Platform->Agent *task* message and returns the Agent->Platform
*terminal* response. During the exchange the agent may issue ``ToolCall`` messages;
the transport invokes ``tool_handler(tool, args)`` (which dispatches to
``PlatformSDK``) and feeds back a ``ToolResult``. This lets an *external* agent call
platform tools while still running as a separate process.

Transports provided
-------------------
* ``CallableTransport``  - for tests / embedding (wraps a Python callable)
* ``SubprocessTransport`` - runs a CLI agent speaking the line-delimited JSON protocol
* ``CLIAgentTransport``  - base for *driving* a CLI agent (Codex / Claude Code) as a
  real tool-calling agent: it runs the CLI, parses its JSON, executes the tool calls,
  and **streams every step into a trace sink** so the run record captures the agent's
  internal execution flow.
* ``CodexTransport``      - concrete ``CLIAgentTransport`` for the Codex CLI.
* ``ClaudeCodeTransport``- concrete ``CLIAgentTransport`` for the Claude Code CLI.
* ``HttpTransport``       - skeleton for a remote agent reached over HTTP.
"""

from __future__ import annotations

import json
import os
import subprocess
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Callable
from typing import Optional

from .protocol import TERMINAL_TYPES


ToolHandler = Callable[[str, dict[str, Any]], Any]
TraceSink = Callable[[str, dict[str, Any]], None]


def _summarize(obj: Any, n: int = 500) -> str:
    """Render an arbitrary object as a compact, capped string for the agent trace."""

    if obj is None:
        return ""
    if isinstance(obj, str):
        s = obj
    else:
        try:
            s = json.dumps(obj, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(obj)
    if len(s) > n:
        s = s[: n - 1] + "…"
    return s


class Transport(ABC):
    """Carries one Platform->Agent task and returns the agent's terminal response."""

    @abstractmethod
    def request(
        self,
        message: dict[str, Any],
        tool_handler: ToolHandler | None = None,
        trace_sink: TraceSink | None = None,
    ) -> dict[str, Any]:
        """Send a platform->agent task; return the agent's terminal response dict.

        ``trace_sink`` (if provided) is called with ``(run_id, step_dict)`` for every
        agent execution step (tool call / final answer) so callers can persist the
        agent's internal flow.
        """


class CallableTransport(Transport):
    """Wraps a Python callable: ``fn(message, tool_handler) -> response_dict``.

    For backward compatibility a one-argument callable ``fn(message) -> response_dict``
    is also accepted (tool_handler is then dropped).
    """

    def __init__(self, fn: Callable[..., dict[str, Any]]) -> None:
        self._fn = fn

    def request(
        self, message: dict[str, Any], tool_handler: ToolHandler | None = None,
        trace_sink: TraceSink | None = None,
    ) -> dict[str, Any]:
        try:
            return self._fn(message, tool_handler)
        except TypeError:
            # Legacy one-argument callable: it does not accept tool_handler.
            return self._fn(message)


class SubprocessTransport(Transport):
    """Runs an agent command; line-delimited JSON over stdin/stdout.

    Wire protocol (one JSON object per line)::

        platform -> agent : TaskDecide | TaskRunStage
        agent    -> platform: ToolCall  (zero or more; platform answers with ToolResult)
        platform -> agent : ToolResult   (answer to each ToolCall)
        agent    -> platform: AgentDecision | AgentStageResult  (terminal, ends turn)

    When ``trace_sink`` is supplied, every ``agent.tool_call`` message is streamed to it
    so the agent's internal execution flow is recorded.
    """

    def __init__(self, agent_command: str, trace_sink: TraceSink | None = None) -> None:
        self.agent_command = agent_command
        self._trace_sink = trace_sink

    def request(
        self, message: dict[str, Any], tool_handler: ToolHandler | None = None,
        trace_sink: TraceSink | None = None,
    ) -> dict[str, Any]:
        sink = trace_sink or self._trace_sink
        proc = subprocess.Popen(
            self.agent_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            shell=True,
            bufsize=1,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

        run_id = (message.get("run_id") or (message.get("run") or {}).get("run_id"))
        terminal: dict[str, Any] | None = None
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            msg = json.loads(raw)
            msg_type = msg.get("msg_type")
            if msg_type in TERMINAL_TYPES:
                terminal = msg
                break
            if msg_type == "agent.tool_call":
                call_id = msg.get("call_id")
                tool = msg.get("tool")
                args = msg.get("args", {}) or {}
                try:
                    result = (
                        tool_handler(msg["tool"], args)
                        if tool_handler is not None
                        else None
                    )
                    ok, error = True, None
                except Exception as exc:  # surface tool errors back to the agent
                    result, ok, error = None, False, str(exc)
                if sink is not None:
                    sink(run_id, {
                        "seq": 0,
                        "kind": "tool_call",
                        "tool": tool,
                        "args_summary": _summarize(args),
                        "result_summary": _summarize({"ok": ok, "result": result, "error": error}),
                    })
                proc.stdin.write(
                    json.dumps(
                        {
                            "msg_type": "platform.tool_result",
                            "call_id": call_id,
                            "ok": ok,
                            "result": result,
                            "error": error,
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()

        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if terminal is None:
            raise RuntimeError(
                f"agent {self.agent_command!r} returned no terminal message"
            )
        return terminal


class HttpTransport(Transport):
    """Skeleton for a remote agent reached over HTTP (e.g. a Codex / WorkBuddy session).

    The agent is an HTTP service: POST the task to ``endpoint`` and read the JSON
    response. Tool calls are performed by the agent hitting the platform's own SDK
    endpoints (to be added: control-plane ``/sdk/...``), so this transport only
    delivers the task + the response. Install ``requests`` and flesh out ``_post``
    for production use — this class exists to mark the integration seam.
    """

    def __init__(
        self, endpoint: str, headers: Optional[dict[str, str]] = None
    ) -> None:
        self.endpoint = endpoint
        self.headers = headers or {}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise NotImplementedError(
                "HttpTransport requires `requests`. For an in-process or CLI agent use "
                "CallableTransport / SubprocessTransport instead."
            ) from exc
        resp = requests.post(
            self.endpoint, json=payload, headers=self.headers, timeout=600
        )
        resp.raise_for_status()
        return resp.json()

    def request(
        self, message: dict[str, Any], tool_handler: ToolHandler | None = None,
        trace_sink: TraceSink | None = None,
    ) -> dict[str, Any]:
        # Tool calls over HTTP are served by the platform's own SDK endpoints, which the
        # remote agent calls directly; this transport just delivers task + response.
        return self._post(message)


class CLIAgentTransport(Transport):
    """Base class for *driving* a CLI agent (Codex / Claude Code) as a real tool-caller.

    Each ``request`` launches one CLI session with the task + running conversation and
    parses the agent's JSON reply. If the agent emits a ``tool_call`` JSON object, the
    transport executes it — ``run_capability`` via the injected ``capability_runner``
    (so it has the ``run_id``), or any SDK tool via ``tool_handler`` — feeds the
    ``tool_result`` back, loops until the agent returns a ``final`` JSON (an
    ``AgentDecision`` / ``AgentStageResult``). Every step is streamed to ``trace_sink``
    (defaulting to ``self._trace_sink``) so the agent's internal execution flow ends up
    in the run record. This is the concrete seam that makes Codex / Claude Code *real*
    agents driving the platform's infrastructure capabilities.

    Subclasses implement ``_call_cli`` (how to actually invoke the CLI binary).
    """

    def __init__(
        self,
        capability_runner: Callable[..., Any],
        tool_handler: ToolHandler | None = None,
        model: str | None = None,
        max_tool_rounds: int = 8,
        timeout: int = 240,
        extra_args: list[str] | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._capability_runner = capability_runner
        self._tool_handler = tool_handler
        self.model = model
        self.max_tool_rounds = max_tool_rounds
        self.timeout = timeout
        self.extra_args = extra_args or []
        self._trace_sink = trace_sink

    # ----------------------------------------------------------------- entry
    def request(
        self, message: dict[str, Any], tool_handler: ToolHandler | None = None,
        trace_sink: TraceSink | None = None,
    ) -> dict[str, Any]:
        th = tool_handler or self._tool_handler
        sink = trace_sink or self._trace_sink
        run_id = self._extract_run_id(message)
        context: list[dict[str, Any]] = [{"role": "platform", "message": message}]

        for i in range(self.max_tool_rounds):
            prompt = self._render(context)
            raw = self._call_cli(prompt)
            parsed = _extract_json(raw)
            if parsed is None:
                return self._fallback(message, raw)
            if parsed.get("type") == "tool_call":
                tool = parsed.get("tool")
                args = parsed.get("args", {}) or {}
                result = self._exec_tool(run_id, tool, args, th)
                if sink is not None:
                    sink(run_id, {
                        "seq": i,
                        "kind": "tool_call",
                        "tool": tool,
                        "args_summary": _summarize(args),
                        "result_summary": _summarize(result),
                    })
                context.append({"role": "agent", "message": parsed})
                context.append(
                    {"role": "platform", "message": {"type": "tool_result", "tool": tool, "result": result}}
                )
            else:
                if sink is not None:
                    sink(run_id, {
                        "seq": i + 1,
                        "kind": "final",
                        "tool": None,
                        "args_summary": "",
                        "result_summary": _summarize(parsed.get("message", parsed)),
                    })
                return parsed.get("message", parsed)

        return self._fallback(message, "max tool rounds exceeded")

    # --------------------------------------------------------------- CLI hook
    @abstractmethod
    def _call_cli(self, prompt: str) -> str:
        """Invoke the CLI binary with ``prompt`` and return its stdout."""

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _extract_run_id(message: dict[str, Any]) -> str | None:
        if "run_id" in message and message["run_id"]:
            return message["run_id"]
        spec = message.get("spec") or {}
        rc = spec.get("run_context") or {}
        if rc.get("run_id"):
            return rc["run_id"]
        return (message.get("run") or {}).get("run_id")

    def _exec_tool(
        self, run_id: str, tool: str, args: dict[str, Any], tool_handler: ToolHandler | None
    ) -> dict[str, Any]:
        if tool == "run_capability":
            cid = args.get("capability_id")
            params = args.get("params", {}) or {}
            if run_id is None or cid is None:
                return {"ok": False, "error": "run_capability needs run_id + capability_id"}
            try:
                # C2 fix: the CLI path must enforce the SAME outer-loop reservation as
                # the harness tool handler — an inner-loop agent may never invoke the
                # external audit (layer_11) or the meta-loop (layer_09).
                from .harness import assert_inner_capability_allowed

                assert_inner_capability_allowed(cid)
                stage, result = self._capability_runner(run_id, cid, params)
                return {
                    "ok": True,
                    "stage_run_id": stage.stage_run_id,
                    "layer_code": stage.stage_code,
                    "event": result.event.model_dump(mode="json") if result.event else None,
                    "gate_result": result.gate_result.value,
                    "output_refs": list(result.output_refs),
                    "detail": result.detail,
                }
            except Exception as exc:  # surface tool errors back to the agent
                return {"ok": False, "error": str(exc)}
        # Other SDK tools go through the harness tool handler.
        try:
            res = tool_handler(tool, args) if tool_handler else None
            return {"ok": True, "result": res}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _render(self, context: list[dict[str, Any]]) -> str:
        sys_prompt = (
            "You are an autonomous ML research agent connected to a research platform over a "
            "strict JSON tool protocol. You will receive the current task and the conversation "
            "history (already-executed tool results). You MUST reply with EXACTLY ONE JSON "
            "object and nothing else (no prose, no markdown fences).\n\n"
            "Respond with one of:\n"
            '1) A tool call:\n'
            '   {"type":"tool_call","tool":"<tool>","args":{...}}\n'
            "   Tools:\n"
            '     - "run_capability": args {"capability_id": <str>, "params": <obj>} — run an '
            'infrastructure-layer capability. PRIMARY tool. Example capability_id "kaggle_eval" '
            'with params {"model":"gbm","cv_folds":5} trains a real model and returns accuracy.\n'
            '     - SDK tools: "load_object"(ref), "emit_event"(event), "record_metric"'
            '(run_id,name,value,tags), "register_lesson"(payload), "publish_artifact"'
            '(payload,schema_version,metadata), "request_approval"(run_id,payload).\n'
            '2) A final response (when DONE with the task):\n'
            '   For task.run_stage:\n'
            '     {"type":"final","message":{"msg_type":"agent.stage_result",'
            '"final_status":"succeeded","gate_result":"passed","event":null,'
            '"output_refs":[...],"detail":"..."}}\n'
            '     IMPORTANT: always set "event":null in your final task.run_stage message — any '
            'capability you ran (e.g. kaggle_eval) already emitted its own platform event, so '
            're-sending one would duplicate it. Only set a non-null event if YOU created a brand '
            'new event the platform has not seen.\n'
            '   For task.decide:\n'
            '     {"type":"final","message":{"msg_type":"agent.decision",'
            '"decision_type":"continue|exit_success|revisit","target_stage":<stage_code or null>,'
            '"reason_codes":[...],"evidence_refs":[...],"rationale":"..."}}\n\n'
            "Procedure: think, then EITHER emit one tool_call (we execute it and return the "
            "result, then you reply again) OR emit the final message. Never emit both. Only call "
            "run_capability when you actually want to execute a capability; otherwise return final."
        )
        conv = json.dumps(context, ensure_ascii=False, indent=2)
        return f"{sys_prompt}\n\n## Conversation so far\n{conv}\n\nRespond now with exactly one JSON object."

    @staticmethod
    def _fallback(message: dict[str, Any], raw: str) -> dict[str, Any]:
        mt = message.get("msg_type")
        snippet = (raw or "").strip()[:300]
        if mt == "task.decide":
            return {
                "msg_type": "agent.decision",
                "decision_type": "continue",
                "target_stage": None,
                "reason_codes": ["agent_no_parse"],
                "evidence_refs": [],
                "rationale": f"agent returned no structured decision: {snippet}",
            }
        return {
            "msg_type": "agent.stage_result",
            # A turn that produced no structured result MUST NOT masquerade as a
            # passed stage: nothing verifiable happened (C4 fix).
            "final_status": "failed",
            "gate_result": "failed",
            "event": None,
            "output_refs": [],
            "detail": f"agent returned no structured result: {snippet}",
        }


class CodexTransport(CLIAgentTransport):
    """Runs the Codex CLI as an interactive, tool-calling external agent.

    Wire it with a ``RemoteAgentHarness``::

        harness = RemoteAgentHarness(
            transport=CodexTransport(
                capability_runner=orchestrator.run_capability,
                tool_handler=harness._tool_handler,
            )
        )
    """

    def __init__(
        self,
        capability_runner: Callable[..., Any],
        tool_handler: ToolHandler | None = None,
        codex_cmd: str = "codex",
        model: str | None = None,
        max_tool_rounds: int = 8,
        timeout: int = 240,
        extra_args: list[str] | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        super().__init__(
            capability_runner=capability_runner,
            tool_handler=tool_handler,
            model=model,
            max_tool_rounds=max_tool_rounds,
            timeout=timeout,
            extra_args=extra_args,
            trace_sink=trace_sink,
        )
        self.codex_cmd = codex_cmd

    def _call_cli(self, prompt: str) -> str:
        cmd = [self.codex_cmd, "exec", "--skip-git-repo-check", "-"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout
        )
        return proc.stdout


class ClaudeCodeTransport(CLIAgentTransport):
    """Runs the Claude Code CLI as an interactive, tool-calling external agent.

    Mirrors :class:`CodexTransport` but targets the ``claude`` CLI (Claude Code). The
    CLI binary is taken from ``$CLAUDE_CMD`` (default ``claude``) and invoked in
    non-interactive ``-p`` (print) mode with the rendered prompt. Like the Codex
    transport it streams every tool call / final answer to the trace sink.

    Wire it with a ``RemoteAgentHarness``::

        harness = RemoteAgentHarness(
            transport=ClaudeCodeTransport(
                capability_runner=orchestrator.run_capability,
                tool_handler=harness._tool_handler,
            )
        )
    """

    def __init__(
        self,
        capability_runner: Callable[..., Any],
        tool_handler: ToolHandler | None = None,
        model: str | None = None,
        max_tool_rounds: int = 8,
        timeout: int = 600,
        extra_args: list[str] | None = None,
        trace_sink: TraceSink | None = None,
        claude_cmd: str | None = None,
    ) -> None:
        super().__init__(
            capability_runner=capability_runner,
            tool_handler=tool_handler,
            model=model,
            max_tool_rounds=max_tool_rounds,
            timeout=timeout,
            extra_args=extra_args,
            trace_sink=trace_sink,
        )
        self.claude_cmd = claude_cmd or os.environ.get("CLAUDE_CMD", "claude")

    def _call_cli(self, prompt: str) -> str:
        cmd = [self.claude_cmd, "-p", prompt]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout
        )
        return proc.stdout


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first balanced JSON object from arbitrary text."""

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
