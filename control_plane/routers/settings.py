"""System settings endpoints: agent CLI model query / selection (系统设置页).

* ``GET  /settings/agent``         — effective agent-CLI settings (+ CLI identity)
* ``PUT  /settings/agent``         — persist a model selection
* ``GET  /settings/agent/models``  — live model list auto-fetched from the CLI itself
  (Claude Code validates ``--model`` and prints its accepted catalog on error, so a
  probe with a sentinel model yields the exact list the logged-in gateway allows).
* ``GET  /settings/environment``   — local-machine environment detection: Docker,
  GPU (nvidia-smi) and agent-CLI availability, plus basic host facts. Read-only,
  short-timeout probes; never mutates anything.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel

from .. import agent_settings

# Known agent-CLI binaries probed for availability (configured CLI first).
_AGENT_CLI_CANDIDATES = ("tclaude", "claude", "codex", "gemini", "qwen", "aider")

_PROBE_TIMEOUT = 8  # seconds per external probe (docker info / nvidia-smi / --version)


def _probe(cmd: list[str], timeout: float = _PROBE_TIMEOUT) -> tuple[int, str, str]:
    """Run a short-timeout read-only probe. Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "binary not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 126, "", f"{type(exc).__name__}: {exc}"


def _detect_docker() -> dict:
    """Docker CLI presence + daemon liveness (docker info)."""
    path = shutil.which("docker")
    if not path:
        return {"available": False, "daemon_running": False,
                "reason": "未安装 docker CLI（PATH 中未找到）"}
    rc, out, err = _probe([path, "--version"])
    if rc != 0:
        return {"available": False, "daemon_running": False, "path": path,
                "reason": err or f"docker --version 退出码 {rc}"}
    rc2, out2, err2 = _probe([path, "info", "--format", "{{.ServerVersion}}"])
    daemon = rc2 == 0
    return {
        "available": True,
        "daemon_running": daemon,
        "path": path,
        "version": out,
        "server_version": out2 if daemon else "",
        "reason": "" if daemon else (err2 or "docker daemon 未运行（docker info 失败）"),
    }


def _detect_gpu() -> dict:
    """GPU environment via nvidia-smi (name / driver / CUDA / per-device state)."""
    path = shutil.which("nvidia-smi")
    if not path:
        return {"available": False, "reason": "未找到 nvidia-smi（无 NVIDIA GPU 或未装驱动）"}
    rc, out, err = _probe([path, "--query-gpu=name,driver_version,memory.total,memory.used,"
                                    "utilization.gpu,temperature.gpu",
                           "--format=csv,noheader,nounits"])
    if rc != 0:
        return {"available": False, "path": path,
                "reason": err or f"nvidia-smi 退出码 {rc}"}
    devices = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 6:
            continue
        name, driver, mem_total, mem_used, util, temp = parts

        def _num(v: str) -> int | None:
            try:
                return int(float(v))
            except ValueError:
                return None

        devices.append({
            "name": name,
            "driver_version": driver,
            "memory_total_mb": _num(mem_total),
            "memory_used_mb": _num(mem_used),
            "utilization_pct": _num(util),
            "temperature_c": _num(temp),
        })
    _, cuda_out, _ = _probe([path], timeout=4)
    cuda_version = ""
    for token in cuda_out.replace(",", " ").split():
        if token.lower().startswith("cuda"):
            cuda_version = token.split(":", 1)[-1].strip() or token
            break
    return {"available": bool(devices), "path": path, "cuda_version": cuda_version,
            "devices": devices, "reason": "" if devices else "nvidia-smi 无输出设备"}


def _detect_agent_cli() -> dict:
    """Configured agent CLI + well-known CLI binaries on PATH."""
    configured, label = agent_settings._cli_base()
    found: list[dict] = []
    seen: set[str] = set()
    for name in [configured, *_AGENT_CLI_CANDIDATES]:
        base = name.split()[0]  # configured value may be "tclaude --flags ..."
        if base in seen:
            continue
        seen.add(base)
        path = shutil.which(base)
        if not path:
            found.append({"name": base, "available": False, "path": "",
                          "version": "", "configured": base == configured})
            continue
        rc, out, err = _probe([path, "--version"], timeout=6)
        found.append({
            "name": base,
            "available": True,
            "path": path,
            "version": (out or err).splitlines()[0] if (out or err) else "",
            "version_probe_ok": rc == 0,
            "configured": base == configured,
        })
    agent_command = os.environ.get("AGENT_COMMAND", "").strip()
    return {
        "configured_cli": configured,
        "configured_label": label,
        "configured_available": shutil.which(configured.split()[0]) is not None,
        "agent_command_set": bool(agent_command),
        "clis": found,
    }


def _detect_host() -> dict:
    """Basic host facts for context (read-only)."""
    mem_total_gb = None
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except OSError:
        pass
    return {
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "mem_total_gb": mem_total_gb,
    }


def detect_environment() -> dict:
    """Full environment snapshot used by GET /settings/environment."""
    return {
        "docker": _detect_docker(),
        "gpu": _detect_gpu(),
        "agent_cli": _detect_agent_cli(),
        "host": _detect_host(),
    }


