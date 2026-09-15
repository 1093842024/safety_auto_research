#!/usr/bin/env python
"""JSON line-protocol bridge: platform AGENT_COMMAND <-> tclaude (Claude Code) CLI.

The platform's ``SubprocessTransport`` speaks a line-delimited JSON protocol
(see ``execution_plane/agent/PROTOCOL.md``)::

    platform -> agent : task.decide | task.run_stage | platform.tool_result
    agent    -> platform: agent.tool_call (zero or more)
    agent    -> platform: agent.decision | agent.stage_result  (terminal, ends turn)

A plain CLI agent (``tclaude`` / Claude Code) speaks prompt-in/text-out instead.
This thin bridge adapts between the two:

  1. reads ONE platform task message from stdin;
  2. renders it (plus the accumulated conversation) into a single prompt that
     states the protocol contract;
  3. calls the CLI once per round (``tclaude -p <prompt>``);
  4. if the reply is a ``tool_call``, writes ``agent.tool_call`` to stdout, reads
     back the ``platform.tool_result`` line, appends both to the conversation and
     loops;
  5. if the reply is a terminal ``agent.decision`` / ``agent.stage_result``,
     writes it to stdout and exits.

Environment variables:

* ``BRIDGE_CLI``      - CLI command template (default ``tclaude -p``).
* ``BRIDGE_TIMEOUT``  - per-CLI-call timeout in seconds (default 600).
* ``BRIDGE_MAX_ROUNDS`` - max tool-call rounds per task (default 12).
* ``BRIDGE_MODEL``    - explicit ``--model`` override (wins over the settings file).

Model selection also reads the platform system settings store
(``data/system_settings.json``, key ``agent_model`` — see
``control_plane/agent_settings.py`` and the 系统设置 page) on EVERY invocation, so a
model picked in the UI applies from the next agent turn without restarting anything.

Wire via::

    AGENT_COMMAND="<python> safety_auto_research/scripts/tclaude_line_agent.py"
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# 登录失效特征（CLI 输出 sign-in 指引而非任务结果）→ 快速失败，不重试。
_SIGNIN_RE = re.compile(
    r"Connecting to sign-in|Waiting for browser sign-in|Sign-in timed out", re.I
)


class _CliAuthError(RuntimeError):
    pass


CLI_TEMPLATE = os.environ.get("BRIDGE_CLI", "tclaude -p")
CLI_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "600"))
MAX_ROUNDS = int(os.environ.get("BRIDGE_MAX_ROUNDS", "12"))
DEBUG = os.environ.get("BRIDGE_DEBUG") == "1"

TERMINAL_TYPES = {"agent.decision", "agent.stage_result"}

# --------------------------------------------------------------------------- io


def _log(msg: str) -> None:
    if DEBUG:
        print(f"[bridge] {msg}", file=sys.stderr)


def _emit(msg: dict) -> None:
    """Write one protocol message to stdout (platform reads line-delimited JSON)."""
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_line() -> dict | None:
    line = sys.stdin.readline()
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


# ------------------------------------------------------------------- json util


def extract_json(text: str) -> dict | None:
    """Best-effort extraction of the first balanced JSON object from CLI output."""
    if not text:
        return None
    # 1. whole text is JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2. ```json fenced block
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    # 3. first balanced {...} scan (string-aware)
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start : i + 1])
                    start = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------- prompt

_CONTRACT = """你是一个自主研究 agent，通过 JSON 行协议与一个研究平台（safety_auto_research）交互。

## 交互契约（必须严格遵守）

你的每轮回复必须包含且仅包含一个 JSON 对象（不要输出任何其它文字、不要用代码块包裹）。

1) 若你还需要调用平台工具，回复：
{"type": "tool_call", "tool": "<工具名>", "args": {<参数对象>}}

平台会执行工具并回传结果，然后你会收到下一轮输入。

