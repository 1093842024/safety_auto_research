"""A minimal but real CLI agent implementing the line protocol (for tests only).

It is NOT a test (no ``test_`` prefix). The platform launches it via
``SubprocessTransport``; it reads one task line on stdin, may emit ``ToolCall``
lines (which the platform answers with ``ToolResult``), then writes the terminal
``AgentDecision`` / ``AgentStageResult`` line and exits.

Run manually:  echo '{"msg_type":"task.decide",...}' | python agent_fixture.py
"""

import json
import sys


def main() -> None:
    raw = sys.stdin.readline()
    if not raw.strip():
        return
    msg = json.loads(raw)

    if msg["msg_type"] == "task.run_stage":
        run_id = msg["run_id"]
        # 1) ask the platform to record a metric (exercises the tool-call round trip)
        sys.stdout.write(
            json.dumps(
                {
                    "msg_type": "agent.tool_call",
                    "call_id": "c1",
                    "tool": "record_metric",
                    "args": {"run_id": run_id, "name": "agent_substep", "value": 1.0},
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        # 2) read the platform's tool result (ignored here, but proves the loop)
        _ = sys.stdin.readline()
        # 3) terminal: a real platform event the agent produced
        sys.stdout.write(
            json.dumps(
                {
                    "msg_type": "agent.stage_result",
                    "event": {
                        "event_type": "artifact_published",
                        "run_id": run_id,
                        "artifact_id": "a-sub",
                        "artifact_type": "dataset_release",
                    },
                    "final_status": "succeeded",
                    "gate_result": "passed",
                    "output_refs": ["o1"],
                    "detail": "executed via subprocess protocol",
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    elif msg["msg_type"] == "task.decide":
        sys.stdout.write(
            json.dumps(
                {
                    "msg_type": "agent.decision",
                    "decision_type": "exit_success",
                    "target_stage": None,
                    "rationale": "remote override via subprocess",
                }
            )
            + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
