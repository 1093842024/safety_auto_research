"""Per-role agent adapters — the configurable "best agent per step" framework.

Each research step (literature / hypothesis / coding / eval / audit / manager …) is
fulfilled by a ``RoleAgentAdapter`` chosen by ``RoleAgentRegistry`` from
``config/role_agents.yaml``. Supported backends:

  * ``deterministic`` — no LLM; delegates to the orchestrator's real deterministic
    executors via ``run_capability`` (so nothing is duplicated).
  * ``codex`` / ``claude_code`` — drive the Codex / Claude Code CLI as a tool-calling
    agent, reusing the existing ``CodexTransport`` / ``ClaudeCodeTransport``.
  * ``workbuddy`` — drive a WorkBuddy session over HTTP or a subprocess CLI.
  * ``llm_api`` — an OpenAI-compatible chat model used for reasoning (manager/eval
    design) and, for the auditor role, as an INDEPENDENT judge (must differ from the
    executor backend, per ``RoleAgentRegistry.enforce_separation``).

All CLI/HTTP adapters reuse the exact same transports as ``RemoteAgentHarness``, so
tool-calling and the agent-trace sink already work. The LLM adapter posts to an
OpenAI-compatible ``/chat/completions`` endpoint with no third-party dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable

from ...control_plane.task_state import AuditVerdict
from ...control_plane.task_state import StateRecord
from ...control_plane.task_state import SubtaskContract
from ...control_plane.task_state import SubtaskSpec
from ...control_plane.task_state import SUBTASK_TO_CAPABILITY
from ...control_plane.task_state import default_plan


# ----------------------------------------------------------------------- IO types
@dataclass
class ExecOutput:
    """What an Executor returns for one subtask (the paper's unverified ``o_i``).

    This summary NEVER mutates the trusted task state; only an ``AuditVerdict`` does.
    """

    status: str = "succeeded"  # succeeded | failed | blocked
    summary: str = ""
    output_refs: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass
class RoleBudget:
    """Per-role execution budget (paper: executor 1800s, mgr/aud 300s)."""

    max_seconds: int = 300
    max_cost: float | None = None
    max_tokens: int | None = None


# --------------------------------------------------------------------- base
class RoleAgentAdapter(ABC):
    """Base class for every step's agent."""

    role: str = "executor"
    backend: str = "deterministic"

    @abstractmethod
    def run_contract(self, contract: SubtaskContract, budget: RoleBudget) -> ExecOutput:
        """Execute one bounded subtask and return an unverified summary."""

    def audit(
        self,
        contract: SubtaskContract,
        exec_output: ExecOutput,
        env_snapshot: dict[str, Any] | None = None,
    ) -> AuditVerdict:
        # Only auditor adapters override this; default is a permissive pass-through.
        return AuditVerdict(
            completion="complete",
            integrity="clean",
            evidence_refs=[],
            rationale="default auditor (no checks configured)",
        )


# -------------------------------------------------------- deterministic executor
class LocalExecutorAdapter(RoleAgentAdapter):
    """Deterministic executor: delegates to the orchestrator's real executors.

    When no ``capability_runner`` is wired (e.g. unit tests), it records the subtask as
    a logged completion so the MEA loop still advances. No LLM is involved.
    """

    backend = "deterministic"

    def __init__(self, capability_runner: Callable[..., Any] | None = None) -> None:
        self._runner = capability_runner

    def run_contract(self, contract: SubtaskContract, budget: RoleBudget) -> ExecOutput:
        if self._runner is not None and contract.capability_id:
            try:
                stage, result = self._runner(
                    contract.params.get("run_id"),
                    contract.capability_id,
                    contract.params,
                )
                return ExecOutput(
                    status=getattr(result.final_status, "value", str(result.final_status)),
                    summary=result.detail or "",
                    output_refs=list(result.output_refs),
                    metrics={},
                )
            except Exception as exc:  # fail-soft: never let one capability block the loop
                return ExecOutput(
                    status="failed",
                    summary=f"capability {contract.capability_id} failed: {exc}",
                    metrics={},
                )
        return ExecOutput(
            status="succeeded",
            summary=f"[local] {contract.subtask_type} recorded (no capability runner)",
            metrics={},
        )


class DeterministicAdapter(LocalExecutorAdapter):
    """Explicit no-LLM executor for the evaluator role (R5).

    Semantically identical to :class:`LocalExecutorAdapter`; the distinct class makes the
    "eval must not use an LLM" intent explicit in config and code.
    """


# -------------------------------------------------------- transport-driven executor
def _adapt_agent_stage_result(resp: Any) -> ExecOutput:
    if not isinstance(resp, dict):
        return ExecOutput(status="failed", summary=str(resp))
    return ExecOutput(
        status=resp.get("final_status") or "succeeded",
        summary=resp.get("detail") or "",
        output_refs=list(resp.get("output_refs") or []),
        metrics={},
    )


class TransportExecutorAdapter(RoleAgentAdapter):
    """Drives a CLI/HTTP agent through one of the existing transports.

    The agent fulfills the ``task.run_stage`` message autonomously and may call back
    ``run_capability`` for sub-capabilities (resolved with the default executor, so the
    outer agent adapter is never re-entered).
    """

    backend = "transport"

    def __init__(
        self,
        transport: Any = None,
        capability_runner: Callable[..., Any] | None = None,
        tool_handler: Callable[[str, dict], Any] | None = None,
        agent_command: str | None = None,
        endpoint: str | None = None,
        transport_kind: str = "generic",
        model: str | None = None,
    ) -> None:
        if transport is None:
            if agent_command:
                from ...execution_plane.agent.transport import SubprocessTransport

                transport = SubprocessTransport(agent_command)
            elif endpoint:
                from ...execution_plane.agent.transport import HttpTransport

                transport = HttpTransport(endpoint)
            else:
                raise ValueError(
                    "TransportExecutorAdapter needs transport / agent_command / endpoint"
                )
        self._transport = transport
        self._runner = capability_runner
        self._tool_handler = tool_handler
        self._transport_kind = transport_kind
        self._model = model

    def run_contract(self, contract: SubtaskContract, budget: RoleBudget) -> ExecOutput:
        spec = {
            "stage_code": contract.capability_id or "open_goal",
            "params": contract.params,
            "open_goal": contract.goal,
            "available_tools": [
                "run_capability",
                "emit_event",
                "record_metric",
                "publish_artifact",
            ],
            "run_context": {
                "run_id": contract.params.get("run_id"),
                "objective": contract.goal,
            },
        }
        msg = {
            "msg_type": "task.run_stage",
            "run_id": contract.params.get("run_id"),
            "spec": spec,
        }
        resp = self._transport.request(msg, self._tool_handler)
        return _adapt_agent_stage_result(resp)


class CodexAdapter(TransportExecutorAdapter):
    backend = "codex"

    def __init__(
        self,
        capability_runner: Callable[..., Any] | None = None,
        tool_handler: Callable[[str, dict], Any] | None = None,
        model: str | None = None,
        max_tool_rounds: int = 8,
        timeout: int = 300,
        extra_args: list[str] | None = None,
    ) -> None:
        from ...execution_plane.agent.transport import CodexTransport

        transport = CodexTransport(
            capability_runner=capability_runner or _noop_runner,
            tool_handler=tool_handler,
            model=model,
            max_tool_rounds=max_tool_rounds,
            timeout=timeout,
            extra_args=extra_args,
        )
        super().__init__(
            transport=transport,
            capability_runner=capability_runner,
            tool_handler=tool_handler,
            transport_kind="codex",
            model=model,
        )


class ClaudeCodeAdapter(TransportExecutorAdapter):
    backend = "claude_code"

    def __init__(
        self,
        capability_runner: Callable[..., Any] | None = None,
        tool_handler: Callable[[str, dict], Any] | None = None,
        model: str | None = None,
        max_tool_rounds: int = 8,
        timeout: int = 600,
        extra_args: list[str] | None = None,
        claude_cmd: str | None = None,
    ) -> None:
        from ...execution_plane.agent.transport import ClaudeCodeTransport

        transport = ClaudeCodeTransport(
            capability_runner=capability_runner or _noop_runner,
            tool_handler=tool_handler,
            model=model,
            max_tool_rounds=max_tool_rounds,
            timeout=timeout,
            extra_args=extra_args,
            claude_cmd=claude_cmd,
        )
        super().__init__(
            transport=transport,
            capability_runner=capability_runner,
            tool_handler=tool_handler,
            transport_kind="claude_code",
            model=model,
        )


class WorkBuddyAdapter(TransportExecutorAdapter):
    backend = "workbuddy"

    def __init__(
        self,
        capability_runner: Callable[..., Any] | None = None,
        tool_handler: Callable[[str, dict], Any] | None = None,
        endpoint: str | None = None,
        agent_command: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            transport=None,
            capability_runner=capability_runner,
            tool_handler=tool_handler,
            agent_command=agent_command,
            endpoint=endpoint or os.environ.get("WORKBUDDY_ENDPOINT"),
            transport_kind="workbuddy",
            model=model,
        )


def _noop_runner(run_id: str, capability_id: str, params: dict | None = None):
    raise RuntimeError("no capability_runner bound to CLI adapter")


# -------------------------------------------------------- Manager (R0) adapter
def _parse_plan_json(text: str, known_types: set[str], max_subtasks: int) -> list[SubtaskSpec]:
    """Extract a JSON array of subtask specs from free-form Manager output.

    Drops entries whose ``subtask_type`` is not a known capability and coerces the rest
    into :class:`SubtaskSpec`. Raises ``ValueError`` if nothing usable is found (so the
    caller can fall back to the deterministic decomposition).
    """

    import json

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("manager output contained no JSON array")
    try:
        arr = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"manager output was not valid JSON: {exc}") from exc
    if not isinstance(arr, list):
        raise ValueError("manager output was not a JSON array")

    specs: list[SubtaskSpec] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        st = item.get("subtask_type") or item.get("type")
        if st not in known_types:
            continue
        specs.append(
            SubtaskSpec(
                subtask_type=st,
                goal=item.get("goal", ""),
                acceptance_criteria=list(item.get("acceptance_criteria") or []),
                boundary_constraints=list(item.get("boundary_constraints") or []),
                depends_on=list(item.get("depends_on") or []),
                params=dict(item.get("params") or {}),
            )
        )
        if len(specs) >= max_subtasks:
            break
    if not specs:
        raise ValueError("no known subtask_type found in manager output")
    return specs


