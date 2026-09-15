/** Experiment comparison view — side-by-side metrics across runs.

Features:
  - Select runs from a dropdown (shows recent runs)
  - Bar chart comparing scores
  - Detail table with model / fe / cv / audit settings
  - Sort by score
*/

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  getRuns,
  getBenchmarkTasks,
  getResearchRecords,
  ResearchRecord,
  WorkflowRunSummary,
  BenchmarkTask,
  STATUS_LABEL,
  STATUS_CLASS,
  compareRuns,
} from "../api/client";

/** Compact "MM-DD HH:mm" formatting for the run cards / table. */
const fmtTime = (iso?: string | null): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** Human-readable wall-clock duration between two ISO timestamps. */
const fmtDuration = (startIso?: string | null, endIso?: string | null): string => {
  if (!startIso || !endIso) return "";
  const ms = Date.parse(endIso) - Date.parse(startIso);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 ? ` ${s % 60}s` : ""}`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60 ? ` ${m % 60}m` : ""}`;
};

interface CompareRow {
  run_id: string;
  task_id: string;
  task_name: string;
  status: string;
  metric_name: string;
  /** "higher" | "lower" — optimization direction of the task's metric. */
  direction?: string;
  score: number | null;
  model: string;
  fe: string;
  cv_folds: number;
  mode: string;
  audit_threshold: number;
  max_outer_iters: number;
  started_at: string | null;
}

/** Validate an array of comparison objects at runtime, logging schema drift. */
function validateCompareRows(raw: unknown): CompareRow[] {
  if (!Array.isArray(raw)) {
    console.warn("[CompareRuns] Expected array, got", typeof raw);
    return [];
  }
  return raw.filter((item, i) => {
    if (!item || typeof item !== "object") {
      console.warn("[CompareRuns] Row", i, "is not an object:", item);
      return false;
    }
    const r = item as Record<string, unknown>;
    if (typeof r.run_id !== "string") {
      console.warn("[CompareRuns] Row", i, "missing run_id:", r);
      return false;
    }
    return true;
  }) as CompareRow[];
}

