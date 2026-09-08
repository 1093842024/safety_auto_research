#!/usr/bin/env python3
"""B 飞轮型端到端 demo：用真实 Titanic 数据跑一轮数据飞轮。

Pipeline（一次飞轮迭代）：
  1. 建一个 ``RunType.FLYWHEEL`` 研究 run。
  2. 自动采集坏例：冻结基线（如 gbm）在 held-out 上的误判样本 → 写 badcase CSV。
  3. 回放重训：按 ``badcase:original`` 自适应配比拼接重训（冻结同一模型族）。
  4. 回归门：冻结原始评测集上主指标不退化（硬护栏）+ 坏例召回提升（收益）。

产出真实 ``EvalCompletedEvent``，并打印 baseline vs retrained 指标与 ACCEPT/REJECT 裁决。

Run:
  /Users/glennge/.workbuddy/binaries/python/envs/default/bin/python \
      safety_auto_research/scripts/run_flywheel_demo.py [--preset titanic] [--model gbm] \
      [--badcase-ratio 0.3] [--regression-tol 0.0] [--rounds 1]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest  # noqa: E402
from safety_auto_research.control_plane.service import ControlPlaneService  # noqa: E402
from safety_auto_research.execution_plane import ClosedLoopOrchestrator  # noqa: E402
from safety_auto_research.execution_plane.capabilities.badcase_retrain_executor import (  # noqa: E402
    BadcaseRetrainExecutor,
)
from safety_auto_research.platform_contracts.enums import RunType  # noqa: E402

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DATA_DIR = os.path.join(_PKG_ROOT, "data", "kaggle", "titanic")


def _fmt(v: float | None, d: int = 4) -> str:
    return f"{v:.{d}f}" if v is not None else "—"


def _run_one_round(orch: ClosedLoopOrchestrator, run_id: str, args: argparse.Namespace) -> dict:
    # Auto-collect badcase into a scratch dir (never pollute the repo's data/kaggle tree).
    scratch = tempfile.mkdtemp(prefix="flywheel-demo-")
    badcase_path, n_badcase = BadcaseRetrainExecutor.collect_badcase(
        data_dir=args.data_dir,
        preset=args.preset,
        target=args.target,
        model_name=args.model,
        fe=args.fe,
        heldout_frac=args.heldout_frac,
        heldout_seed=args.heldout_seed,
        out_path=os.path.join(scratch, "badcase.csv"),
    )
    stage, result = orch.run_capability(
        run_id,
        "badcase_retrain",
        {
            "preset": args.preset,
            "target": args.target,
            "model": args.model,
            "fe": args.fe,
            "data_dir": args.data_dir,
            "badcase_path": badcase_path,
            "badcase_ratio": args.badcase_ratio,
            "regression_tol": args.regression_tol,
            "eval_metric": args.eval_metric,
            "heldout_frac": args.heldout_frac,
            "heldout_seed": args.heldout_seed,
        },
    )
    m = result.event.metrics if result.event is not None else {}
    return {
        "n_badcase": n_badcase,
        "metrics": m,
        "gate_result": result.gate_result.value,
        "detail": result.detail,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="B 飞轮型端到端 demo（真实 Titanic 数据）")
    ap.add_argument("--preset", default="titanic")
    ap.add_argument("--target", default=None)
    ap.add_argument("--model", default="gbm")
    ap.add_argument("--fe", default="basic", choices=["basic", "rich"])
    ap.add_argument("--data-dir", default=_DEFAULT_DATA_DIR)
    ap.add_argument("--eval-metric", default="accuracy")
    ap.add_argument("--badcase-ratio", type=float, default=0.3)
    ap.add_argument("--regression-tol", type=float, default=0.0)
    ap.add_argument("--heldout-frac", type=float, default=0.3)
    ap.add_argument("--heldout-seed", type=int, default=42)
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()

    svc = ControlPlaneService()
    orch = ClosedLoopOrchestrator(svc)
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="flywheel-demo",
            run_type=RunType.FLYWHEEL,
            entry_stage="badcase_retrain",
            target_id=f"flywheel-{args.preset}",
            objective_snapshot={"name": f"飞轮 demo · {args.preset}", "eval_metric": args.eval_metric},
        )
    )
    svc.start_workflow_run(run.run_id)

    print("=" * 78)
    print(f"B 飞轮型 demo  ·  run={run.run_id}  ·  preset={args.preset}  ·  model={args.model}")
    print(f"badcase_ratio={args.badcase_ratio}  ·  regression_tol={args.regression_tol}  ·  "
          f"eval_metric={args.eval_metric}")
    print("=" * 78)

    for rnd in range(1, args.rounds + 1):
        out = _run_one_round(orch, run.run_id, args)
        m = out["metrics"]
        verdict = "ACCEPT" if out["gate_result"] == "passed" else "REJECT"
        reg = "通过" if m.get("regression_passed") == 1.0 else "退化"
        bc_imp = "提升" if m.get("badcase_improved") == 1.0 else "未提升"
        print(f"\n[第 {rnd} 轮]  坏例数 = {out['n_badcase']}  ·  裁决 = {verdict}")
        print(f"  坏例召回 (badcase acc) : {_fmt(m.get('baseline_badcase_acc'))} → "
              f"{_fmt(m.get('retrained_badcase_acc'))}  ({bc_imp})")
        print(f"  回归指标 ({args.eval_metric})       : {_fmt(m.get('baseline_primary'))} → "
              f"{_fmt(m.get('retrained_primary'))}  （回归门 {reg}）")
        print(f"  detail: {out['detail']}")

    print("\n" + "=" * 78)
    print("飞轮一轮完成。事件已写入 run 的事件日志，可经前端「🔄 数据飞轮」或")
    print(f"  GET /workflow-runs/{run.run_id}/flywheel  查看迭代历史。")
    print("=" * 78)


if __name__ == "__main__":
    main()
