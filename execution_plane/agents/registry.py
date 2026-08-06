"""RoleAgentRegistry — routes each research step to its configured best agent.

Loads ``config/role_agents.yaml`` (or a plain dict) describing, per role and per
subtask type, which backend/model/budget to use. The per-role AgentAdapter is built
lazily and cached. A hard separation check guarantees the Auditor's backend/model is
independent of the Executor's (paper's anti-self-confirmation constraint).
"""

from __future__ import annotations

import json
import os
from typing import Any

from .adapter import ClaudeCodeAdapter
from .adapter import CodexAdapter
from .adapter import DeterministicAdapter
from .adapter import IndependentAuditorAdapter
from .adapter import LLMApiAdapter
from .adapter import ManagerAdapter
from .adapter import MetaDeciderAdapter
from .adapter import RoleAgentAdapter
from .adapter import RoleBudget
from .adapter import WorkBuddyAdapter


class RoleAgentRegistry:
    """Resolves the optimal agent for each role / subtask type."""

    def __init__(self, spec: dict[str, Any]) -> None:
        # ``spec`` is the ``roles`` mapping (role -> {backend, model, budget, ...}).
        self._spec = spec or {}
        self._runner: Any = None
        self._tool_handler: Any = None
        self._cache: dict[str, RoleAgentAdapter] = {}

    # ----------------------------------------------------------------- load
    @classmethod
    def load(cls, path_or_dict: Any) -> "RoleAgentRegistry":
        if isinstance(path_or_dict, dict):
            return cls(path_or_dict.get("roles", path_or_dict))
        path = str(path_or_dict)
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover - env dependent
                raise RuntimeError(
                    "PyYAML is required to load a .yaml role spec; "
                    "install pyyaml or pass a dict."
                ) from exc
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        roles = data.get("roles", data) if isinstance(data, dict) else {}
        return cls(roles)

    # --------------------------------------------------------------- wiring
    def bind(self, capability_runner: Any, tool_handler: Any = None) -> None:
        """Inject the orchestrator's run_capability + tool handler into adapters."""

        self._runner = capability_runner
        self._tool_handler = tool_handler

    # --------------------------------------------------------------- resolve
    def _role_cfg(self, role: str, subtask_type: str | None) -> dict[str, Any]:
        if role == "executor":
            # Accept ``executor_overrides`` either at the top level OR nested under
            # ``executor_default`` (both layouts are valid in role_agents.yaml).
            overrides = self._spec.get("executor_overrides") or (
                self._spec.get("executor_default") or {}
            ).get("executor_overrides") or {}
            override = overrides.get(subtask_type or "")
            if override:
                return override
            return self._spec.get("executor_default", {}) or {}
        return self._spec.get(role, {}) or {}

    def _make(
        self, backend: str, cfg: dict[str, Any], role: str, subtask_type: str | None
    ) -> RoleAgentAdapter:
        model = cfg.get("model")
        # The Manager (R0) decomposes the objective; the meta-decider (R7) routes. Both
        # are strong-reasoning roles that wrap the configured brain so the loop can call
        # decompose()/decide() regardless of which backend the brain uses.
        if role == "manager":
            return ManagerAdapter(brain=self._build_brain(backend, cfg), runner=self._runner)
        if role == "meta_decider":
            return MetaDeciderAdapter(brain=self._build_brain(backend, cfg), runner=self._runner)
        if backend == "deterministic":
            return DeterministicAdapter(capability_runner=self._runner)
        if backend == "codex":
            return CodexAdapter(
                capability_runner=self._runner, tool_handler=self._tool_handler, model=model
            )
        if backend == "claude_code":
            return ClaudeCodeAdapter(
                capability_runner=self._runner, tool_handler=self._tool_handler, model=model
            )
        if backend == "workbuddy":
            return WorkBuddyAdapter(
                capability_runner=self._runner,
                tool_handler=self._tool_handler,
                endpoint=cfg.get("endpoint"),
                agent_command=cfg.get("agent_command"),
                model=model,
            )
        if backend == "llm_api":
            llm = LLMApiAdapter(model=model, system_prompt=cfg.get("system_prompt"))
            if role == "auditor":
                return IndependentAuditorAdapter(llm=llm, capability_runner=self._runner)
            return llm
        raise ValueError(f"unknown backend {backend!r} for role {role}")

    def _build_brain(
        self, backend: str, cfg: dict[str, Any]
    ) -> RoleAgentAdapter | None:
        """Construct the Manager's reasoning "brain" (the agent powering ``decompose``).

        Only ``llm_api`` currently exposes a ``chat`` method the Manager uses to emit a
        JSON plan; any other backend yields ``None`` and the Manager fails soft to the
        deterministic :func:`default_plan`, so decomposition always succeeds offline.
        """

        if backend == "llm_api":
            return LLMApiAdapter(model=cfg.get("model"), system_prompt=cfg.get("system_prompt"))
        return None

    def resolve(self, role: str, subtask_type: str | None = None) -> RoleAgentAdapter:
        cache_key = f"{role}:{subtask_type}"
        if cache_key not in self._cache:
            cfg = self._role_cfg(role, subtask_type)
            backend = cfg.get("backend", "deterministic")
            self._cache[cache_key] = self._make(backend, cfg, role, subtask_type)
        return self._cache[cache_key]

    # --------------------------------------------------------------- budget
    def budget_for(self, role: str, subtask_type: str | None = None) -> RoleBudget:
        cfg = self._role_cfg(role, subtask_type)
        # Support both the flat ``budget_seconds`` key and a nested ``budget`` dict.
        seconds = cfg.get("budget_seconds")
        b = cfg.get("budget", {}) or {}
        if seconds is None and not b and role == "executor":
            b = self._spec.get("executor_default", {}).get("budget", {}) or {}
            seconds = self._spec.get("executor_default", {}).get("budget_seconds")
        if seconds is None:
            seconds = b.get("max_seconds")
        default_seconds = 1800 if role == "executor" else 300
        max_seconds = int(seconds) if seconds is not None else default_seconds
        return RoleBudget(
            max_seconds=max_seconds,
            max_cost=b.get("max_cost"),
            max_tokens=b.get("max_tokens"),
        )

    # --------------------------------------------------- separation guard
    def enforce_separation(self) -> None:
        """Raise if the Auditor is not independent of the Executor.

        Triggered by ``auditor.require_different_from: executor``. The check fails only
        when backend AND model are identical (a truly indistinguishable auditor).
        """

        exec_cfg = self._spec.get("executor_default", {}) or {}
        aud_cfg = self._spec.get("auditor", {}) or {}
        if aud_cfg.get("require_different_from") != "executor":
            return
        if (
            aud_cfg.get("backend") == exec_cfg.get("backend")
            and aud_cfg.get("model") == exec_cfg.get("model")
        ):
            raise ValueError(
                "RoleAgentRegistry: the Auditor must be INDEPENDENT of the Executor "
                f"(both backend={exec_cfg.get('backend')!r}, model={exec_cfg.get('model')!r}). "
                "Set auditor.require_different_from: executor with a DISTINCT backend/model."
            )
