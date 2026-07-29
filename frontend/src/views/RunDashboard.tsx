import React, { useEffect, useState } from "react";
import {
  getAgentTrace,
  getEvents,
  getRun,
  AgentTraceStep,
  RunDetail,
} from "../api/client";
import { DualLoopLive } from "./DualLoopLive";
import { AuditBoard } from "./AuditBoard";
import { HypothesisTree } from "./HypothesisTree";
import { ImprovementTimeline } from "./ImprovementTimeline";
import { ApprovalConsole } from "./ApprovalConsole";
import { EventStream } from "./EventStream";

type Sub = "live" | "audit" | "tree" | "improve" | "approval" | "events" | "agent";

const SUBS: Array<{ id: Sub; label: string }> = [
  { id: "live", label: "总览" },
  { id: "audit", label: "审计结论" },
  { id: "tree", label: "假设树" },
  { id: "improve", label: "改进时间线" },
  { id: "approval", label: "审批台" },
  { id: "agent", label: "Agent 执行流水" },
  { id: "events", label: "事件" },
];

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  requested: "已请求",
  waiting_approval: "等待审批",
  succeeded: "成功",
  failed: "失败",
  exited_budget: "已完成 · 预算耗尽",
  exited_converged: "已完成 · 已收敛",
  cancelled: "已取消",
};
const STATUS_CLASS: Record<string, string> = {
  running: "warn",
  requested: "accent",
  waiting_approval: "accent",
  succeeded: "ok",
  failed: "bad",
  exited_budget: "ok",
  exited_converged: "ok",
  cancelled: "bad",
};

const DECISION_LABEL: Record<string, string> = {
  accept: "ACCEPT · 接受",
  revisit: "REFINE · 复核",
  restart: "RESTART · 重启",
  exit_success: "EXIT_SUCCESS",
};
const DECISION_CLASS: Record<string, string> = {
  accept: "ok",
  revisit: "warn",
  restart: "bad",
  exit_success: "ok",
};