class ManagerAdapter(RoleAgentAdapter):
    """The Manager role: decomposes the objective into a bounded, dependency-ordered plan.

    Mirrors the paper's ``Φ_mgr``: it owns the trusted task state and emits the subtask
    contracts ``c_i`` (goal + acceptance + boundary + dependencies). It does NOT execute
    — that is the Executor's job — and it does NOT audit — that is the Auditor's.

    The Manager's "brain" is whatever backend is configured for the ``manager`` role
    (typically an ``llm_api`` reasoning model). If that brain exposes a ``chat`` method
    we ask it to produce a JSON plan; on any failure (no brain, no API, bad JSON) we
    fall back to :func:`default_plan` so the loop always has a sensible decomposition.
    """

    backend = "manager"
    role = "manager"

    def __init__(self, brain: RoleAgentAdapter | None = None, runner: Any = None) -> None:
        self._brain = brain
        self._runner = runner

    def run_contract(self, contract, budget):
        # The Manager decomposes; it never executes a research subtask.
        raise NotImplementedError("ManagerAdapter decomposes objectives; it does not execute subtasks")

    def decompose(
        self,
        objective: str,
        state: Any = None,
        max_subtasks: int = 8,
    ) -> list[SubtaskSpec]:
        """Produce the decomposition (paper's planned subtask DAG) for ``objective``."""

        if self._brain is not None and hasattr(self._brain, "chat"):
            try:
                return self._llm_decompose(objective, state, max_subtasks)
            except Exception:
                pass  # fail-soft -> deterministic fallback
        return default_plan(objective)

    def _llm_decompose(self, objective, state, max_subtasks) -> list[SubtaskSpec]:
        known = set(SUBTASK_TO_CAPABILITY.keys())
        prior = ""
        if state is not None:
            done = [
                r.content.get("subtask_type")
                for r in state.records.values()
                if r.status == "completed"
            ]
            if done:
                prior = f"\nAlready-completed subtasks: {done}."
        user = (
            f"Research objective: {objective}{prior}\n\n"
            f"Decompose this into at most {max_subtasks} bounded subtasks forming a DAG. "
            "Each subtask is a JSON object with: "
            "subtask_type (one of " + ", ".join(sorted(known)) + "), "
            "goal (string), acceptance_criteria (list of strings), "
            "boundary_constraints (list of strings), and depends_on (list of subtask_type "
            "names that must finish first; empty if none). Order dependencies BEFORE their "
            "dependents. Return ONLY a JSON array of these objects."
        )
        text = self._brain.chat(  # type: ignore[attr-defined]
            system=(
                "You are the research Manager. You decompose an objective into a minimal, "
                "dependency-ordered set of bounded subtasks and output JSON only."
            ),
            user=user,
        )
        return _parse_plan_json(text, known, max_subtasks)