export function CompareRuns({
  onOpenRun,
}: {
  onOpenRun: (runId: string) => void;
}) {
  const [allRuns, setAllRuns] = useState<WorkflowRunSummary[]>([]);
  const [taskMap, setTaskMap] = useState<Record<string, BenchmarkTask>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [rows, setRows] = useState<CompareRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [recordByRun, setRecordByRun] = useState<Record<string, ResearchRecord>>({});

  useEffect(() => {
    getRuns().then(setAllRuns).catch((e) => console.warn("[CompareRuns] Failed to load runs:", e));
    getBenchmarkTasks()
      .then((ts) => {
        const m: Record<string, BenchmarkTask> = {};
        for (const t of ts) m[t.task_id] = t;
        setTaskMap(m);
      })
      .catch((e) => console.warn("[CompareRuns] Failed to load benchmark tasks:", e));
    // 研究记录带每个 run 的最终指标（metric_name + score + 最优 3 标记）。
    getResearchRecords()
      .then((rs) => {
        const m: Record<string, ResearchRecord> = {};
        for (const r of rs) if (r.run_id) m[r.run_id] = r;
        setRecordByRun(m);
      })
      .catch((e) => console.warn("[CompareRuns] Failed to load research records:", e));
  }, []);

  const taskName = (tid: string) => taskMap[tid]?.name || tid;
  const runLabel = (r: WorkflowRunSummary) =>
    `${taskName(r.target_id)} · ${STATUS_LABEL[r.status] || r.status}`;

  const runById = useMemo(() => {
    const m: Record<string, WorkflowRunSummary> = {};
    for (const r of allRuns) m[r.run_id] = r;
    return m;
  }, [allRuns]);

  /** 时间信息：起 → 止（耗时）；未结束的显示「进行中」。 */
  const timeText = (rid: string, started_at?: string | null): string => {
    const r = runById[rid];
    const start = r?.started_at || started_at || null;
    const end = r?.ended_at || null;
    const endText = end
      ? fmtTime(end)
      : ["running", "waiting_approval"].includes(r?.status || "")
      ? "进行中"
      : "—";
    const dur = fmtDuration(start, end);
    return `${fmtTime(start)} → ${endText}${dur ? `（${dur}）` : ""}`;
  };

  /** 最终性能：研究记录中的最终指标 + 最优 3 标记。 */
  const perfOf = (rid: string): ResearchRecord | undefined => recordByRun[rid];

  // 当前已选 run 所属的任务名（同任务不同优化版本才能对比；空 = 未定）。
  const selectedTaskName = useMemo(() => {
    const names = new Set<string>();
    for (const r of allRuns) {
      if (selected.has(r.run_id)) names.add(taskName(r.target_id));
    }
    return names.size === 1 ? [...names][0] : "";
  }, [allRuns, selected, taskMap]);

  const toggle = (rid: string, name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) {
        next.delete(rid);
        return next;
      }
      // 约束：只能对比同一任务（同名任务）的不同 run。
      if (selectedTaskName && name !== selectedTaskName) return prev;
      if (next.size < 8) next.add(rid); // max 8 comparison
      return next;
    });
  };

  const fetchCompare = useCallback(async () => {
    if (selected.size === 0) { setRows([]); return; }
    setLoading(true);
    setError("");
    try {
      const raw = await compareRuns([...selected]);
      setRows(validateCompareRows(raw));
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [selected]);

  useEffect(() => { fetchCompare(); }, [fetchCompare]);

  // R12 fix: rank direction-aware. A lower-is-better metric (e.g. eval_loss)
  // used to be sorted descending, so the WORST run appeared on top.
  const sortedRows = useMemo(() => {
    const rank = (r: CompareRow) =>
      typeof r.score === "number" && Number.isFinite(r.score)
        ? (r.direction === "lower" ? -r.score : r.score)
        : -Infinity;
    return [...rows].sort((a, b) => rank(b) - rank(a));
  }, [rows]);

  const { maxScore, minPositive } = useMemo(() => {
    const vals = sortedRows
      .map((r) => r.score)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    const pos = vals.filter((v) => v > 0);
    return {
      maxScore: Math.max(...vals, 0.01),
      minPositive: pos.length ? Math.min(...pos) : 0,
    };
  }, [sortedRows]);

  /** Bar length: always "longer = better", whichever direction the metric runs. */
  const barWidth = (r: CompareRow): number => {
    if (typeof r.score !== "number" || !Number.isFinite(r.score)) return 0;
    if (r.direction === "lower") {
      return minPositive > 0 && r.score > 0
        ? Math.min(100, (minPositive / r.score) * 100)
        : 0;
    }
    return Math.max(0, Math.min(100, (r.score / maxScore) * 100));
  };

  const recentRuns = useMemo(() =>
    [...allRuns]
      .filter((r) => r.status !== "requested")
      .sort((a, b) => (b.started_at ? Date.parse(b.started_at) : 0) - (a.started_at ? Date.parse(a.started_at) : 0))
      .slice(0, 30),
    [allRuns]
  );

  // 按任务名称聚合（同名任务 = 同一任务的不同优化版本）。
  const taskGroups = useMemo(() => {
    const m = new Map<string, WorkflowRunSummary[]>();
    for (const r of recentRuns) {
      const name = taskName(r.target_id);
      const arr = m.get(name);
      if (arr) arr.push(r);
      else m.set(name, [r]);
    }
    return [...m.entries()];
  }, [recentRuns, taskMap]);

  return (
    <div className="card">
      <h2>📊 实验对比</h2>
      <p className="muted">
        任务按名称聚合；<b>只有同一任务（同名）的不同优化版本 run 才能选择对比</b>，
        最多 8 个。当前对比目标：
        {selectedTaskName ? (
          <b> {selectedTaskName}</b>
        ) : (
          <span className="muted"> 未选择（点击任一 run 后，同任务的其他版本可选）</span>
        )}
        {selected.size > 0 && (
          <button className="btn tiny" style={{ marginLeft: 10 }} onClick={() => setSelected(new Set())}>
            清空选择
          </button>
        )}
      </p>

      {error && <div className="card error" style={{ marginTop: 8 }}>{error}</div>}

      {/* ---- Run picker（按任务名称分组）---- */}
      <div style={{ marginTop: 12 }}>
        {taskGroups.map(([name, runs]) => {
          const locked = !!selectedTaskName && name !== selectedTaskName;
          return (
            <div key={name} style={{ marginTop: 12 }}>
              <h3 style={{ borderBottom: "1px solid var(--border)", paddingBottom: 4, opacity: locked ? 0.55 : 1 }}>
                {name} <span className="muted">({runs.length} 个版本)</span>
                {locked && <span className="muted small" style={{ marginLeft: 8 }}>(已锁定其他任务，清空选择后可选)</span>}
              </h3>
              <div className="task-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
                {runs.map((r) => {
                  const disabled = locked;
                  return (
                    <button
                      key={r.run_id}
                      type="button"
                      className={`task-card ${selected.has(r.run_id) ? "selected" : ""}`}
                      disabled={disabled}
                      title={disabled ? `已选择「${selectedTaskName}」的 run；只有同一任务的不同版本才能对比` : undefined}
                      style={disabled ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
                      onClick={() => toggle(r.run_id, name)}
                    >
                      <div className="row">
                        <span className={`pill ${STATUS_CLASS[r.status] || "accent"}`}>{STATUS_LABEL[r.status] || r.status}</span>
                        <strong>{name}</strong>
                      </div>
                      <div className="muted small" style={{ marginTop: 4 }}>
                        🕐 {timeText(r.run_id, r.started_at)}
                      </div>
                      <div className="small" style={{ marginTop: 2 }}>
                        {(() => {
                          const rec = perfOf(r.run_id);
                          if (!rec) return <span className="muted">最终性能 —（无研究记录）</span>;
                          return (
                            <>
                              最终 <b>{rec.metric_name}</b> ={" "}
                              <span className="mono">{rec.score?.toFixed?.(4) ?? rec.score}</span>
                              {rec.is_top3 && <span title="该任务最优 3 条记录之一"> 🏅</span>}
                            </>
                          );
                        })()}
                      </div>
                      <div className="muted mono small">{r.run_id}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
        {taskGroups.length === 0 && <div className="muted">暂无已完成的 run。</div>}
      </div>

      {selected.size > 0 && (
        <div style={{ marginTop: 12 }}>
          <button className="btn primary" disabled={loading} onClick={fetchCompare}>
            {loading ? "对比中…" : "开始对比"}
          </button>
          <span className="muted small" style={{ marginLeft: 10 }}>已选 {selected.size} 项</span>
        </div>
      )}

      {/* ---- Comparison chart ---- */}
      {sortedRows.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <h3>指标对比</h3>
          <div className="compare-chart">
            {sortedRows.map((r) => (
              <div key={r.run_id} className="compare-bar-row">
                <div className="compare-bar-label" style={{ minWidth: 160 }}>
                  <div className="mono small">{taskName(r.task_id)}</div>
                  <div className="muted mono" style={{ fontSize: 10 }}>{r.run_id.slice(0, 16)}…</div>
                </div>
                <div className="compare-bar-track" style={{ flex: 1 }}>
                  <div
                    className="compare-bar-fill"
                    style={{ width: `${barWidth(r)}%` }}
                  />
                </div>
                <span
                  className="mono"
                  style={{ minWidth: 82, textAlign: "right", fontWeight: 700 }}
                  title={r.direction === "lower" ? `${r.metric_name}（越低越好）` : r.metric_name}
                >
                  {typeof r.score === "number" ? r.score.toFixed(4) : "—"}
                  {r.direction === "lower" && <span className="muted small"> ↓</span>}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ---- Detail table ---- */}
      {sortedRows.length > 0 && (
        <table className="lb-table" style={{ marginTop: 18 }}>
          <thead>
            <tr>
              <th>Run</th>
              <th>起止时间（耗时）</th>
              <th>最终性能</th>
              <th>模型</th>
              <th>FE</th>
              <th>CV</th>
              <th>审计严格度</th>
              <th>最大轮数</th>
              <th>模式</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((r) => {
              const rec = perfOf(r.run_id);
              return (
              <tr key={r.run_id}>
                <td>
                  <div className="mono small">{taskName(r.task_id)}</div>
                  <div className="muted mono" style={{ fontSize: 10 }}>{r.run_id}</div>
                </td>
                <td className="muted small">{timeText(r.run_id, r.started_at)}</td>
                <td>
                  {rec ? (
                    <span className="mono">
                      {rec.metric_name} = <strong>{rec.score?.toFixed?.(4) ?? rec.score}</strong>
                      {rec.is_top3 && <span title="该任务最优 3 条记录之一"> 🏅</span>}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td><code>{r.model}</code></td>
                <td><span className="pill accent">{r.fe}</span></td>
                <td className="mono">{r.cv_folds}</td>
                <td className="mono">{r.audit_threshold.toFixed(2)}</td>
                <td className="mono">{r.max_outer_iters}</td>
                <td><span className="pill warn">{r.mode}</span></td>
                <td>
                  <button className="btn tiny" onClick={() => onOpenRun(r.run_id)}>查看</button>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
