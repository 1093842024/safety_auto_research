import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  getAgentTrace,
  getEvents,
  getRun,
  cancelRun,
  debugRun,
  runExperiment,
  resolveCollaboration,
  AgentTraceStep,
  CollaborationAdjustments,
  DebugResult,
  RunDetail,
  STATUS_LABEL,
  STATUS_CLASS,
  DECISION_LABEL,
  DECISION_CLASS,
} from "../api/client";
import { useSSE } from "../api/useSSE";
import { useToast } from "../components/Toast";
import { DualLoopLive } from "./DualLoopLive";
import { AuditBoard } from "./AuditBoard";
import { HypothesisTree } from "./HypothesisTree";
import { ImprovementTimeline } from "./ImprovementTimeline";
import { ApprovalConsole } from "./ApprovalConsole";
import { EventStream } from "./EventStream";
import { EvolutionPanel } from "./EvolutionPanel";

type Sub = "live" | "debug" | "audit" | "tree" | "improve" | "approval" | "events" | "agent" | "evolution";

const SUBS: Array<{ id: Sub; label: string }> = [
  { id: "live", label: "总览" },
  { id: "debug", label: "调试" },
  { id: "audit", label: "审计结论" },
  { id: "tree", label: "假设树" },
  { id: "improve", label: "改进时间线" },
  { id: "evolution", label: "进化观察" },
  { id: "approval", label: "审批台" },
  { id: "agent", label: "Agent 执行流水" },
  { id: "events", label: "事件" },
];