def _parse_decision_json(text: str) -> dict[str, str]:
    """Parse the R7 meta-decider's JSON decision from free-form output."""

    import json

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("meta decider output contained no JSON object")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"meta decider output not valid JSON: {exc}") from exc
    action = obj.get("action", "continue")
    if action not in ("continue", "escalate", "replan"):
        action = "continue"
    return {"action": action, "rationale": str(obj.get("rationale", ""))}


class MetaDeciderAdapter(RoleAgentAdapter):
    """The R7 meta-decider / router: strong-reasoning *budget-saving* routing decisions.

    Per the P3 cost strategy, the expensive reasoning model is RESERVED for meta/decision
    rounds (R0 Manager + R7 meta-decider) and is NOT spent on every executor step. The
    decider reads the verified task state and proposes a routing action
    (continue / escalate / replan); its output is gated downstream by the existing
    ``validate_route`` guard. Offline (no brain / no API / bad JSON) it fails soft to a
    deterministic ``{"action": "continue"}`` so the loop always progresses.
    """

    backend = "meta_decider"
    role = "meta_decider"

    def __init__(self, brain: RoleAgentAdapter | None = None, runner: Any = None) -> None:
        self._brain = brain
        self._runner = runner

    def run_contract(self, contract, budget):
        # The meta-decider decides routing; it never executes a research subtask.
        raise NotImplementedError("MetaDeciderAdapter decides routing; it does not execute subtasks")

    def decide(
        self,
        objective: str,
        state: Any = None,
        last_verdict: Any = None,
    ) -> dict[str, str]:
        """Produce the next routing action for the MEA loop (paper's meta/decision R7)."""

        if self._brain is not None and hasattr(self._brain, "chat"):
            try:
                return self._llm_decide(objective, state, last_verdict)
            except Exception:
                pass  # fail-soft -> deterministic continue
        return {"action": "continue", "rationale": "deterministic fallback (no meta brain)"}

    def _llm_decide(self, objective, state, last_verdict) -> dict[str, str]:
        done: list[str] = []
        if state is not None:
            done = [
                r.content.get("subtask_type")
                for r in state.records.values()
                if r.status == "completed"
            ]
        user = (
            f"Research objective: {objective}\n"
            f"Completed steps: {done}\n\n"
            "Decide the next routing action for the MEA loop. Reply with a JSON object: "
            '{"action": "continue" | "escalate" | "replan", "rationale": "..."}.'
        )
        text = self._brain.chat(  # type: ignore[attr-defined]
            system=(
                "You are the research meta-decider. You make budget-aware routing decisions "
                "for a long-horizon research loop; you never execute subtasks."
            ),
            user=user,
        )
        return _parse_decision_json(text)


