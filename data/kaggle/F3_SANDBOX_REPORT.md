# F3 Hard-Isolation Sandbox — Verification Report

- Generated: 2026-08-10 09:59 UTC
- Workflow run: `run-3ac3fdabf89a`
- Executor: `scripts/build_agent_sandbox.sh` + Colima/Docker
- Network mode: `none`

## Per-task result
| Task | Accuracy | CV-folds | Gate | Data RO | Net blocked | EvalEvent |
|------|----------|----------|------|---------|-------------|-----------|
| titanic | 0.8257 | 5 | PASS | True | True | yes |
| spaceship | 0.7965 | 5 | FAIL | True | True | yes |

## Isolation verdict
- [x] dataset mounted read-only (writes to /data rejected)
- [x] outbound network blocked (--network none)
- [x] every sandbox run produced a real EvalCompletedEvent on the host

## Conclusion
F3 hard isolation VERIFIED: agent-mode research executed inside a disposable container with a read-only dataset mount and no network access, and the genuine CV metric was written back to the control plane as a real EvalCompletedEvent. The previously-deferred result-writeback is now implemented.