export function RunDashboard({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [sub, setSub] = useState<Sub>("live");
  const [trace, setTrace] = useState<AgentTraceStep[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [pollError, setPollError] = useState("");
  const { push } = useToast();
  const [cancelBusy, setCancelBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [r, e] = await Promise.all([getRun(runId), getEvents(runId)]);
        if (!alive) return;
        setRun(r);
        setEvents(e);
        setPollError("");
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for RunDashboard:", err);
        if (alive) setPollError("数据刷新失败，显示的是缓存数据。请检查后端是否运行。");
      }
    };
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  // SSE real-time progress → trigger immediate re-fetch when an event arrives.
  const { connected: sseConnected, latestEvent } = useSSE(runId);
  const latestEventRef = useRef(latestEvent);
  latestEventRef.current = latestEvent;
  const fetchNow = useCallback(async () => {
    try {
      const [r, e] = await Promise.all([getRun(runId), getEvents(runId)]);
      setRun(r);
      setEvents(e);
      setPollError("");
    } catch (err: any) {
      setPollError("数据刷新失败，显示的是缓存数据。请检查后端是否运行。");
    }
  }, [runId]);
  useEffect(() => {
    if (latestEvent) {
      fetchNow();
    }
  }, [latestEvent, fetchNow]);

  // Cancel a running run (wired to POST /workflow-runs/{id}/cancel). The backend
  // honors the cancel event at the next loop checkpoint.
  const handleCancel = useCallback(async () => {
    setCancelBusy(true);
    try {
      await cancelRun(runId);
      push("已发送取消请求，运行将在下一个检查点停止。", "warn");
      fetchNow();
    } catch (e: any) {
      push(`取消失败：${e?.message || e}`, "error");
    } finally {
      setCancelBusy(false);
    }
  }, [runId, fetchNow, push]);

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
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for RunDashboard:", err);
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
  const collabMode: string = (il as any).collaboration_mode || (cfg as any).collaboration_mode || "autonomous";
  const collabLabel: string = ({ autonomous: "完全自主", step_confirm: "每步确认", outer_confirm: "外循环确认" } as Record<string, string>)[collabMode] || collabMode;

  // ---- Debug state ----
  const [debugBusy, setDebugBusy] = useState<"inner" | "outer" | null>(null);
  const [debugResults, setDebugResults] = useState<DebugResult[]>([]);

  const handleDebug = async (stage: "inner" | "outer") => {
    setDebugBusy(stage);
    try {
      await debugRun(runId, stage);
    } catch (e: any) {
      console.error("debug error", e);
    }
    setDebugBusy(null);
  };

  // Poll debug results from events
  useEffect(() => {
    if (sub !== "debug") return;
    const dbg = events
      .filter((e) => e.event_type === "debug_result")
      .map((e) => ({
        stage: e.stage as "inner" | "outer",
        ok: e.ok as boolean,
        summary: e.summary as string,
        metrics: e.metrics as Record<string, number> | undefined,
        verdict: e.verdict as DebugResult["verdict"],
        report_ref: e.report_ref as string | null | undefined,
        detail: e.detail as string | null | undefined,
        error: e.error as string | null | undefined,
      }));
    setDebugResults(dbg);
  }, [sub, events]);

  // ---- Experiment start ----
  const [expBusy, setExpBusy] = useState(false);
  const handleStartExperiment = async () => {
    setExpBusy(true);
    try {
      await runExperiment(runId);
    } catch (e: any) {
      console.error("start experiment error", e);
    }
    setExpBusy(false);
  };

  // ---- Collaboration state ----
  const [collabLoading, setCollabLoading] = useState(false);
  const collabCtx = collabMode !== "autonomous" && runStatus === "waiting_approval"
    ? events.find((e) => e.event_type === "approval_required" && e.subject_type === "collaboration")
    : null;
  const [collabAdj, setCollabAdj] = useState<CollaborationAdjustments>({});
  const handleCollabResolve = async (resolution: "approved" | "rejected") => {
    setCollabLoading(true);
    try {
      await resolveCollaboration(runId, { resolution, adjustments: collabAdj });
    } catch (e: any) {
      console.error("resolve collab error", e);
    }
    setCollabLoading(false);
  };

  return (
    <div className="run-dashboard">
      {pollError && (
        <div className="card" style={{ borderColor: "var(--warn)", background: "var(--warn-soft)", color: "var(--warn)", marginBottom: 12 }}>
          ⚠ {pollError}
        </div>
      )}
      <div className="card status-header">
        <div className="row" style={{ gap: 14, alignItems: "center", flexWrap: "wrap" }}>
          <span className={`pill ${STATUS_CLASS[runStatus] || "accent"}`}>
            {STATUS_LABEL[runStatus] || runStatus}
          </span>
          <strong>{obj.name || run?.target_id || runId}</strong>
          <span className="muted mono small">{runId}</span>
          <span className={`dot ${sseConnected ? "ok" : "bad"}`} title={sseConnected ? "实时推送已连接" : "实时推送断开，使用轮询"} style={{ width: 7, height: 7, marginLeft: -6 }} />
          {runStatus === "running" && (
            <button className="btn" disabled={cancelBusy} onClick={handleCancel} style={{ marginLeft: "auto" }}>
              {cancelBusy ? "取消中…" : "✕ 取消运行"}
            </button>
          )}
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
              内循环={innerSummary} · 审计严格度={cfg.audit_threshold ?? 0.8} · 预算={maxOuter}轮 · 协作={collabLabel}
            </span>
          </span>
        </div>

        {/* Experiment-start button for requested runs */}
        {runStatus === "requested" && (
          <div className="card note" style={{ marginTop: 12 }}>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <strong>该研究任务已创建，尚未启动完整实验</strong>
                <p className="muted" style={{ marginTop: 4, marginBottom: 0 }}>
                  可先在「调试」页验证内/外循环，确认无误后再启动完整自主实验。
                </p>
              </div>
              <button className="btn primary" disabled={expBusy} onClick={handleStartExperiment}>
                {expBusy ? "启动中…" : "开始完整自主实验 →"}
              </button>
            </div>
          </div>
        )}

        {/* Collaboration confirmation panel */}
        {collabCtx && runStatus === "waiting_approval" && (
          <div className="card" style={{ marginTop: 12, border: "2px solid var(--accent)" }}>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <strong>⚡ 协作暂停 — 等待确认</strong>
              <span className="pill warn">{collabLabel}</span>
            </div>
            <p className="muted" style={{ marginTop: 6 }}>
              当前步骤已暂停，请审阅运行状态后选择「继续」或「中止」。
            </p>
            <div className="collab-form" style={{ marginTop: 10 }}>
              <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                <label className="field" style={{ margin: 0 }}>
                  <span>模型　</span>
                  <select value={collabAdj.model || ""} onChange={(e) => setCollabAdj({ ...collabAdj, model: e.target.value || null })} style={{ width: 120 }}>
                    <option value="">不变</option>
                    {["gbm", "gbm-strong", "rf", "logreg"].map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </label>
                <label className="field" style={{ margin: 0 }}>
                  <span>FE　</span>
                  <select value={collabAdj.fe || ""} onChange={(e) => setCollabAdj({ ...collabAdj, fe: (e.target.value || null) as any })} style={{ width: 100 }}>
                    <option value="">不变</option>
                    <option value="basic">basic</option>
                    <option value="rich">rich</option>
                  </select>
                </label>
                <label className="field" style={{ margin: 0 }}>
                  <span>CV　　　</span>
                  <input type="number" min={1} max={20} value={collabAdj.cv_folds ?? ""} placeholder="不变" onChange={(e) => setCollabAdj({ ...collabAdj, cv_folds: e.target.value ? Number(e.target.value) : null })} style={{ width: 70 }} />
                </label>
                <label className="field" style={{ margin: 0 }}>
                  <span>门槛　</span>
                  <input type="number" step={0.01} value={collabAdj.threshold ?? ""} placeholder="不变" onChange={(e) => setCollabAdj({ ...collabAdj, threshold: e.target.value ? Number(e.target.value) : null })} style={{ width: 80 }} />
                </label>
                <label className="field" style={{ margin: 0 }}>
                  <span>审计严格度</span>
                  <input type="number" step={0.05} min={0} max={1} value={collabAdj.audit_threshold ?? ""} placeholder="不变" onChange={(e) => setCollabAdj({ ...collabAdj, audit_threshold: e.target.value ? Number(e.target.value) : null })} style={{ width: 80 }} />
                </label>
              </div>
              <div className="row" style={{ gap: 8, marginTop: 8 }}>
                <button className="btn primary" disabled={collabLoading} onClick={() => handleCollabResolve("approved")}>
                  {collabLoading ? "确认中…" : "✓ 继续"}
                </button>
                <button className="btn" disabled={collabLoading} onClick={() => handleCollabResolve("rejected")}>
                  ✕ 中止
                </button>
              </div>
              <p className="muted small" style={{ marginTop: 6 }}>
                上方字段留空 = 保持原设定不变。填写的调整将在下一轮内循环中生效。
              </p>
            </div>
          </div>
        )}

        {runStatus !== "running" && auditCount >= maxOuter && (
          <div className="muted" style={{ marginTop: 8 }}>
            双循环已结束：已完成 {maxOuter} 轮外部审计，外循环未给出 ACCEPT（持续 REFINE），以「已达最大轮数」终态退出。
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
      {sub === "debug" && (
        <DebugPanel
          runStatus={runStatus}
          debugBusy={debugBusy}
          debugResults={debugResults}
          onDebug={handleDebug}
          onStartExperiment={handleStartExperiment}
          expBusy={expBusy}
        />
      )}
      {sub === "audit" && (
        events.filter(e => e.event_type === "audit_completed").length === 0
          ? <div className="card muted" style={{ textAlign: "center", padding: 40, marginTop: 12 }}>暂无外循环审计记录。研究启动后，每轮外循环会在此展示约束检查结果与置信度。</div>
          : <AuditBoard runId={runId} />
      )}
      {sub === "tree" && (
        events.filter(e => e.event_type === "hypothesis_observed").length === 0
          ? <div className="card muted" style={{ textAlign: "center", padding: 40, marginTop: 12 }}>暂无假设树节点。每次内循环产生研究结果时，会在此记录一条假设及其证据。</div>
          : <HypothesisTree runId={runId} />
      )}
      {sub === "improve" && (
        events.filter(e => e.event_type === "improvement_applied").length === 0
          ? <div className="card muted" style={{ textAlign: "center", padding: 40, marginTop: 12 }}>暂无改进时间线。元循环（layer_09）在启用后会在此记录每次改进前后的指标对比。</div>
          : <ImprovementTimeline runId={runId} />
      )}
      {sub === "approval" && <ApprovalConsole runId={runId} />}
      {sub === "evolution" && (
        <EvolutionPanel
          runId={runId}
          scope={String(run?.objective_snapshot?.benchmark_task_id || run?.target_id || "")}
        />
      )}
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

// ---------------------------------------------------------------------------
// Debug panel: isolate inner / outer loop stages before the full experiment
// ---------------------------------------------------------------------------

function DebugPanel({
  runStatus,
  debugBusy,
  debugResults,
  onDebug,
  onStartExperiment,
  expBusy,
}: {
  runStatus: string;
  debugBusy: "inner" | "outer" | null;
  debugResults: DebugResult[];
  onDebug: (stage: "inner" | "outer") => void;
  onStartExperiment: () => void;
  expBusy: boolean;
}) {
  const innerLatest = debugResults.filter((r) => r.stage === "inner").slice(-1)[0];
  const outerLatest = debugResults.filter((r) => r.stage === "outer").slice(-1)[0];

  return (
    <div className="card debug-panel" style={{ marginTop: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>⚙ 调试面板</h3>
        <span className="muted small">
          {debugResults.length > 0 ? `${debugResults.length} 条调试记录` : "尚未调试"}
        </span>
      </div>
      <p className="muted" style={{ marginBottom: 16 }}>
        在启动完整实验前，可对双循环的关键环节进行<b>单独调试</b>：验证数据加载、模型训练、审计阈值是否合理。
        调试结果不会污染研究记录与榜单。
      </p>

      <div className="grid2" style={{ gap: 12 }}>
        {/* Inner loop debug */}
        <div className="debug-card">
          <h4>内循环调试</h4>
          <p className="muted small">运行一次 kaggle_eval（或 agent 内循环），验证数据、模型与指标。</p>
          <button
            className="btn primary"
            disabled={debugBusy !== null}
            onClick={() => onDebug("inner")}
          >
            {debugBusy === "inner" ? "运行中…" : "▶ 运行内循环调试"}
          </button>
          {innerLatest && <DebugResultCard r={innerLatest} />}
        </div>

        {/* Outer loop debug */}
        <div className="debug-card">
          <h4>外循环调试</h4>
          <p className="muted small">对最近一次内循环结果运行外审计（layer_11），验证审计阈值与结论。</p>
          <button
            className="btn"
            disabled={debugBusy !== null || !innerLatest}
            onClick={() => onDebug("outer")}
          >
            {debugBusy === "outer" ? "运行中…" : "▶ 运行外循环调试"}
          </button>
          {!innerLatest && <p className="muted small">请先运行内循环调试，再调试外循环。</p>}
          {outerLatest && <DebugResultCard r={outerLatest} />}
        </div>
      </div>

      {runStatus === "requested" && (
        <div className="card note" style={{ marginTop: 16 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>调试通过？</strong>
              <span className="muted" style={{ marginLeft: 8 }}>可开始完整的自主实验。</span>
            </div>
            <button className="btn primary" disabled={expBusy} onClick={onStartExperiment}>
              {expBusy ? "启动中…" : "开始完整自主实验 →"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function DebugResultCard({ r }: { r: DebugResult }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`debug-result ${r.ok ? "ok" : "fail"}`} style={{ marginTop: 10 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span>
          <span className={`pill ${r.ok ? "ok" : "bad"}`}>{r.ok ? "✓ 通过" : "✕ 失败"}</span>
          <span className="muted small" style={{ marginLeft: 8 }}>{r.summary}</span>
        </span>
        <button type="button" className="btn tiny" onClick={() => setOpen((v) => !v)}>
          {open ? "收起" : "详情"}
        </button>
      </div>
      {open && (
        <div style={{ marginTop: 8 }}>
          {r.error && <p className="muted" style={{ color: "var(--red)" }}>错误: {r.error}</p>}
          {r.metrics && Object.keys(r.metrics).length > 0 && (
            <div>
              <span className="muted small">指标：</span>
              {Object.entries(r.metrics).map(([k, v]) => (
                <span key={k} className="pill accent" style={{ marginRight: 4 }}>{k}={typeof v === "number" ? v.toFixed(4) : String(v)}</span>
              ))}
            </div>
          )}
          {r.verdict && (
            <div>
              <span className="muted small">审计结论：confidence={r.verdict.confidence}, recoverable={String(r.verdict.recoverable)}, gate_passed={String(r.verdict.gate_passed)}</span>
              {r.verdict.unresolved?.length > 0 && <span className="muted small">, unresolved={r.verdict.unresolved.join(", ")}</span>}
            </div>
          )}
          {r.detail && <pre className="trace-detail">{r.detail}</pre>}
        </div>
      )}
    </div>
  );
}
