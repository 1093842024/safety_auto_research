"""Agent harness — the decider in an agent-driven research loop.

Minimal-pivot refactor (architecture discussion 2026-07-20):

  * The control plane + platform contracts stay as the agent's *runtime + memory*.
  * ``PlatformSDK`` is the agent's *tool surface*.
  * ``StageExecutor`` becomes a *default / fallback* implementation; the agent fulfills
    the ``StageTaskSpec`` autonomously.
  * ``IterationRouter`` is demoted to a *reference policy + safety validator*; the
    authoritative decision is made here, by the ``AgentHarness``.

``LocalAgentHarness`` is the default backend. It mimics an agent by accepting the
router's reference policy for decisions and delegating stage runs to the deterministic
executors. The system therefore runs end-to-end with **no external agent** — and this
backend is also the fallback when no remote agent is configured.

``RemoteAgentHarness`` is the real integration seam. It speaks the JSON wire protocol
(see :mod:`protocol` and :mod:`transport`) with an external agent (a ``codex`` CLI, a
WorkBuddy session, or any HTTP agent). The agent receives ``TaskDecide`` / ``TaskRunStage``
messages, may call platform tools via ``ToolCall``/``ToolResult``, and returns
``AgentDecision`` / ``AgentStageResult``. This is exactly where Codex / WorkBuddy plug in.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Any

from ...platform_contracts.enums import DecisionType
from ...platform_contracts.events import ALL_EVENT_MODELS
from ...platform_contracts.events import AgentStepEvent
from ...platform_contracts.events import BasePlatformEvent
from ...platform_contracts.objects import WorkflowRun
from ..base import ExecResult
from ..base import StageTaskSpec
from ...control_plane.service import NotFoundError
from ..registry import AdapterRegistry
from ..registry import default_registry
from ..decision.router import IterationRouter
from ..decision.router import RouteDecision
from .protocol import AgentDecision
from .protocol import AgentStageResult
from .protocol import RouteSuggestion
from .protocol import TaskDecide
from .protocol import TaskRunStage
from .transport import CallableTransport
from .transport import SubprocessTransport
from .transport import TraceSink
from .transport import Transport

# The SDK surface exposed to an agent as callable tools (spec §9.4), plus the
# agent-facing ``run_capability`` tool that lets it invoke any infrastructure-layer
# capability (see execution_plane.capabilities).
AGENT_TOOL_NAMES: list[str] = [
    "load_object",
    "publish_artifact",
    "emit_event",
    "request_approval",
    "record_metric",
    "register_lesson",
    "run_capability",
    "observe_hypothesis",
    "backpropagate_insight",
    "register_experience",
    "query_experiences",
    "compact_research_state",
]

# The OUTER loop's audit (layer_11) and recursive-improvement (layer_09) capabilities are
# reserved for the control plane. An INNER-loop (optimization) agent may drive ANY other
# capability — literature search, eval, red-teaming, kaggle_eval, … — but never these two,
# otherwise it could evaluate or mutate its own result, destroying the evaluation-bias /
# self-confirmation guarantee the outer loop exists to provide. The control plane calls
# these capabilities directly (bypassing this check). Aliases are included so an agent
# cannot slip past the guard by using a synonym.
OUTER_LOOP_RESERVED_CAPS: frozenset[str] = frozenset({
    "layer_11_external_audit", "external_audit", "audit",
    "layer_09_self_iterative_evolution", "self_iterative_evolution", "self_evolution",
    # layer_12 induces / reviews the rubric the run is GRADED against. If an inner-loop
    # agent could invoke it, it could regenerate its own grading standard (or relax a
    # threshold) and then trivially "pass" — a strictly worse self-confirmation hole than
    # self-auditing. The rubric is produced once by the control plane, frozen, and handed
    # to the inner loop READ-ONLY as an execution contract.
    "layer_12_rubric_induction", "rubric_induction", "rubric",
})


def assert_inner_capability_allowed(capability_id: str) -> None:
    """Raise if an inner-loop agent tries to invoke a reserved OUTER-loop capability."""

    if capability_id in OUTER_LOOP_RESERVED_CAPS:
        raise ValueError(
            f"capability '{capability_id}' is reserved for the OUTER loop (control plane) "
            f"and cannot be invoked by an INNER-loop agent. It would let the inner loop "
            f"evaluate or mutate its own result."
        )


# Resolve an event model from its ``event_type`` default so a remote agent's JSON
# response can be rebuilt into a concrete platform event before it is committed.
_EVENT_MODEL_BY_TYPE: dict[str, type[BasePlatformEvent]] = {}
for _model in ALL_EVENT_MODELS:
    _field = _model.model_fields.get("event_type")
    if _field is None:
        continue
    _default = getattr(_field.default, "value", None)
    if _default is None:
        # BasePlatformEvent has no default event_type; skip it.
        continue
    _EVENT_MODEL_BY_TYPE[_default] = _model


def _json_safe(value: Any) -> Any:
    """Coerce an SDK tool result into a JSON-serializable value.

    The line transports (``SubprocessTransport`` / ``HttpTransport``) write every
    ``platform.tool_result`` with ``json.dumps``. SDK methods may return pydantic
    models (e.g. ``load_object`` -> ``Artifact``), enums or datetimes; without this
    coercion the *platform* crashes after the agent's tool call and the whole run
    dies mid-loop. Best-effort: pydantic ``model_dump`` -> ``to_dict`` -> ``str``.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict())
        except Exception:
            pass
    return str(value)