class AgentSettingsUpdate(BaseModel):
    """Body for PUT /settings/agent (module-level so FastAPI can build its schema)."""

    model: str | None = None
    api_key: str | None = None
    api_base_url: str | None = None


class ApiKeyUpsert(BaseModel):
    """Body for POST /settings/agent/apikeys (save an account)."""

    label: str
    api_key: str
    base_url: str | None = None


class ApiKeyLabelRequest(BaseModel):
    """Body for activate/delete endpoints, identifying a saved account by label."""

    label: str


class ApiKeyValidateRequest(BaseModel):
    """Body for POST /settings/agent/apikeys/validate.

    ``label``  -> validate a saved account; ``api_key`` -> validate a raw key
    (not necessarily saved); both empty -> validate the CLI's own login state.
    """

    label: str | None = None
    api_key: str | None = None
    base_url: str | None = None


def build_settings_router(deps) -> APIRouter:
    router = APIRouter()

    @router.get("/settings/agent", summary="Effective agent-CLI settings (model + API key accounts)")
    def get_agent_settings() -> dict:
        cli, label = agent_settings._cli_base()
        s = agent_settings.get_settings()
        return {
            "cli": cli,
            "cli_label": label,
            "model": s["agent_model"],
            "model_explicit": bool(s["agent_model"]),
            # API key state — raw keys never leave the backend, masked only.
            "api_key_set": bool(s.get("agent_api_key")),
            "api_key_masked": agent_settings._mask(s.get("agent_api_key", "")),
            "api_base_url": s.get("agent_api_base_url", ""),
            "active_account": agent_settings.active_account(),
            "saved_keys": agent_settings.list_saved_keys(),
            "note": (
                "model 为空时使用 CLI 默认（Claude-Opus-5 主模型 + Claude-Haiku-4.5 兜底）；"
                "设置后 agent 每次调用都会带上 --model。API key 为空时使用 CLI 自身登录态。"
            ),
        }

    @router.put("/settings/agent", summary="Persist the agent-CLI model / active API key / base URL")
    def put_agent_settings(body: AgentSettingsUpdate) -> dict:
        values: dict[str, str] = {}
        if body.model is not None:
            values["agent_model"] = body.model.strip()
        if body.api_key is not None:
            values["agent_api_key"] = body.api_key.strip()
        if body.api_base_url is not None:
            values["agent_api_base_url"] = body.api_base_url.strip()
        s = agent_settings.update_settings(values)
        return {"ok": True, "model": s["agent_model"]}

    @router.get(
        "/settings/agent/models",
        summary="Available models for the agent CLI (auto-fetched from the CLI itself)",
    )
    def get_agent_models() -> dict:
        return agent_settings.fetch_models()

    @router.get(
        "/settings/agent/cli-login",
        summary="Detect the CLI's own login state (tclaude login 同步检测；refresh=true 强制重测)",
    )
    def get_cli_login(refresh: bool = False) -> dict:
        if refresh:
            agent_settings._login_cache.clear()
        return agent_settings.cli_login_state()

    @router.get(
        "/settings/environment",
        summary="Local machine environment: Docker / GPU / agent CLI detection",
    )
    def get_environment() -> dict:
        return detect_environment()

    # ----- API key (tclaude 账号) management -----

    @router.post(
        "/settings/agent/apikeys",
        summary="Save (or update) an agent-CLI API key account by label",
    )
    def post_api_key(body: ApiKeyUpsert) -> dict:
        try:
            saved = agent_settings.save_api_key(
                body.label, body.api_key, (body.base_url or "").strip()
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
        return {"ok": True, "saved_keys": saved}

    @router.post(
        "/settings/agent/apikeys/delete",
        summary="Delete a saved API key account by label",
    )
    def delete_api_key(body: ApiKeyLabelRequest) -> dict:
        return {"ok": True, "saved_keys": agent_settings.delete_api_key(body.label)}

    @router.post(
        "/settings/agent/apikeys/activate",
        summary="Activate a saved account (empty label => fall back to CLI login)",
    )
    def activate_api_key(body: ApiKeyLabelRequest) -> dict:
        try:
            agent_settings.activate_api_key(body.label)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
        return {"ok": True, "saved_keys": agent_settings.list_saved_keys()}

    @router.post(
        "/settings/agent/apikeys/validate",
        summary="Probe whether an API key works: run a minimal real turn with the key injected",
    )
    def validate_api_key(body: ApiKeyValidateRequest) -> dict:
        label = (body.label or "").strip()
        key = (body.api_key or "").strip()
        if label:
            saved = {k["label"]: k for k in agent_settings.list_saved_keys()}
            if label not in saved:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, detail=f"未找到账号 {label!r}"
                )
        # Raw keys are resolved inside agent_settings.validate_api_key via the
        # store when label is used — look it up here so raw keys stay in-process.
        if label and not key:
            for item in agent_settings._read_keys():
                if item["label"] == label:
                    key = item["key"]
                    break
        return agent_settings.validate_api_key(key, (body.base_url or "").strip())

    return router
