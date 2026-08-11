import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getPlaybook,
  getStrategies,
  getEvolution,
  launchProgramEvolution,
  getRun,
  cancelRun,
  PlaybookEntry,
  StrategyEntry,
  EvolutionCandidate,
  ProgramEvolutionConfig,
  TERMINAL_RUN_STATUSES,
} from "../api/client";

interface Props {
  runId: string;
  /** Playbook scope: benchmark task id (falls back to run target). */
  scope: string;
}

type CandView = "table" | "island";

const SECTION_LABEL: Record<string, string> = {
  strategy: "策略",
  failure: "避坑",
  fact: "事实",
};

const STATUS_LABEL: Record<string, string> = {
  pending_verification: "待验证",
  verified: "已验证 ✓",
  rolled_back: "已回滚 ✗",
  expired_unverified: "过期(未验证)",
  expired_unapplied: "过期(未应用)",
  expired_superseded: "被人工调整取代",
  rejected: "已拒绝",
  no_op: "无需变更",
};

// Distinct, dark-theme-friendly hues for island colouring (island 0..n).
const ISLAND_COLORS = [
  "#5b8bff", "#46c281", "#e0a63a", "#f0696b", "#b07cf0",
  "#37c2c8", "#ef7dad", "#9bd45a", "#ff9f43", "#7a8cff",
];

/** Parse an island index from a branch tag: ``gen2.isl3`` -> 3, ``gen0`` -> 0. */
function islandIndex(branch: string): number {
  const m = /isl(\d+)/.exec(branch || "");
  return m ? parseInt(m[1], 10) : 0;
}

function islandColor(idx: number): string {
  return ISLAND_COLORS[idx % ISLAND_COLORS.length];
}

/** Run statuses that mean the program-evolution loop has stopped (poll should end).
 *  Single source of truth now lives in ``api/client`` (shared with RunDashboard). */
const TERMINAL_STATUSES = TERMINAL_RUN_STATUSES;

