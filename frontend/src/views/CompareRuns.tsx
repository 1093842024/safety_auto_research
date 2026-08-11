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
  WorkflowRunSummary,
  BenchmarkTask,
  STATUS_LABEL,
  STATUS_CLASS,
  compareRuns,
} from "../api/client";

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

  useEffect(() => {
    getRuns().then(setAllRuns).catch((e) => console.warn("[CompareRuns] Failed to load runs:", e));
    getBenchmarkTasks()
      .then((ts) => {
        const m: Record<string, BenchmarkTask> = {};
        for (const t of ts) m[t.task_id] = t;
        setTaskMap(m);
      })
      .catch((e) => console.warn("[CompareRuns] Failed to load benchmark tasks:", e));
  }, []);

  const taskName = (tid: string) => taskMap[tid]?.name || tid;
  const runLabel = (r: WorkflowRunSummary) =>
    `${taskName(r.target_id)} · ${STATUS_LABEL[r.status] || r.status}`;

  const toggle = (rid: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) next.delete(rid);
      else if (next.size < 8) next.add(rid); // max 8 comparison
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

  return (
    <div className="card">
      <h2>📊 实验对比</h2>
      <p className="muted">
        选择最多 8 个已完成的研究 run，对比其指标、模型、配置。
      </p>

      {error && <div className="card error" style={{ marginTop: 8 }}>{error}</div>}

      {/* ---- Run picker ---- */}
      <div style={{ marginTop: 12 }}>
        <div className="task-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
          {recentRuns.map((r) => (
            <button
              key={r.run_id}
              type="button"
              className={`task-card ${selected.has(r.run_id) ? "selected" : ""}`}
              onClick={() => toggle(r.run_id)}
            >
              <div className="row">
                <span className={`pill ${STATUS_CLASS[r.status] || "accent"}`}>{STATUS_LABEL[r.status] || r.status}</span>
                <strong>{taskName(r.target_id)}</strong>
              </div>
              <div className="muted mono small">{r.run_id}</div>
            </button>
          ))}
        </div>
        {recentRuns.length === 0 && <div className="muted">暂无已完成的 run。</div>}
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
            {sortedRows.map((r) => (
              <tr key={r.run_id}>
                <td>
                  <div className="mono small">{taskName(r.task_id)}</div>
                  <div className="muted mono" style={{ fontSize: 10 }}>{r.run_id}</div>
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
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
