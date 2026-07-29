import React, { useEffect, useState } from "react";
import { getAudit, getImprovements, getEvents, getRun } from "../api/client";

/** Live dual-loop overview: inner-loop events vs outer-loop audit + improvement. */
export function DualLoopLive({ runId }: { runId: string }) {
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [audit, setAudit] = useState<any[]>([]);
  const [improve, setImprove] = useState<any[]>([]);
  const [run, setRun] = useState<any>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [e, a, i, r] = await Promise.all([
          getEvents(runId),
          getAudit(runId),
          getImprovements(runId),
          getRun(runId),
        ]);
        if (!alive) return;
        setEvents(e);
        setAudit(a);
        setImprove(i);
        setRun(r);
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for DualLoopLive:", err);
      }
    };
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  const innerCount = events.filter((e) =>
    ["eval_completed", "artifact_published", "stage_started"].includes(e.event_type)
  ).length;

  const runStatus: string = run?.status ?? "—";
  const maxOuter: number = (run?.objective_snapshot?.config?.max_outer_iters as number) || 3;
  const outerDone = audit.length;
  const decisions = events.filter((e) => e.event_type === "decision_issued");
  const lastDecision: string | null = decisions.length
    ? decisions[decisions.length - 1].decision_type
    : null;
  const decisionLabel =
    lastDecision === "accept" ? "ACCEPT · 接受"
    : lastDecision === "revisit" ? "REFINE · 复核"
    : lastDecision === "restart" ? "RESTART · 重启"
    : lastDecision === "exit_success" ? "EXIT_SUCCESS"
    : lastDecision ?? "—";
  const decisionClass =
    lastDecision === "accept" ? "ok"
    : lastDecision === "revisit" ? "warn"
    : lastDecision === "restart" ? "bad"
    : "accent";
  const statusLabel: Record<string, string> = {
    running: "运行中",
    requested: "已请求",
    waiting_approval: "等待审批",
    succeeded: "成功",
    failed: "失败",
    exited_budget: "已完成 · 预算耗尽",
    exited_converged: "已完成 · 已收敛",
    cancelled: "已取消",
  };
  const statusClass: Record<string, string> = {
    running: "warn",
    requested: "accent",
    waiting_approval: "accent",
    succeeded: "ok",
    failed: "bad",
    exited_budget: "ok",
    exited_converged: "ok",
    cancelled: "bad",
  };

  return (
    <div>
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="row" style={{ gap: 16, alignItems: "center", flexWrap: "wrap" }}>
          <span className={`pill ${statusClass[runStatus] || "accent"}`}>
            {statusLabel[runStatus] || runStatus}
          </span>
          <span className="muted">外部审计进度</span>
          <span>
            {outerDone} / {maxOuter} 轮
          </span>
          <span className="muted">最新裁决 (router)</span>
          <span className={`pill ${decisionClass}`}>{decisionLabel}</span>
        </div>
        {runStatus !== "running" && outerDone >= maxOuter && (
          <div className="muted" style={{ marginTop: 8 }}>
            双循环已结束：跑满 {maxOuter} 轮外部审计，外循环均未给出 ACCEPT（持续 REFINE），以「预算耗尽」终态退出。
          </div>
        )}
      </div>

      <div className="grid2">
        <div className="card">
          <h2>内循环 · 内部研究</h2>
          <div className="kv">
            <span className="muted">事件数</span>
            <span>{innerCount}</span>
          </div>
          <div className="kv">
            <span className="muted">最新评测</span>
            <span className="mono">
              {(() => {
                const ev = events.filter((e) => e.event_type === "eval_completed").pop();
                if (!ev) return "—";
                const metrics = ev.metrics ?? {};
                const parts = Object.entries(metrics).map(([k, v]) => `${k}=${v}`);
                return `${parts.length ? parts.join(" ") + " " : ""}gate=${ev.gate_passed}`;
              })()}
            </span>
          </div>
          <h3 style={{ marginTop: 12 }}>内循环轨迹</h3>
          <ul className="tight mono">
            {events
              .filter((e) =>
                ["eval_completed", "stage_started", "artifact_published", "gate_passed"].includes(
                  e.event_type
                )
              )
              .slice(-8)
              .map((e, i) => (
                <li key={i}>{e.event_type}</li>
              ))}
          </ul>
        </div>

        <div className="card">
          <h2>外循环 · 审计 + 递归改进</h2>
          <div className="kv">
            <span className="muted">审计次数</span>
            <span>{audit.length}</span>
          </div>
          <div className="kv">
            <span className="muted">最新裁决 (router)</span>
            <span className={`pill ${decisionClass}`}>{decisionLabel}</span>
          </div>
          <div className="kv">
            <span className="muted">审计置信度 s</span>
            <span>{audit.length ? audit[audit.length - 1].confidence : "—"}</span>
          </div>
          <div className="kv">
            <span className="muted">改进提案</span>
            <span>{improve.length}</span>
          </div>
          <h3 style={{ marginTop: 12 }}>外循环轨迹</h3>
          <div style={{ marginBottom: 8 }}>
            <span className="muted">审计事件</span>
            <ul className="tight mono">
              {audit.map((a, i) => (
                <li key={i}>{a.audit_id} gate={String(a.gate_passed)}</li>
              ))}
            </ul>
          </div>
          <div>
            <span className="muted">改进事件</span>
            <ul className="tight mono">
              {improve.map((it, i) => (
                <li key={i}>{it.improvement_id} → {it.target_mechanism}</li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
