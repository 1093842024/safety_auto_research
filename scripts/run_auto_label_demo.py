"""End-to-end demo: 启动后端 + auto_label 能力 + 飞轮调度，验证整条链路。

跑法（后端 :8000 必须已启动）::

    /Users/glennge/.workbuddy/binaries/python/envs/default/bin/python \\
        scripts/run_auto_label_demo.py

行为：
  1. 创建 FLYWHEEL 类型的 workflow run（与 FlywheelPanel "启动飞轮" 一致）。
  2. 直接调用 ``run_capability(auto_label)`` 生成一份含 30 条样本的合成 CSV，
     让 executor 用默认 provider（Venus 代理 + deepseek-v4-flash-official）
     标好 label，并落盘到 ``data/kaggle/titanic/badcase_labeled.csv``。
  3. 启动事件驱动调度器（threshold=10, poll=1s, auto_clear=True），把
     ``badcase_labeled.csv`` 当作将被持续追加的"业务日志"，观察自动触发并
     停止调度。

任何一步失败都把非零返回码 + 上下文写到 stderr，便于 CI 与人工排查。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import pandas as pd

# Make the package importable when running from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safety_auto_research.control_plane.service import ControlPlaneService  # noqa: E402
from safety_auto_research.control_plane.store import Repository  # noqa: E402


def _make_run(service: ControlPlaneService) -> str:
    """Spin up a fresh FLYWHEEL run."""
    from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
    from safety_auto_research.platform_contracts.enums import RunType

    run = service.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="flywheel",
            run_type=RunType.FLYWHEEL,
            entry_stage="badcase_retrain",
            target_id="flywheel-titanic",
            objective_snapshot={"name": "auto_label + scheduler demo", "eval_metric": "accuracy"},
        )
    )
    return run.run_id


def _synth_unlabeled_csv(path: str, n_rows: int) -> None:
    """Write an unlabeled badcase CSV in the same shape as the titanic preset,
    minus the target column."""
    rows = [
        {
            "PassengerId": 9000 + i,
            "Pclass": 3 if i % 2 == 0 else 1,
            "Name": f"Demo, Mr. Tester-{i}",
            "Sex": "female" if i % 3 == 0 else "male",
            "Age": 25.0 + (i % 6),
            "SibSp": 0,
            "Parch": 0,
            "Ticket": f"DEMO {i:05d}",
            "Fare": 7.25 if i % 2 == 0 else 80.0,
            "Cabin": "",
            "Embarked": "S",
        }
        for i in range(n_rows)
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def step1_run_capability_auto_label(run_id: str, csv_path: str, out_path: str) -> None:
    """Drive the ``auto_label`` capability via the orchestrator. Returns once
    the labeled CSV is written; raises on any failure (incl. provider outage)."""
    from safety_auto_research.control_plane.api import create_app

    # The capability runner requires the FastAPI deps container; build one
    # against the same in-memory store the service is using.
    svc = ControlPlaneService(Repository())
    app = create_app(svc)
    deps = app.state.deps
    orch = deps.orchestrator
    params = {
        "input_path": csv_path,
        "output_path": out_path,
        "target_col": "Survived",
        "drop_cols": ["PassengerId", "Name", "Ticket"],
        "max_rows": 30,
        "batch_size": 1,
        # Use the default provider (Venus + deepseek-v4-flash-official). Override
        # via VENUS_LLM_API_KEY / VENUS_BASE_URL env if needed.
    }
    _, result = orch.run_capability(run_id, "auto_label", params)
    print(f"[auto_label] status={result.final_status.value} gate={result.gate_result.value}")
    print(f"[auto_label] detail: {result.detail}")
    if not os.path.exists(out_path):
        raise SystemExit(f"[auto_label] expected labeled CSV at {out_path!r}, found nothing")


def step2_scheduler_smoke(run_id: str, badcase_path: str) -> None:
    """Spin up the scheduler at threshold=10, watch it auto-trigger, stop it."""
    from safety_auto_research.control_plane.api import create_app

    svc = ControlPlaneService(Repository())
    app = create_app(svc)
    deps = app.state.deps
    sched = deps.svc  # we go through the real router by calling the service+orch
    # Instead of re-instantiating the router, drive the same module-level scheduler
    # by calling into the router via TestClient (no HTTP socket required).
    from fastapi.testclient import TestClient

    client = TestClient(app)
    r = client.post(
        f"/workflow-runs/{run_id}/flywheel/schedule",
        json={
            "badcase_path": badcase_path,
            "threshold": 10,
            "poll_interval_sec": 1.0,
            "auto_clear_after_trigger": True,
            "preset": "titanic",
            "model": "logreg",
            "eval_metric": "accuracy",
            "badcase_ratio": 0.3,
        },
    )
    if r.status_code != 200:
        raise SystemExit(f"[scheduler] POST /schedule failed: {r.status_code} {r.text}")
    print(f"[scheduler] started: {r.json()}")

    # Append 12 badcase rows in one shot (simulate an upstream collector batch).
    rows = pd.read_csv(badcase_path)
    n_extra = max(0, 12 - len(rows))
    if n_extra:
        extra = pd.DataFrame(
            [
                {
                    "PassengerId": 10000 + i,
                    "Survived": 0,
                    "Pclass": 3,
                    "Name": f"Demo, Mr. Extra-{i}",
                    "Sex": "male",
                    "Age": 30.0,
                    "SibSp": 0,
                    "Parch": 0,
                    "Ticket": f"EXT {i:05d}",
                    "Fare": 7.25,
                    "Cabin": "",
                    "Embarked": "S",
                }
                for i in range(n_extra)
            ]
        )
        rows = pd.concat([rows, extra], ignore_index=True)
        rows.to_csv(badcase_path, index=False)

    deadline = time.time() + 30.0
    triggered = False
    while time.time() < deadline:
        s = client.get(f"/workflow-runs/{run_id}/flywheel/schedule").json()
        if (s.get("trigger_count") or 0) >= 1:
            print(f"[scheduler] fired → {s}")
            triggered = True
            break
        time.sleep(0.5)
    if not triggered:
        raise SystemExit("[scheduler] did not fire within 30s")

    stop = client.delete(f"/workflow-runs/{run_id}/flywheel/schedule")
    if stop.status_code != 200:
        raise SystemExit(f"[scheduler] DELETE /schedule failed: {stop.status_code} {stop.text}")
    print(f"[scheduler] stopped: {stop.json()}")


def main() -> int:
    svc = ControlPlaneService(Repository())
    run_id = _make_run(svc)

    with tempfile.TemporaryDirectory(prefix="auto-label-demo-") as tmp:
        unlabeled_csv = os.path.join(tmp, "badcase_unlabeled.csv")
        labeled_csv = os.path.join(tmp, "badcase_labeled.csv")
        _synth_unlabeled_csv(unlabeled_csv, n_rows=12)

        print(f"[demo] run_id={run_id}")
        print(f"[demo] unlabeled CSV → {unlabeled_csv}")
        print("[demo] === step 1: auto_label capability ===")
        step1_run_capability_auto_label(run_id, unlabeled_csv, labeled_csv)
        n_labeled = sum(1 for _ in open(labeled_csv)) - 1
        print(f"[demo] labeled {n_labeled} rows -> {labeled_csv}")

        if n_labeled < 1:
            print("[demo] WARNING: 0 rows labeled — provider may be unreachable; skipping step 2", file=sys.stderr)
            return 2

        print("[demo] === step 2: event-driven scheduler ===")
        step2_scheduler_smoke(run_id, labeled_csv)

    print("[demo] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())