export function RunDashboard({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [sub, setSub] = useState<Sub>("live");
  const [trace, setTrace] = useState<AgentTraceStep[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [r, e] = await Promise.all([getRun(runId), getEvents(runId)]);
        if (!alive) return;
        setRun(r);
        setEvents(e);
      } catch {
        /* transient */
      }
    };
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  // Agent execution trace ("Agent 执行流水"): pull while the tab is active and refresh
  // periodically so the flow grows live as the inner-loop agent works.
  useEffect(() => {
    if (sub !== "agent") return;
    let alive = true;
    const loadTrace = async () => {
      if (!alive) return;
      setTraceLoading(true);
      try {
        const steps = await getAgentTrace(runId);
        if (alive) setTrace(steps);
      } catch {
        /* transient */
      } finally {
        if (alive) setTraceLoading(false);
      }
    };
    loadTrace();
    const t = setInterval(loadTrace, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [sub, runId]);

  const obj = run?.objective_snapshot || {};
  const cfg = obj.config || {};
  const il: any = cfg.inner_loop || {};
  const runStatus: string = run?.status ?? "—";
  const maxOuter: number = (cfg.max_outer_iters as number) || 3;
  const auditCount = events.filter((e) => e.event_type === "audit_completed").length;
  const decisions = events.filter((e) => e.event_type === "decision_issued");
  const lastDecision: string | null = decisions.length
    ? decisions[decisions.length - 1].decision_type
    : null;

  const innerSummary = il.mode === "agent"
    ? `agent · cli=${il.agent_cli || cfg.agent_cli || "auto"} · skills=${(il.skills || []).length} tools=${(il.tools || []).length} steps=${(il.step_plan || []).length}`
    : `scripted · model=${il.model || cfg.model || "gbm"} fe=${il.fe || "basic"} cv=${il.cv_folds ?? 5}` +
      (il.threshold != null ? ` thr=${il.threshold}` : "");

  return (
    <div className="run-dashboard">
      <div className="card status-header">
        <div className="row" style={{ gap: 14, alignItems: "center", flexWrap: "wrap" }}>
          <span className={`pill ${STATUS_CLASS[runStatus] || "accent"}`}>
            {STATUS_LABEL[runStatus] || runStatus}
          </span>
          <strong>{obj.name || run?.target_id || runId}</strong>
          <span className="muted mono small">{runId}</span>
        </div>

        <div className="row status-meta" style={{ gap: 22, flexWrap: "wrap" }}>
          <span>
            <span className="muted">外部审计进度　</span>
            <b>{auditCount} / {maxOuter}</b> 轮
          </span>
          <span>
            <span className="muted">最新裁决 (router)　</span>
            <span className={`pill ${DECISION_CLASS[lastDecision || ""] || "accent"}`}>
              {lastDecision ? DECISION_LABEL[lastDecision] : "待审计"}
            </span>
          </span>
          <span>
            <span className="muted">研究设定　</span>
            <span className="mono small">
              内循环={innerSummary} · 审计严格度={cfg.audit_threshold ?? 0.8} · 预算={maxOuter}轮
            </span>
          </span>
        </div>

        {runStatus !== "running" && auditCount >= maxOuter && (
          <div className="muted" style={{ marginTop: 8 }}>
            双循环已结束：跑满 {maxOuter} 轮外部审计，外循环均未给出 ACCEPT（持续 REFINE），以「预算耗尽」终态退出。
          </div>
        )}

        {runStatus === "failed" && run?.status_detail && (
          <div className="card error" style={{ marginTop: 10 }}>
            <strong>运行失败</strong>
            <p style={{ marginTop: 6, marginBottom: 0 }}>{run.status_detail}</p>
          </div>
        )}
      </div>

      <div className="tabs sub">
        {SUBS.map((s) => (
          <button
            key={s.id}
            className={`tab ${sub === s.id ? "active" : ""}`}
            onClick={() => setSub(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {sub === "live" && <DualLoopLive runId={runId} />}
      {sub === "audit" && <AuditBoard runId={runId} />}
      {sub === "tree" && <HypothesisTree runId={runId} />}
      {sub === "improve" && <ImprovementTimeline runId={runId} />}
      {sub === "approval" && <ApprovalConsole runId={runId} />}
      {sub === "agent" && (
        <AgentTracePanel steps={trace} loading={traceLoading} runStatus={runStatus} />
      )}
      {sub === "events" && <EventStream runId={runId} />}
    </div>
  );
}

/** "Agent 执行流水" panel: renders the inner-loop agent's step-by-step execution. */
function AgentTracePanel({
  steps,
  loading,
  runStatus,
}: {
  steps: AgentTraceStep[];
  loading: boolean;
  runStatus: string;
}) {
  if (loading && steps.length === 0) {
    return <div className="card" style={{ marginTop: 12 }}>加载 Agent 执行流水…</div>;
  }
  if (steps.length === 0) {
    return (
      <div className="card" style={{ marginTop: 12 }}>
        <p className="muted">
          该 run 暂无 Agent 执行流水记录。仅当内循环以<b>自主 Agent 模式</b>运行时，每次工具调用与最终答案才会
          被记录到这里（脚本化双循环 run 不产出此流水）。
        </p>
        {runStatus === "running" && (
          <p className="muted small">运行进行中：若已选择 agent 模式，流水会随 agent 工作逐步出现。</p>
        )}
      </div>
    );
  }
  return (
    <div className="card agent-trace" style={{ marginTop: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>Agent 执行流水</h3>
        <span className="muted small">{steps.length} 步</span>
      </div>
      <div className="trace-list">
        {steps.map((s) => (
          <TraceStep key={s.seq} step={s} />
        ))}
      </div>
    </div>
  );
}

/** A single agent step with a collapsible detail (详情/收起). */
function TraceStep({ step }: { step: AgentTraceStep }) {
  const [open, setOpen] = useState(false);
  const isFinal = step.kind === "final";
  return (
    <div className={`trace-step ${isFinal ? "final" : "tool"}`}>
      <div className="trace-head">
        <span className="trace-seq">#{step.seq}</span>
        <span className={`pill ${isFinal ? "ok" : "accent"}`}>
          {isFinal ? "最终答案" : "工具调用"}
        </span>
        {step.tool && <span className="mono trace-tool">{step.tool}</span>}
        <button type="button" className="btn tiny trace-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? "收起详情 ▲" : "展开详情 ▼"}
        </button>
      </div>
      {!isFinal && step.args_summary && (
        <div className="trace-line">
          <span className="muted">输入　</span>
          <code>{step.args_summary}</code>
        </div>
      )}
      {step.result_summary && (
        <div className="trace-line">
          <span className="muted">{isFinal ? "输出　" : "结果　"}</span>
          <code>{step.result_summary}</code>
        </div>
      )}
      {open && step.detail && (
        <pre className="trace-detail">{step.detail}</pre>
      )}
    </div>
  );
}