# -------------------------------------------------------- independent LLM adapter
class LLMApiAdapter(RoleAgentAdapter):
    """An OpenAI-compatible chat model, used for reasoning roles (manager / design).

    Self-contained: reads ``OPENMLE_API_*`` env vars by default, overridable per call.
    No third-party SDK required (uses ``urllib``).
    """

    backend = "llm_api"
    role = "llm"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> None:
        self.model = model or os.environ.get("OPENMLE_API_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OPENMLE_API_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENMLE_API_API_KEY", "")
        self.system = system_prompt or "You are a careful autonomous-research agent."
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(self, system: str | None, user: str) -> str:
        if not self.base_url:
            raise RuntimeError("LLMApiAdapter has no base_url (set OPENMLE_API_BASE_URL)")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or self.system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"LLM API {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM API connection failed: {e.reason}") from e
        obj = json.loads(raw)
        return obj["choices"][0]["message"]["content"]

    def run_contract(self, contract: SubtaskContract, budget: RoleBudget) -> ExecOutput:
        user = (
            f"子任务类型: {contract.subtask_type}\n"
            f"目标: {contract.goal}\n"
            f"验收标准: {contract.acceptance_criteria}\n"
            f"约束: {contract.boundary_constraints}\n"
            f"已有证据: {contract.prior_evidence_refs}\n\n"
            "请给出该子任务的产出（要点、结论或可执行的下一步）。"
        )
        try:
            out = self.chat(None, user)
            return ExecOutput(status="succeeded", summary=out, metrics={})
        except Exception as exc:
            return ExecOutput(status="failed", summary=f"LLM failed: {exc}", metrics={})