2) 若任务已可以终结，回复终结消息（二选一）：
- 决策类任务：{"msg_type": "agent.decision", "decision_type": "<continue|revisit|exit_success|exit_budget|exit_converged>", "target_stage": <阶段代码或 null>, "reason_codes": ["..."], "rationale": "<一句话理由>"}
- 执行类任务：{"msg_type": "agent.stage_result", "final_status": "<succeeded|failed>", "gate_result": "<passed|failed|unknown>", "output_refs": ["..."], "metrics": {<关键指标>}, "detail": "<执行摘要>", "event": null}

## 重要说明

* 可用工具以输入消息中的 available_tools / run_context.capabilities 为准。
* 执行类任务（task.run_stage）通常应调用工具 run_capability 来完成实际研究动作
  （例如运行真实评测能力），args 形如 {"run_id": "<run_id>", "capability_id": "<能力id>", "params": {...}}。
* 真实评测事件由平台在你调用 run_capability 时自动提交，终结消息中 event 置 null 即可。
* 不得自行编造评测指标：metrics 只能引用工具结果中返回的真实数字。
* 输出必须是单行合法 JSON（无换行、无 markdown 代码块标记、无解释文字）。

## 工具签名（参数名必须完全一致，否则调用会失败）

* run_capability(run_id, capability_id, params) — 调用基础设施层能力（真实评测/实验执行）。
* load_object(ref) — ref 必须是 "kind:id" 形式，如 "artifact:artifact-abc"、"run:run-123"。
  不要传 object_id / id 等其它参数名。
* record_metric(run_id, name, value, tags?)
* publish_artifact(payload, schema_version, metadata?)
* register_lesson(payload) / request_approval(run_id, payload) / emit_event(event)
* query_experiences(task_id, limit) / observe_hypothesis / backpropagate_insight /
  register_experience / compact_research_state — 参数以 spec.run_context 中的说明为准。
* layer_XX 开头且 detail 含 "stub" 的能力是占位实现，不会产生真实指标——
  优先使用任务指定的沙箱评测能力（见任务说明中的 sandbox_capability / research_cmd）。
