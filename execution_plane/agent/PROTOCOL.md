# Agent ↔ Platform Wire Protocol

How an **external autonomous agent** (Codex, WorkBuddy, or any HTTP/CLI agent)
connects to the unified safety R&D platform and drives the research loop.

The platform owns **state, memory (events), tools, and guardrails**. The agent owns
**decisions and execution**. This file is the contract between them.

---

## 1. Messages

Every message is a JSON object with a `msg_type` discriminator. The full JSON
Schema for each type is served live at the control-plane endpoint
`GET /agent/protocol` (so an agent can fetch the contract at runtime).

| `msg_type` | Direction | Purpose |
|------------|-----------|---------|
| `task.decide` | platform → agent | "Decide the next step." Carries run state, the just-completed event, and the router's reference suggestion (`RouteSuggestion`). |
| `task.run_stage` | platform → agent | "Fulfill this stage." Carries a `StageTaskSpec` (goal + tools + context). |
| `platform.tool_result` | platform → agent | Answer to an agent `tool_call`. |
| `agent.decision` | agent → platform | The chosen next step (`decision_type`, `target_stage`, `reason_codes`, `rationale`). **Terminal.** |
| `agent.stage_result` | agent → platform | Stage outcome, including the platform `event` to emit. **Terminal.** |
| `agent.tool_call` | agent → platform | Request to invoke an SDK tool (see §3). Zero or more per task. |

`agent.decision` / `agent.stage_result` are **terminal** — they end the turn.

---

## 2. The decision flow (`task.decide` → `agent.decision`)

```jsonc
// platform → agent
{ "msg_type": "task.decide", "run_id": "run-1",
  "run": { /* WorkflowRun.model_dump */ },
  "event": { /* completed-stage event, or null */ },
  "suggestion": { "decision_type": "revisit", "target_stage": "10_adversarial_data_generation",
                  "reason_codes": ["R10"], "route_rule": "R10", "summary": "..." },
  "available_tools": ["load_object", "emit_event", "record_metric", "..."] }

// agent → platform  (the agent MAY accept, modify, or override the suggestion)
{ "msg_type": "agent.decision", "decision_type": "exit_success",
  "target_stage": null, "reason_codes": ["agent_override"], "rationale": "goal met" }
```

The returned decision is **still passed through the router's safety guardrail**
(`validate_route`) before it is committed. An illegal decision (e.g. `continue` with a
`target_stage`) is rejected with `400` regardless of who proposed it.

---

## 3. The execution flow (`task.run_stage` → tools → `agent.stage_result`)

```jsonc
// platform → agent
{ "msg_type": "task.run_stage", "run_id": "run-1",
  "spec": { "stage_code": "03_eval", "stage_run_id": "stage-9",
            "params": {...}, "available_tools": [...], "run_context": {...} } }

// agent → platform  (optional, repeatable) — call a platform tool
{ "msg_type": "agent.tool_call", "call_id": "c1", "tool": "record_metric",
  "args": { "run_id": "run-1", "name": "accuracy", "value": 0.92 } }

// platform → agent  (answer)
{ "msg_type": "platform.tool_result", "call_id": "c1", "ok": true,
  "result": null, "error": null }

// agent → platform  (terminal) — the stage is done, emit its event
{ "msg_type": "agent.stage_result",
  "event": { "event_type": "eval_completed", "run_id": "run-1", ... },
  "final_status": "succeeded", "gate_result": "passed",
  "output_refs": ["o1"], "metrics": {...}, "detail": "agent ran eval" }
```

The agent may call any of the six platform tools, plus the agent-facing `run_capability`
tool described in §3.1.

| Tool | Signature |
|------|-----------|
| `load_object(ref)` | read a canonical object (`run:`, `stage:`, `decision:`, `events:`, `artifact:`, `lesson:`) |
| `publish_artifact(payload, schema_version, metadata?)` | publish an output + emit `ArtifactPublishedEvent` |
| `emit_event(event)` | append a platform event to the durable log |
| `request_approval(run_id, payload)` | open a HITL gate (`ApprovalRequiredEvent`) |
| `record_metric(run_id, name, value, tags?)` | record an observation metric |
| `register_lesson(payload)` | promote a `LessonCard` + emit `LessonPromotedEvent` (reinjection) |
| `run_capability(run_id, capability_id, params)` | **invoke an infrastructure-layer capability** (see §3.1) |