# -------------------------------------------------------- independent auditor
def _check_integrity(env_snapshot: dict[str, Any] | None) -> str:
    """Read-only integrity check: did the executor mutate a protected artifact?

    The orchestrator's snapshot contains ONLY protected paths, so any diff is a violation.
    """

    if not env_snapshot:
        return "clean"
    before = env_snapshot.get("before") or {}
    after = env_snapshot.get("after") or {}
    if set(after.keys()) != set(before.keys()):
        return "violation"
    for k, v in after.items():
        if before.get(k) != v:
            return "violation"
    return "clean"


def _parse_llm_completion(text: str) -> str | None:
    t = text.strip().lower()
    if "incomplete" in t or "fail" in t or "not complete" in t:
        return "incomplete"
    if "complete" in t or "pass" in t or "accept" in t:
        return "complete"
    return None


class IndependentAuditorAdapter(RoleAgentAdapter):
    """Read-only auditor: deterministic integrity check + INDEPENDENT LLM judge.

    Hard constraint (enforced by the registry): its LLM backend/model must differ from
    the executor's, so the audit cannot be self-confirmed. The LLM is optional — if it is
    unavailable the auditor degrades to a deterministic completion signal (fail-soft).
    """

    backend = "llm_api"
    role = "auditor"

    def __init__(
        self,
        llm: LLMApiAdapter | None = None,
        capability_runner: Callable[..., Any] | None = None,
    ) -> None:
        self._llm = llm
        self._runner = capability_runner

    def run_contract(self, contract: SubtaskContract, budget: RoleBudget) -> ExecOutput:
        # Auditors verify; they never execute a subtask.
        raise NotImplementedError("IndependentAuditorAdapter does not execute subtasks")

    def audit(
        self,
        contract: SubtaskContract,
        exec_output: ExecOutput,
        env_snapshot: dict[str, Any] | None = None,
    ) -> AuditVerdict:
        integrity = _check_integrity(env_snapshot)
        # Deterministic completion signal from the executor's reported status.
        completion = (
            "complete" if (exec_output.status == "succeeded" and integrity == "clean") else "incomplete"
        )
        evidence: list[str] = []

        # Optional independent LLM second opinion (the "different model" requirement).
        if self._llm is not None:
            try:
                judge = self._llm.chat(
                    system=(
                        "You are an independent, READ-ONLY research auditor. You never "
                        "modify anything; you only judge whether a subtask met its acceptance "
                        "criteria based on the executor's summary. Reply with one word: "
                        "complete or incomplete, plus a short reason."
                    ),
                    user=json.dumps(
                        {
                            "goal": contract.goal,
                            "acceptance": contract.acceptance_criteria,
                            "exec_summary": exec_output.summary,
                            "metrics": exec_output.metrics,
                        },
                        ensure_ascii=False,
                    ),
                )
                llm_opinion = _parse_llm_completion(judge)
                if llm_opinion == "incomplete":
                    completion = "incomplete"  # conservative: disagreement blocks completion
                evidence.append("llm_audit")
            except Exception:
                pass  # fail-soft: deterministic-only audit

        status = "completed" if completion == "complete" else "pending"
        updates = [
            StateRecord(
                kind="requirement",
                key=contract.record_key or contract.subtask_type,
                status=status,
                content={"subtask_type": contract.subtask_type},
            )
        ]
        return AuditVerdict(
            completion=completion,
            integrity=integrity,
            state_updates=updates,
            evidence_refs=evidence,
            rationale="independent auditor (integrity + optional LLM)",
            audit_run_id=contract.params.get("run_id"),
        )
