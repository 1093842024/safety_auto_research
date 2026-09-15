"""System settings for the agent CLI (tclaude / Claude Code).

Single source of truth for *which underlying model the agent CLI uses*:

* persisted to ``data/system_settings.json`` (env ``SYSTEM_SETTINGS_STORE``);
* read by
  - the tclaude line-protocol bridge (``scripts/tclaude_line_agent.py``) on every
    CLI invocation, so a model change takes effect on the very next agent turn;
  - :class:`~safety_auto_research.execution_plane.agent.transport.ClaudeCodeTransport`
    consumers (MEA role agents) when their role config says ``model: auto``;
* the available-model list is **auto-fetched from the CLI itself**: invoking the
  CLI with a deliberately invalid ``--model`` makes it print its accepted model
  list on the error line ("Available models: ..."), which we parse. This reflects
  exactly what the logged-in gateway allows — no hard-coded catalog.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCK = threading.Lock()

# Fallback list when the CLI probe fails (matches the logged-in tclaude gateway
# at authoring time; the live probe is authoritative whenever it succeeds).
_FALLBACK_MODELS: list[str] = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4.5",
]

DEFAULTS: dict[str, str] = {
    # Empty string => do not pass --model; the CLI's own default applies
    # (Opus main + Haiku fallback for Claude Code).
    "agent_model": "",
    # Active agent-CLI API key (tclaude 账号). Empty => use the CLI's own login.
    "agent_api_key": "",
    # Optional gateway base URL paired with the API key (e.g. self-hosted gateway).
    "agent_api_base_url": "",
    # Saved accounts as a JSON array of {"label": str, "key": str} — 多账号管理。
    "agent_api_keys": "[]",
}


def apply_env_defaults() -> None:
    """Ensure agent/sandbox env vars exist no matter HOW the backend was started.

    The backend gets restarted by several operators (scripts, IDE watchers, manual
    runs); when launched without ``AGENT_COMMAND`` / ``AGENT_SANDBOX`` the agent
    loop degrades to "not configured" and Docker isolation silently turns off.
    This fills the blanks with sane defaults derived from the checkout itself
    (never overrides values the operator explicitly set).
    """
    import sys

    bridge = os.path.join(_PKG_ROOT, "scripts", "tclaude_line_agent.py")
    defaults = {
        "AGENT_COMMAND": f"{sys.executable} {bridge}",
        "CLAUDE_CMD": "tclaude",
        "AGENT_SANDBOX": "1",
        "AGENT_SANDBOX_REQUIRE_HARD": "1",
    }
    for k, v in defaults.items():
        if not os.environ.get(k):
            os.environ[k] = v


def _store_path() -> str:
    override = os.environ.get("SYSTEM_SETTINGS_STORE")
    if override:
        return override
    return os.path.join(_PKG_ROOT, "data", "system_settings.json")


def _read_raw() -> dict[str, str]:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_settings() -> dict[str, str]:
    """Effective settings = defaults overlaid with the persisted store."""
    with _LOCK:
        raw = _read_raw()
    out = dict(DEFAULTS)
    out.update({k: str(v) for k, v in raw.items() if k in DEFAULTS})
    return out


def update_settings(values: dict[str, str]) -> dict[str, str]:
    """Persist the given keys (only known keys are accepted)."""
    allowed = {k: str(v) for k, v in values.items() if k in DEFAULTS}
    with _LOCK:
        raw = _read_raw()
        raw.update(allowed)
        tmp = _store_path() + ".tmp"
        os.makedirs(os.path.dirname(_store_path()), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _store_path())
    return get_settings()


# --------------------------------------------------------------------------- #
# Live model list probe: Claude Code validates ``--model`` against the gateway's
# catalog and prints the accepted list when the value is unknown. Probing with a
# sentinel value turns the CLI into its own models-list API.
# --------------------------------------------------------------------------- #
_avail_cache: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL = 600.0
_MODEL_RE = re.compile(r"Available models:\s*(.+)")


def _cli_base() -> tuple[str, str]:
    """(binary, source-label) for the configured agent CLI."""
    cmd = os.environ.get("CLAUDE_CMD", "tclaude")
    return cmd, "tclaude" if "tclaude" in cmd else cmd


def fetch_models(timeout: int = 45) -> dict:
    """Best-effort live model list + how it was obtained."""
    key = "models"
    now = time.monotonic()
    cached = _avail_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return {"models": cached[1], "source": "cache", "cli": _cli_base()[0]}

    cli, label = _cli_base()
    cmd = [cli, "-p", "x", "--model", "__probe_unavailable_model__"]
    models: list[str] = []
    source = "fallback"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = _MODEL_RE.search(blob)
        if m:
            models = [x.strip() for x in m.group(1).split(",") if x.strip()]
            if models:
                source = "cli-probe"
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not models:
        models = list(_FALLBACK_MODELS)

    _avail_cache[key] = (now, models)
    return {"models": models, "source": source, "cli": cli, "cli_label": label}


# --------------------------------------------------------------------------- #
# API key (tclaude 账号) management: 多账号保存 / 启用 / 环境注入 / 有效性验证.
#
# tclaude 的网关认证由环境变量 ``TCLAUDE_AUTH_TOKEN``（兼容 ``ANTHROPIC_AUTH_TOKEN``）
# 覆盖；保存的 key 会在所有 CLI 调用点（tclaude_line_agent.py 桥、
# ClaudeCodeTransport / CodexTransport）注入子进程环境，使选中的账号即刻生效。
# --------------------------------------------------------------------------- #


def _mask(key: str) -> str:
    """Mask an API key for display: keep head/tail only."""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _read_keys(raw: dict[str, str] | None = None) -> list[dict[str, str]]:
    if raw is None:
        raw = _read_raw()
    try:
        arr = json.loads(raw.get("agent_api_keys", "[]"))
    except json.JSONDecodeError:
        return []
    out = []
    for item in arr if isinstance(arr, list) else []:
        if isinstance(item, dict) and item.get("label") and item.get("key"):
            out.append({
                "label": str(item["label"]),
                "key": str(item["key"]),
                "base_url": str(item.get("base_url", "") or ""),
            })
    return out


def list_saved_keys() -> list[dict[str, str]]:
    """Saved accounts with masked keys only (raw keys never leave the backend).

    ``active`` is derived by comparing keys with the currently enabled one, so
    enabling an account automatically deactivates the previous one (单选语义).
    """
    with _LOCK:
        raw = _read_raw()
    active = str(raw.get("agent_api_key", ""))
    out = []
    for item in _read_keys(raw):
        out.append({
            "label": item["label"],
            "masked": _mask(item["key"]),
            "base_url": item["base_url"],
            "active": item["key"] == active,
        })
    return out


def active_account() -> dict[str, str]:
    """The currently enabled account: label / masked key / base_url (or CLI 登录态)."""
    with _LOCK:
        raw = _read_raw()
    active = str(raw.get("agent_api_key", ""))
    for item in _read_keys(raw):
        if item["key"] == active:
            return {
                "label": item["label"],
                "masked": _mask(item["key"]),
                "base_url": str(raw.get("agent_api_base_url", "") or "")
                or item["base_url"],
            }
    return {"label": "", "masked": "", "base_url": str(raw.get("agent_api_base_url", "") or "")}


def save_api_key(label: str, key: str, base_url: str = "") -> list[dict[str, str]]:
    """Upsert an account by label (key + optional paired gateway base_url)."""
    label, key = label.strip(), key.strip()
    if not label or not key:
        raise ValueError("label 与 api_key 均不能为空")
    with _LOCK:
        raw = _read_raw()
        keys = _read_keys(raw)
        entry = {"label": label, "key": key, "base_url": base_url.strip()}
        for i, item in enumerate(keys):
            if item["label"] == label:
                keys[i] = entry
                break
        else:
            keys.append(entry)
        raw["agent_api_keys"] = json.dumps(keys, ensure_ascii=False)
        # If this account is currently active, keep its key/base_url in sync.
        if raw.get("agent_api_key") == key:
            raw["agent_api_base_url"] = base_url.strip()
        _write_raw(raw)
    return list_saved_keys()


def delete_api_key(label: str) -> list[dict[str, str]]:
    with _LOCK:
        raw = _read_raw()
        removed_key = next(
            (k["key"] for k in _read_keys(raw) if k["label"] == label.strip()), ""
        )
        keys = [k for k in _read_keys(raw) if k["label"] != label.strip()]
        raw["agent_api_keys"] = json.dumps(keys, ensure_ascii=False)
        # Deactivate if the removed account was the active one.
        if raw.get("agent_api_key") and not any(
            k["key"] == raw.get("agent_api_key") for k in keys
        ):
            raw["agent_api_key"] = ""
            raw["agent_api_base_url"] = ""
        elif removed_key and raw.get("agent_api_base_url"):
            pass  # base_url of a still-active other account is managed on activate
        _write_raw(raw)
    return list_saved_keys()


def activate_api_key(label: str) -> dict[str, str]:
    """Set the given saved account as the active key (empty label => CLI 登录态).

    Enabling an account automatically deactivates the previous one (keys are
    single-active) and applies the account's own gateway base_url.
    """
    label = label.strip()
    with _LOCK:
        raw = _read_raw()
        if not label:
            raw["agent_api_key"] = ""
            raw["agent_api_base_url"] = ""
        else:
            match = next(
                (k for k in _read_keys(raw) if k["label"] == label), None
            )
            if match is None:
                raise ValueError(f"未找到账号 {label!r}")
            raw["agent_api_key"] = match["key"]
            raw["agent_api_base_url"] = match.get("base_url", "")
        _write_raw(raw)
    return get_settings()


def _write_raw(raw: dict[str, str]) -> None:
    tmp = _store_path() + ".tmp"
    os.makedirs(os.path.dirname(_store_path()), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _store_path())


def cli_env_overrides(api_key: str = "", base_url: str = "") -> dict[str, str]:
    """Env vars to inject when invoking the agent CLI ("" => use its own login).

    tclaude 的网关认证读取 ``TCLAUDE_AUTH_TOKEN``（并兼容 ``ANTHROPIC_AUTH_TOKEN``）；
    注意 **不能** 注入 ``ANTHROPIC_API_KEY``——实测它会被网关忽略并让 CLI 回退到
    浏览器登录流程。``ANTHROPIC_BASE_URL`` 可选，指向配套网关。
    """
    overrides: dict[str, str] = {}
    key = (api_key or get_settings().get("agent_api_key", "")).strip()
    if key:
        overrides["TCLAUDE_AUTH_TOKEN"] = key
        overrides["ANTHROPIC_AUTH_TOKEN"] = key
    url = (base_url or get_settings().get("agent_api_base_url", "")).strip()
    if url:
        overrides["ANTHROPIC_BASE_URL"] = url
    return overrides


def _gateway_base_url(base_url: str = "") -> str:
    """Base URL for the tclaude gateway daemon (explicit override > daemon.json)."""
    url = (base_url or "").strip()
    if url:
        return url.rstrip("/")
    try:
        daemon_json = os.path.join(
            os.path.expanduser("~"), ".tclaude", "daemon.json"
        )
        with open(daemon_json, encoding="utf-8") as f:
            url = json.load(f).get("url", "")
        return str(url).rstrip("/")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def validate_api_key(api_key: str = "", base_url: str = "", timeout: int = 30) -> dict:
    """Probe whether an API key actually works against the tclaude gateway.

    Sends a minimal ``POST /v1/messages`` (haiku, max_tokens=8) with the key as
    ``x-api-key`` to the gateway daemon (discovered from ``~/.tclaude/daemon.json``,
    or an explicit ``base_url``). This is a fast, real authentication check —
    an invalid key is rejected with HTTP 401 before any model call.

    Empty ``api_key`` => validate the CLI's own login state (no key header).
    Verdict: ``True`` valid / ``False`` invalid / ``None`` inconclusive.
    """
    import urllib.error
    import urllib.request

    url = _gateway_base_url(base_url)
    if not url:
        return {
            "valid": None,
            "detail": "未发现 tclaude 网关守护进程（~/.tclaude/daemon.json），也未提供 Base URL",
            "latency_ms": None,
        }
    key = (api_key or get_settings().get("agent_api_key", "")).strip()
    if not key and not base_url:
        # No key => validate the CLI's own login state via a real CLI turn
        # (the gateway daemon requires a key header, so HTTP probing cannot
        # express "the session the CLI itself uses").
        return _validate_cli_login(timeout)
    payload = json.dumps({
        # 探测请求也使用系统设置中的生效模型（而非硬编码 haiku），与真实
        # agent 调用的模型选择保持一致；设置缺失时回退 haiku。
        "model": get_settings().get("agent_model") or "claude-haiku-4-5",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "X-Claude-Code-Session-Id": "platform-key-probe",
        "x-api-key": key,
        "Authorization": f"Bearer {key}",
    }
    req = urllib.request.Request(f"{url}/v1/messages", data=payload, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            body = (resp.read() or b"").decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        body = (exc.read() or b"").decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"valid": None, "detail": f"网关不可达: {exc}", "latency_ms": None}
    latency_ms = int((time.monotonic() - started) * 1000)

    if status_code == 200:
        ok = _response_indicates_auth(body)
        return {
            "valid": bool(ok),
            "detail": "认证通过，网关返回了真实模型响应" if ok else f"HTTP 200 但响应异常: {body[:160]}",
            "latency_ms": latency_ms,
        }
    if status_code in (401, 403):
        reason = body.strip()[:160] or "invalid key"
        return {
            "valid": False,
            "detail": f"认证失败（HTTP {status_code}）: {reason}",
            "latency_ms": latency_ms,
        }
    return {
        "valid": None,
        "detail": f"HTTP {status_code}: {body.strip()[:160]}",
        "latency_ms": latency_ms,
    }


_LOGIN_MARKERS = ("connecting to sign-in", "waiting for browser sign-in", "/login")

# CLI 登录态探测缓存（一次真实 CLI 调用约 5-10s，短 TTL 避免页面刷新反复探测）。
_login_cache: dict[str, tuple[float, dict]] = {}
_LOGIN_CACHE_TTL = 60.0


def cli_login_state(timeout: int = 45) -> dict:
    """Detect the CLI's own login state (``tclaude login`` 的结果).

    Runs one minimal real turn **without** any key env override: the gateway
    requires the CLI's session, so a real response proves the login works;
    sign-in-flow output proves it does not. Result is cached for 60s —
    pass ``force=True`` via :func:`cli_login_state_refresh` semantics by
    clearing the cache externally if needed.

    NOTE: tclaude does not persist any account identity locally (only an opaque
    userID hash), so the *which account* question cannot be answered from this
    machine — only whether the login session works.
    """
    now = time.monotonic()
    cached = _login_cache.get("state")
    if cached and now - cached[0] < _LOGIN_CACHE_TTL:
        return {**cached[1], "cached": True}

    cli, _label = _cli_base()
    cmd = [cli, "-p", "reply with exactly: pong"]
    # 与真实 agent 调用一致：带上系统设置中的生效模型（如有）。
    model = get_settings().get("agent_model", "").strip()
    if model:
        cmd += ["--model", model]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result = {
            "logged_in": None,
            "detail": f"CLI 探测超时（>{timeout}s）——登录态可能不可用",
            "latency_ms": None,
        }
        _login_cache["state"] = (now, result)
        return result
    except OSError as exc:
        result = {"logged_in": False, "detail": f"CLI 启动失败: {exc}", "latency_ms": None}
        _login_cache["state"] = (now, result)
        return result
    latency_ms = int((time.monotonic() - started) * 1000)
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    if any(m in blob for m in _LOGIN_MARKERS):
        result = {
            "logged_in": False,
            "detail": "CLI 未登录或登录态不可用（进入浏览器登录流程）",
            "latency_ms": latency_ms,
        }
    elif proc.returncode == 0 and (proc.stdout or "").strip():
        result = {
            "logged_in": True,
            "detail": "CLI 登录态可用，实测返回了真实模型响应",
            "latency_ms": latency_ms,
        }
    else:
        result = {
            "logged_in": None,
            "detail": (proc.stderr or proc.stdout or "无输出").strip()[:160],
            "latency_ms": latency_ms,
        }
    _login_cache["state"] = (time.monotonic(), result)
    return result


def _response_indicates_auth(body: str) -> bool:
    """True when a 200 body proves a real authenticated model response.

    The gateway may answer either plain JSON (``{"type": "message", ...}``) or
    an SSE stream (``event: message_start\\ndata: {...}``) — both prove auth OK.
    """
    body = (body or "").strip()
    if not body:
        return False
    if body.startswith("event:") or "event: message_start" in body:
        return "message_start" in body
    try:
        return json.loads(body).get("type") in ("message", "message_start")
    except json.JSONDecodeError:
        return False


def _validate_cli_login(timeout: int = 90) -> dict:
    """Validate the CLI's own login state by running one minimal real turn."""
    cli, _label = _cli_base()
    cmd = [cli, "-p", "reply with exactly: pong"]
    # 与真实 agent 调用一致：带上系统设置中的生效模型（如有）。
    model = get_settings().get("agent_model", "").strip()
    if model:
        cmd += ["--model", model]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "valid": None,
            "detail": f"CLI 探测超时（>{timeout}s）——登录态可能不可用",
            "latency_ms": None,
        }
    except OSError as exc:
        return {"valid": False, "detail": f"CLI 启动失败: {exc}", "latency_ms": None}
    latency_ms = int((time.monotonic() - started) * 1000)
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    if any(m in blob for m in _LOGIN_MARKERS):
        return {
            "valid": False,
            "detail": "CLI 未登录或登录态不可用（进入浏览器登录流程）",
            "latency_ms": latency_ms,
        }
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return {
            "valid": True,
            "detail": "CLI 登录态有效，返回了真实模型响应",
            "latency_ms": latency_ms,
        }
    return {
        "valid": None,
        "detail": (proc.stderr or proc.stdout or "无输出").strip()[:160],
        "latency_ms": latency_ms,
    }