"""


def _render(context: list[dict], tools: list[str], round_no: int, max_rounds: int) -> str:
    conv = "\n".join(
        f"--- {c['role']} ---\n{json.dumps(c['message'], ensure_ascii=False)}" for c in context
    )
    return (
        f"{_CONTRACT}\n\n## 可用工具\n{json.dumps(tools, ensure_ascii=False)}\n\n"
        f"## 对话历史（第 {round_no}/{max_rounds} 轮）\n{conv}\n\n"
        "现在请输出你的下一个 JSON 回复（仅一个 JSON 对象，单行）。"
    )


def _selected_model() -> str:
    """Model override for this invocation: env wins, then the settings store."""
    env = os.environ.get("BRIDGE_MODEL", "").strip()
    if env:
        return env
    data = _read_settings()
    val = (data or {}).get("agent_model")
    return str(val).strip() if val else ""


def _read_settings() -> dict | None:
    try:
        # The backend (and this bridge) live under the same package root.
        settings_path = os.environ.get(
            "SYSTEM_SETTINGS_STORE",
            os.path.join(
                # this file lives in <package_root>/scripts/ -> package root is 2 levels up
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "system_settings.json",
            ),
        )
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _cli_env() -> dict | None:
    """Env overrides for the active API key (系统设置启用的 tclaude 账号).

    tclaude 网关认证读取 ``TCLAUDE_AUTH_TOKEN``（兼容 ``ANTHROPIC_AUTH_TOKEN``）；
    不能注入 ``ANTHROPIC_API_KEY``（会被网关忽略并触发浏览器登录回退）。
    Empty key => the CLI's own login state applies (no override, ``None``).
    """
    data = _read_settings() or {}
    key = str(data.get("agent_api_key", "")).strip()
    if not key:
        return None
    env = dict(os.environ)
    env["TCLAUDE_AUTH_TOKEN"] = key
    env["ANTHROPIC_AUTH_TOKEN"] = key
    base_url = str(data.get("agent_api_base_url", "")).strip()
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return env


def _call_cli(prompt: str) -> str:
    cmd = CLI_TEMPLATE.split()
    model = _selected_model()
    if model:
        # Claude Code validates --model against the gateway catalog and applies it
        # for this session (instead of its Opus/Haiku default pair).
        cmd += ["--model", model]
    cmd.append(prompt)
    _log(f"calling CLI: {CLI_TEMPLATE} (prompt {len(prompt)} chars)")
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT, env=_cli_env()
    )
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if _SIGNIN_RE.search(blob):
        # 登录失效：立即失败（提示重新 tclaude login），避免 600s 白等与无效重试。
        raise _CliAuthError(
            "tclaude 登录态失效，请在宿主机执行 `tclaude login` 重新认证后重试"
        )
    if proc.returncode != 0:
        _log(f"CLI stderr: {proc.stderr[:500]}")
    return proc.stdout


# ------------------------------------------------------------------------ main


def _synthesize_stage_failure(detail: str) -> dict:
    return {
        "msg_type": "agent.stage_result",
        "final_status": "failed",
        "gate_result": "unknown",
        "output_refs": [],
        "metrics": {},
        "detail": detail,
        "event": None,
    }


def _normalize_tool_call(parsed: dict, seq: int) -> dict | None:
    tool = parsed.get("tool")
    if not tool:
        return None
    return {
        "msg_type": "agent.tool_call",
        "call_id": parsed.get("call_id") or f"bridge-{seq}",
        "tool": tool,
        "args": parsed.get("args") or parsed.get("arguments") or {},
    }


def handle(task: dict) -> None:
    run_id = task.get("run_id") or (task.get("run") or {}).get("run_id")
    tools = task.get("available_tools") or []
    spec = task.get("spec") or {}
    rc = spec.get("run_context") or {}
    capabilities = rc.get("capabilities") or []

    # Enrich tool-call guidance for run_stage: the agent must know which capability
    # ids it may invoke and that run_capability is the way to do real work.
    if task.get("msg_type") == "task.run_stage" and capabilities:
        guidance = (
            '{"type": "tool_call", "tool": "run_capability", '
            '"args": {"run_id": "<run_id>", "capability_id": "<能力id>", "params": {}}}'
        )
        # 任务预置的沙箱评测路径（launch 时写入 run_context.agent_config）：显式指给
        # agent，避免它误选 layer_XX 的 stub 占位能力导致空跑。
        ac = rc.get("agent_config") or {}
        pre = []
        if ac.get("sandbox_capability"):
            cap = ac["sandbox_capability"]
            rc_cmd = ac.get("research_cmd") or ""
            data_dir = ac.get("data_dir") or ""
            call = (
                '{"type": "tool_call", "tool": "run_capability", "args": {"run_id": "'
                + str(run_id)
                + '", "capability_id": "'
                + cap
                + '"'
            )
            if rc_cmd:
                call += ', "params": {"research_cmd": "' + rc_cmd + '"'
                if data_dir:
                    call += ', "data_dir": "' + data_dir + '"'
                call += "}}"
            else:
                call += ", \"params\": {}}"
            pre.append(
                f"* ★ 本任务预置了沙箱评测能力 **{cap}**——这是产出真实指标的正确路径，"
                f"请优先调用它（不要调用 layer_XX 的 stub 占位能力）。"
            )
            pre.append(f"* 推荐调用：{call}")
            if data_dir:
                pre.append(f"* 数据目录（容器内挂载为 /data 的宿主路径）：{data_dir}")
        _CONTRACT_NOTE = (
            f"\n* 本 run 可调用的能力 id 列表：{json.dumps(capabilities, ensure_ascii=False)}。\n"
            f"* 调用示例：{guidance}\n"
            + ("\n".join(pre) + "\n" if pre else "")
        )
    else:
        _CONTRACT_NOTE = (
            f"\n* run_id: {run_id}\n"
            "* 本消息是决策类任务：阅读 run 状态、刚完成的事件与路由建议(suggestion)，"
            "给出 agent.decision。通常接受建议即可（decision_type/target_stage/reason_codes 原样返回）。"
        )

    context = [{"role": "platform", "message": task}]
    for round_no in range(1, MAX_ROUNDS + 1):
        prompt = _render(context, tools, round_no, MAX_ROUNDS) + _CONTRACT_NOTE
        try:
            raw = _call_cli(prompt)
        except subprocess.TimeoutExpired:
            _emit(_synthesize_stage_failure(f"CLI 调用超时（>{CLI_TIMEOUT}s）"))
            return
        except _CliAuthError as exc:
            _emit(_synthesize_stage_failure(str(exc)))
            return
        _log(f"round {round_no} raw: {raw[:300]}")
        parsed = extract_json(raw)
        if parsed is None:
            # 登录失效不重试（重试同样会输出 sign-in 指引）。
            if _SIGNIN_RE.search(raw):
                _emit(_synthesize_stage_failure(
                    "tclaude 登录态失效，请在宿主机执行 `tclaude login` 重新认证后重试"
                ))
                return
            # One retry with an explicit format reminder before giving up.
            retry_prompt = (
                "你上一轮的输出不是合法的单行 JSON。请重新输出：仅一个 JSON 对象，"
                "若是终结请用 msg_type 为 agent.decision / agent.stage_result 的对象。"
                + prompt
            )
            try:
                raw = _call_cli(retry_prompt)
            except subprocess.TimeoutExpired:
                raw = ""
            parsed = extract_json(raw) or {}
        _log(f"round {round_no} parsed: {json.dumps(parsed, ensure_ascii=False)[:300]}")

        msg_type = parsed.get("msg_type")
        if msg_type in TERMINAL_TYPES:
            _emit(parsed)
            return

        tool_call = (
            parsed
            if msg_type == "agent.tool_call"
            else (_normalize_tool_call(parsed, round_no) if parsed.get("type") == "tool_call" else None)
        )
        if tool_call is not None:
            _emit(tool_call)
            result = _read_line()
            if result is None:
                _emit(_synthesize_stage_failure("平台在工具调用后关闭了输入"))
                return
            context.append({"role": "agent", "message": parsed})
            context.append({"role": "platform", "message": result})
            continue

        # Not terminal, not a tool call -> nudge once, then fall back to a safe
        # deterministic terminal so the platform never hangs.
        if round_no >= 2:
            if task.get("msg_type") == "task.run_stage":
                _emit(_synthesize_stage_failure(
                    f"agent 输出无法解析为协议消息：{raw[:200]}"
                ))
            else:
                sug = task.get("suggestion") or {}
                _emit({
                    "msg_type": "agent.decision",
                    "decision_type": sug.get("decision_type", "continue"),
                    "target_stage": sug.get("target_stage"),
                    "reason_codes": sug.get("reason_codes", []),
                    "rationale": f"bridge fallback (unparseable agent output): {raw[:120]}",
                })
            return
        context.append({"role": "agent", "message": {"note": "unparseable output", "raw": raw[:400]}})

    # exhausted rounds
    if task.get("msg_type") == "task.run_stage":
        _emit(_synthesize_stage_failure(f"达到最大工具轮数（{MAX_ROUNDS}）仍未终结"))
    else:
        sug = task.get("suggestion") or {}
        _emit({
            "msg_type": "agent.decision",
            "decision_type": sug.get("decision_type", "continue"),
            "target_stage": sug.get("target_stage"),
            "reason_codes": ["bridge_max_rounds"],
            "rationale": "达到最大轮数，回退为接受路由建议",
        })


def main() -> None:
    task = _read_line()
    if task is None:
        return
    try:
        handle(task)
    except Exception as exc:  # never leave the platform hanging
        _log(f"fatal: {exc}")
        if task.get("msg_type") == "task.run_stage":
            _emit(_synthesize_stage_failure(f"bridge fatal: {exc}"))
        else:
            sug = task.get("suggestion") or {}
            _emit({
                "msg_type": "agent.decision",
                "decision_type": sug.get("decision_type", "continue"),
                "target_stage": sug.get("target_stage"),
                "reason_codes": ["bridge_fatal"],
                "rationale": f"bridge fatal: {exc}",
            })


if __name__ == "__main__":
    main()