function truncate(s: string, n = 60): string {
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/** Coerce a raw input string to an int clamped to [min,max], falling back to `def`. */
function clampInt(raw: string, min: number, max: number, def: number): number {
  const v = parseInt(raw, 10);
  if (Number.isNaN(v)) return def;
  return Math.max(min, Math.min(max, v));
}

/** Coerce a raw input string to a float clamped to [min,max], falling back to `def`. */
function clampNum(raw: string, min: number, max: number, def: number): number {
  const v = parseFloat(raw);
  if (Number.isNaN(v)) return def;
  return Math.max(min, Math.min(max, v));
}

/** Phase 1-3 observability: ACE playbook + strategy lifecycle + evolution population. */
export function EvolutionPanel({ runId, scope }: Props) {
  const [playbook, setPlaybook] = useState<PlaybookEntry[]>([]);
  const [strategies, setStrategies] = useState<StrategyEntry[]>([]);
  const [candidates, setCandidates] = useState<EvolutionCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [view, setView] = useState<CandView>("table");
  const [programOnly, setProgramOnly] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [pb, st, evo] = await Promise.allSettled([
        getPlaybook(scope || undefined),
        getStrategies(runId),
        getEvolution(runId),
      ]);
      if (pb.status === "fulfilled") setPlaybook(pb.value);
      if (st.status === "fulfilled") setStrategies(st.value);
      if (evo.status === "fulfilled") setCandidates(evo.value);
      // R17 fix: the old guard required BOTH playbook and strategies to fail
      // before saying anything, so a single broken endpoint silently rendered
      // stale data as if it were fresh. Report every failed source by name.
      const failures: string[] = [];
      if (pb.status === "rejected") failures.push("Playbook");
      if (st.status === "rejected") failures.push("策略");
      if (evo.status === "rejected") failures.push("进化种群");
      setError(
        failures.length > 0
          ? `部分数据加载失败：${failures.join("、")}（后端未启动或端点不可用）；下方为上次成功获取的数据`
          : ""
      );
    } finally {
      setLoading(false);
    }
  }, [runId, scope]);

  // ---- program-evolution launch state --------------------------------------
  const [showLaunch, setShowLaunch] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [launchErr, setLaunchErr] = useState("");
  const [progStatus, setProgStatus] = useState<string>("");
  const [progCfg, setProgCfg] = useState<ProgramEvolutionConfig>({
    backend_type: "template",
    islands: 1,
    pop_per_island: 3,
    generations: 2,
    max_workers: 1,
    novelty_threshold: 0.92,
    audit: true,
    seed: 42,
  });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current != null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  /** Poll the run status + refresh candidates while a program-evolution loop runs. */
  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const run = await getRun(runId);
        setProgStatus(run.status);
        await load();
        if (TERMINAL_STATUSES.has(run.status)) {
          setLaunching(false);
          stopPolling();
        }
      } catch {
        /* transient backend hiccup during the run — keep polling */
      }
    }, 2500);
  }, [runId, load, stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const onLaunch = useCallback(async () => {
    setLaunchErr("");
    setLaunching(true);
    setProgStatus("running");
    try {
      await launchProgramEvolution(runId, progCfg);
      setShowLaunch(false);
      startPolling();
    } catch (err: any) {
      setLaunching(false);
      setLaunchErr(err?.message || "启动失败");
    }
  }, [runId, progCfg, startPolling]);

  const onStop = useCallback(async () => {
    try {
      await cancelRun(runId);
      setProgStatus("cancelled");
    } catch (err: any) {
      setLaunchErr(err?.message || "停止失败");
    }
  }, [runId]);

  useEffect(() => {
    load();
  }, [load]);

  // ---- island-view grouping -------------------------------------------------
  const islandView = useMemo(() => {
    const src = programOnly
      ? candidates.filter((c) => (c.node_kind ?? "config") === "program")
      : candidates;
    const groups = new Map<number, EvolutionCandidate[]>();
    for (const c of src) {
      const isl = islandIndex(c.branch);
      if (!groups.has(isl)) groups.set(isl, []);
      groups.get(isl)!.push(c);
    }
    const islands = [...groups.keys()].sort((a, b) => a - b);
    for (const isl of islands) {
      groups.get(isl)!.sort((a, b) => a.generation - b.generation || (b.fitness ?? -1) - (a.fitness ?? -1));
    }
    return { groups, islands };
  }, [candidates, programOnly]);

  const empty =
    playbook.length === 0 && strategies.length === 0 && candidates.length === 0;

  const showIsland = view === "island" && candidates.length > 0;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>🧬 进化观察（Harness 工程一/三期）</h3>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn ghost small" onClick={() => setShowLaunch((v) => !v)} disabled={launching}>
            {showLaunch ? "收起" : "🚀 启动程序进化"}
          </button>
          <button className="btn ghost small" onClick={load} disabled={loading || launching}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      {showLaunch && !launching && (
        <div className="card" style={{ marginTop: 8, padding: 12 }}>
          <div className="muted small" style={{ marginBottom: 8 }}>
            程序级进化（OpenRSI 岛模型）：在 <b>代码空间</b> 上跑 Draft/Improve/Debug/Crossover 四算子，后台执行，结果落入岛视图。空表单即用内置 titanic 预设。
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 10 }}>
            <label className="col">
              <span className="small muted">后端</span>
              <select
                className="input"
                value={progCfg.backend_type}
                onChange={(e) => setProgCfg((c) => ({ ...c, backend_type: e.target.value as "template" | "llm" }))}
              >
                <option value="template">template（离线确定性）</option>
                <option value="llm">llm（OpenAI 兼容 API）</option>
              </select>
            </label>
            <label className="col">
              <span className="small muted">岛数 islands (1–16)</span>
              <input className="input" type="number" min={1} max={16}
                value={progCfg.islands}
                onChange={(e) => setProgCfg((c) => ({ ...c, islands: clampInt(e.target.value, 1, 16, 1) }))} />
            </label>
            <label className="col">
              <span className="small muted">每岛候选 pop_per_island (1–32)</span>
              <input className="input" type="number" min={1} max={32}
                value={progCfg.pop_per_island}
                onChange={(e) => setProgCfg((c) => ({ ...c, pop_per_island: clampInt(e.target.value, 1, 32, 3) }))} />
            </label>
            <label className="col">
              <span className="small muted">代数 generations (1–50)</span>
              <input className="input" type="number" min={1} max={50}
                value={progCfg.generations}
                onChange={(e) => setProgCfg((c) => ({ ...c, generations: clampInt(e.target.value, 1, 50, 2) }))} />
            </label>
            <label className="col">
              <span className="small muted">并行 max_workers (1–8)</span>
              <input className="input" type="number" min={1} max={8}
                value={progCfg.max_workers}
                onChange={(e) => setProgCfg((c) => ({ ...c, max_workers: clampInt(e.target.value, 1, 8, 1) }))} />
            </label>
            <label className="col">
              <span className="small muted">新颖度阈值 (0–1)</span>
              <input className="input" type="number" min={0} max={1} step={0.01}
                value={progCfg.novelty_threshold}
                onChange={(e) => setProgCfg((c) => ({ ...c, novelty_threshold: clampNum(e.target.value, 0, 1, 0.92) }))} />
            </label>
            <label className="col">
              <span className="small muted">种子 seed</span>
              <input className="input" type="number"
                value={progCfg.seed}
                onChange={(e) => setProgCfg((c) => ({ ...c, seed: parseInt(e.target.value || "42", 10) }))} />
            </label>
            <label className="col" style={{ justifyContent: "center" }}>
              <span className="small muted">结束时审计</span>
              <input type="checkbox" checked={progCfg.audit !== false}
                onChange={(e) => setProgCfg((c) => ({ ...c, audit: e.target.checked }))} />
            </label>
          </div>
          <div className="row" style={{ gap: 6, marginTop: 10 }}>
            <button className="btn accent small" onClick={onLaunch}>确认启动</button>
            <button className="btn ghost small" onClick={() => setShowLaunch(false)}>取消</button>
          </div>
        </div>
      )}

      {launching && (
        <div className="card warn" style={{ marginTop: 8, padding: 10 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <span className="small">⏳ 程序进化运行中…（状态：{progStatus || "running"}，结果实时落入岛视图）</span>
            <button className="btn small" onClick={onStop}>停止</button>
          </div>
        </div>
      )}

      {launchErr && <div className="card error" style={{ marginTop: 8 }}>{launchErr}</div>}
      {error && <div className="card error" style={{ marginTop: 8 }}>{error}</div>}
      {empty && !loading ? (
        <div className="muted" style={{ padding: 24, textAlign: "center" }}>
          暂无进化数据。双循环每轮会沉淀 Playbook 条目与策略补丁；进化搜索会在此列出候选种群。
        </div>
      ) : (
        <>
          {playbook.length > 0 && (
            <>
              <h4 style={{ marginBottom: 6 }}>📖 Playbook（scope={scope}，{playbook.length} 条）</h4>
              <table className="lb-table">
                <thead>
                  <tr><th style={{ width: 60 }}>类型</th><th>内容</th><th style={{ width: 90 }}>+/−</th></tr>
                </thead>
                <tbody>
                  {playbook.map((e) => (
                    <tr key={e.entry_id}>
                      <td><span className={`pill small ${e.section === "failure" ? "bad" : e.section === "strategy" ? "ok" : ""}`}>{SECTION_LABEL[e.section] || e.section}</span></td>
                      <td className="small">{e.content}</td>
                      <td className="muted small">+{e.helpful}/−{e.harmful}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {strategies.length > 0 && (
            <>
              <h4 style={{ margin: "14px 0 6px" }}>🎯 策略补丁生命周期（{strategies.length} 条）</h4>
              <table className="lb-table">
                <thead>
                  <tr><th>补丁</th><th>基线</th><th>实测</th><th>Δ</th><th>状态</th></tr>
                </thead>
                <tbody>
                  {strategies.map((s) => (
                    <tr key={s.rollback_id}>
                      <td className="mono small">{JSON.stringify(s.inner_param_patch || {})}</td>
                      <td className="small">{s.prediction?.baseline ?? "—"}</td>
                      <td className="small">{s.actual_accuracy ?? "—"}</td>
                      <td className="small">{s.actual_delta ?? "—"}</td>
                      <td className="small">{STATUS_LABEL[s.status || ""] || s.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {candidates.length > 0 && (
            <>
              <div className="row" style={{ margin: "14px 0 6px", gap: 8, alignItems: "center" }}>
                <h4 style={{ margin: 0 }}>🧫 进化种群（{candidates.length} 候选）</h4>
                <div className="row" style={{ gap: 4 }}>
                  <button
                    className={`btn small ${view === "table" ? "accent" : "ghost"}`}
                    onClick={() => setView("table")}
                  >种群表</button>
                  <button
                    className={`btn small ${view === "island" ? "accent" : "ghost"}`}
                    onClick={() => setView("island")}
                  >🏝️ 岛视图</button>
                </div>
              </div>

              {showIsland ? (
                <>
                  <div className="row" style={{ gap: 10, flexWrap: "wrap", marginBottom: 8 }}>
                    {islandView.islands.map((isl) => (
                      <span key={isl} className="pill small" style={{ borderColor: islandColor(isl), color: islandColor(isl) }}>
                        🏝️ 岛 {isl} · {islandView.groups.get(isl)!.length}
                      </span>
                    ))}
                    <label className="row" style={{ gap: 4, marginLeft: "auto", cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={programOnly}
                        onChange={(e) => setProgramOnly(e.target.checked)}
                      />
                      <span className="small muted">仅程序级（岛模型）</span>
                    </label>
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 10 }}>
                    {islandView.islands.map((isl) => (
                      <div
                        key={isl}
                        className="card"
                        style={{ borderLeft: `4px solid ${islandColor(isl)}`, margin: 0, padding: 10 }}
                      >
                        <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                          <strong style={{ color: islandColor(isl) }}>🏝️ 岛 {isl}</strong>
                          <span className="muted small">gen {islandView.groups.get(isl)![0]?.generation ?? "?"}–{islandView.groups.get(isl)![islandView.groups.get(isl)!.length - 1]?.generation ?? "?"}</span>
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                          {islandView.groups.get(isl)!.map((c) => (
                            <div key={c.candidate_id} className="card" style={{ margin: 0, padding: 6, background: "var(--bg)" }}>
                              <div className="row" style={{ justifyContent: "space-between" }}>
                                <span className="pill small accent">{c.operator ?? (c.node_kind ?? "config")}</span>
                                <span className="small">{c.fitness != null ? c.fitness.toFixed(4) : "—"}</span>
                              </div>
                              <div className="mono small muted" style={{ marginTop: 3, wordBreak: "break-all" }}>
                                {c.node_kind === "program" && c.code
                                  ? truncate(c.code.split("\n").find((l) => l.trim()) || "", 48)
                                  : truncate(JSON.stringify(c.params), 48)}
                              </div>
                              <div className="row" style={{ justifyContent: "space-between", marginTop: 3 }}>
                                <span className="muted small">g{c.generation}</span>
                                <span className="muted small">{c.status}</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <table className="lb-table">
                  <thead>
                    <tr><th style={{ width: 50 }}>代</th><th>参数</th><th style={{ width: 80 }}>适应度</th><th style={{ width: 70 }}>新颖度</th><th style={{ width: 100 }}>状态</th></tr>
                  </thead>
                  <tbody>
                    {candidates.map((c) => (
                      <tr key={c.candidate_id}>
                        <td className="small">g{c.generation}</td>
                        <td className="mono small">{JSON.stringify(c.params)}</td>
                        <td className="small">{c.fitness != null ? c.fitness.toFixed(4) : "—"}</td>
                        <td className="small">{c.novelty}</td>
                        <td className="small">{c.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