### 3.1 `run_capability` — orchestrating the ten infrastructure layers

The platform registers every infrastructure layer (① 文献检索 … ⑩ 数据生成对抗) as a
callable **capability** in `execution_plane.capabilities`. The agent invokes one by id:

```jsonc
// agent → platform
{ "msg_type": "agent.tool_call", "call_id": "c2", "tool": "run_capability",
  "args": { "run_id": "run-1", "capability_id": "layer_01_literature_research",
            "params": { "label": "adversarial-safety-survey" } } }

// platform → agent  (answer: the audited stage run that was created)
{ "msg_type": "platform.tool_result", "call_id": "c2", "ok": true,
  "result": { "stage_run_id": "stage-12", "layer_code": "01_literature_research",
              "event_type": null, "gate_result": "passed",
              "output_refs": ["a-7"], "detail": "stub capability ..." }, "error": null }
```

What `run_capability` does (and why it stays safe):
1. Resolves `capability_id` from the `CapabilityRegistry` (404 if unknown).
2. Creates a real `StageRun` for the layer, transitions it `running` → `succeeded`.
3. Runs the layer's bound executor (real adapter for ③/⑧/⑩; deterministic stub otherwise),
   which emits events + publishes artifacts through the same `PlatformSDK` six-method surface.
4. Returns a serializable summary to the agent.

Because every capability invocation goes through the same audited, state-machine-validated
path, the agent can orchestrate the **whole research chain end-to-end** — calling literature
search, then idea generation, then eval, then red-team, then experience — by issuing a
sequence of `run_capability` tool calls, instead of being confined to one assigned stage.

The full capability catalog (id, layer_code, title, description, param schema) is served at
`GET /agent/protocol` under the `capabilities` field, so an agent can discover what it may
call at runtime. A `StageTaskSpec` sent via `task.run_stage` also carries the catalog in
`run_context.capabilities` (and, for open goals, `open_goal` + `candidate_capabilities`).

Open-goal orchestration: `ClosedLoopOrchestrator.dispatch_open_goal(run_id, goal, candidates)`
hands the agent a goal and lets it invoke `run_capability` for whichever layers it chooses.
The orchestrator only wraps the turn in a container `StageRun`; the agent decides the sequence.

---

## 4. Transports (how bytes move)

Implement `execution_plane.agent.transport.Transport` or use a built-in:

* **`CallableTransport(fn)`** — wraps a Python callable `fn(message, tool_handler) -> dict`.
  Use for tests / embedding. A legacy one-argument callable `fn(message)` is also accepted.
* **`SubprocessTransport(agent_command)`** — runs `agent_command` as a child process.
  One JSON object per line on stdin/stdout:
  * platform writes `task.*`;
  * agent may write `agent.tool_call` lines (platform answers each with `platform.tool_result`);
  * agent writes a terminal `agent.decision` / `agent.stage_result` and exits.
* **`HttpTransport(endpoint, headers?)`** — skeleton for a remote HTTP agent
  (Codex / WorkBuddy session). POSTs the task and reads the JSON response; tool calls are
  served by the platform's own SDK endpoints (to be added). Requires `requests`.

---

## 5. Wiring an agent (two lines)

```python
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.agent import RemoteAgentHarness

# CLI agent speaking the line protocol:
orch = ClosedLoopOrchestrator(service, mode="agent",
                               harness=RemoteAgentHarness(agent_command="python agent.py"))

# Or an HTTP agent (Codex / WorkBuddy session):
# orch = ClosedLoopOrchestrator(service, mode="agent",
#                                harness=RemoteAgentHarness(transport=HttpTransport("https://agent/internal/step")))
```

When no `agent_command` / `transport` is supplied, `RemoteAgentHarness.decide` /
`run_stage` raise `NotImplementedError` — making the integration seam explicit.