def _rebuild_event(data: dict[str, Any]) -> BasePlatformEvent | None:
    """Best-effort rebuild of a platform event from an agent-returned dict."""

    if not data:
        return None
    et = data.get("event_type")
    model = _EVENT_MODEL_BY_TYPE.get(et)
    if model is None:
        return None
    return model.model_validate(data)


class AgentHarness(ABC):
    """The decider. In agent mode the orchestrator asks the harness, not the router."""

    def attach_sdk(self, sdk: Any) -> None:
        """Bind the platform SDK so the harness can fulfill an agent's tool calls.

        No-op for backends that do not need it (e.g. ``LocalAgentHarness``).
        """

        self.sdk = sdk

    def attach_runner(self, runner: Any) -> None:
        """Bind the capability runner (orchestrator.run_capability) for the agent's
        ``run_capability`` tool. No-op for backends that do not need it."""

        self._capability_runner = runner

    @abstractmethod
    def decide(
        self,
        run: WorkflowRun,
        event: BasePlatformEvent | None,
        suggestion: RouteDecision,
    ) -> RouteDecision:
        """Decide the next step from the event + run state.

        ``suggestion`` is the router's reference policy (R1-R10); the agent may accept,
        modify, or override it. The returned decision is still passed through the
        router's safety guardrail (``validate_route``) before it is committed.
        """

    @abstractmethod
    def run_stage(self, spec: StageTaskSpec, sdk: Any) -> ExecResult:
        """Fulfill a stage task spec autonomously and return the execution result."""


class LocalAgentHarness(AgentHarness):
    """Default backend: an agent-equivalent that uses the reference policy + executors.

    Behavior-preserving — with this harness the closed loop behaves exactly like the old
    scripted orchestrator. It exists so the platform runs with no external agent, and as
    the fallback when no ``RemoteAgentHarness`` is configured.
    """

    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self.registry = registry or default_registry()
        self.router = IterationRouter()

    def decide(
        self,
        run: WorkflowRun,
        event: BasePlatformEvent | None,
        suggestion: RouteDecision,
    ) -> RouteDecision:
        # Accept the reference policy as-is (autopilot). A real agent would reason here.
        return suggestion

    def run_stage(self, spec: StageTaskSpec, sdk: Any) -> ExecResult:
        executor = self.registry.resolve(spec.stage_run.stage_code)
        if executor is None:
            raise NotFoundError(f"no executor registered for stage {spec.stage_run.stage_code}")
        return executor.execute(spec.stage_run, sdk, spec.params)


class RemoteAgentHarness(AgentHarness):
    """Integration seam for a real external agent (Codex / WorkBuddy).

    Wire it by passing either ``agent_command`` (a CLI agent speaking the line
    protocol from :mod:`transport`) or a ``transport`` (a :class:`Transport` instance,
    or a plain callable ``fn(message, tool_handler) -> dict`` for tests / embedding).
    When neither is configured, calls raise ``NotImplementedError`` to make the seam
    explicit — this is the point where an autonomous agent connects.
    """

    def __init__(
        self,
        agent_command: str | None = None,
        transport: Transport | Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
        registry: AdapterRegistry | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.agent_command = agent_command
        self._trace_sink = trace_sink
        if transport is None and agent_command:
            transport = SubprocessTransport(agent_command, trace_sink=self._trace_sink)
        elif transport is not None and not isinstance(transport, Transport):
            # Convenience: a plain callable becomes a CallableTransport.
            transport = CallableTransport(transport)
        self._transport: Transport | None = transport
        self._registry = registry or default_registry()
        self.sdk: Any = None
        self._capability_runner: Any = None

    # --------------------------------------------------- agent trace sink
    def _make_trace_sink(self) -> TraceSink:
        """Default trace sink: persist each agent step as an ``AgentStepEvent``.

        Called lazily once the SDK is attached, so steps the inner-loop agent makes
        during ``run_stage`` are recorded into the run's event log (the "Agent 执行流水").
        """

        def sink(run_id: str, step: dict[str, Any]) -> None:
            if self.sdk is None:
                return
            event = AgentStepEvent(
                run_id=run_id,
                seq=int(step.get("seq", 0)),
                kind=step.get("kind", "tool_call"),
                tool=step.get("tool"),
                args_summary=step.get("args_summary", ""),
                result_summary=step.get("result_summary", ""),
                detail=step.get("detail"),
            )
            self.sdk.emit_event(event)

        return sink

    # --------------------------------------------------------------- tool dispatch
    def _tool_handler(self, tool: str, args: dict[str, Any]) -> Any:
        if self.sdk is None:
            raise RuntimeError("RemoteAgentHarness has no SDK bound; call attach_sdk(sdk).")
        if tool == "run_capability":
            # Enforce the inner/outer context separation: an inner-loop agent may only
            # drive research/optimization capabilities, never the outer-loop audit or
            # recursive-improvement capabilities (which would let it judge its own work).
            assert_inner_capability_allowed(args.get("capability_id"))
            if self._capability_runner is None:
                raise RuntimeError(
                    "RemoteAgentHarness has no capability runner bound; construct the "
                    "orchestrator with mode='agent' so it wires one."
                )
            stage, result = self._capability_runner(**args)
            return _json_safe({
                "stage_run_id": stage.stage_run_id,
                "layer_code": stage.stage_code,
                "event_type": result.event.event_type if result.event else None,
                "gate_result": result.gate_result.value,
                "output_refs": list(result.output_refs),
                "detail": result.detail,
            })
        # M2 fix: explicit whitelist BEFORE getattr — any public SDK method outside
        # AGENT_TOOL_NAMES (e.g. mark_pruned/mark_merged) must not be agent-callable.
        if tool not in AGENT_TOOL_NAMES:
            raise ValueError(f"unknown agent tool: {tool!r} (available: {AGENT_TOOL_NAMES})")
        method = getattr(self.sdk, tool, None)
        if method is None:
            raise ValueError(f"unknown agent tool: {tool!r} (available: {AGENT_TOOL_NAMES})")
        return _json_safe(method(**args))

    # ------------------------------------------------------------------ decide
    def decide(
        self,
        run: WorkflowRun,
        event: BasePlatformEvent | None,
        suggestion: RouteDecision,
    ) -> RouteDecision:
        if self._transport is None:
            raise NotImplementedError(
                "RemoteAgentHarness is not wired: pass `agent_command` (Codex/WorkBuddy CLI) "
                "or a `transport`. This is the integration point for an autonomous agent."
            )
        task = TaskDecide(
            run_id=run.run_id,
            run=run.model_dump(mode="json"),
            event=event.model_dump(mode="json") if event is not None else None,
            suggestion=RouteSuggestion(
                decision_type=suggestion.decision_type.value,
                target_stage=suggestion.target_stage,
                reason_codes=list(suggestion.reason_codes),
                evidence_refs=list(suggestion.evidence_refs),
                route_rule=suggestion.route_rule,
                summary=suggestion.summary,
            ),
            available_tools=list(AGENT_TOOL_NAMES),
        )
        resp = self._transport.request(task.model_dump(mode="json"), self._tool_handler)
        parsed = AgentDecision.model_validate(resp)
        return RouteDecision(
            decision_type=DecisionType(parsed.decision_type),
            target_stage=parsed.target_stage,
            reason_codes=list(parsed.reason_codes),
            evidence_refs=list(parsed.evidence_refs),
            route_rule="AGENT",
            summary=parsed.rationale,
        )

    # --------------------------------------------------------------- run_stage
    def run_stage(self, spec: StageTaskSpec, sdk: Any) -> ExecResult:
        from ...platform_contracts.enums import GateResult
        from ...platform_contracts.enums import StageStatus

        self.attach_sdk(sdk)
        if self._trace_sink is None:
            self._trace_sink = self._make_trace_sink()
        if self._transport is None:
            raise NotImplementedError(
                "RemoteAgentHarness is not wired: pass `agent_command` (Codex/WorkBuddy CLI) "
                "or a `transport`. This is the integration point for an autonomous agent."
            )
        task = TaskRunStage(
            run_id=spec.run_context.get("run_id") or spec.stage_run.run_id,
            spec=spec.to_dict(),
            available_tools=list(spec.available_tools) or list(AGENT_TOOL_NAMES),
        )
        resp = self._transport.request(
            task.model_dump(mode="json"), self._tool_handler, trace_sink=self._trace_sink
        )
        parsed = AgentStageResult.model_validate(resp)

        event = _rebuild_event(parsed.event)
        if event is not None:
            # The remote agent returns the platform event it produced; commit it locally.
            self.sdk.emit_event(event)

        return ExecResult(
            final_status=StageStatus(parsed.final_status),
            gate_result=GateResult(parsed.gate_result),
            event=event,
            output_refs=list(parsed.output_refs),
            detail=parsed.detail,
        )
