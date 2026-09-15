import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  getResearchRecords,
  getLeaderboard,
  getBenchmarkTasks,
  reproduceRecord,
  BenchmarkTask,
  ResearchRecord,
} from "../api/client";

type Mode = "global" | "task";

interface Props {
  onOpenRun: (runId: string) => void;
  onNewResearch: () => void;
  taskName: (taskId: string) => string;
}

const dirLabel = (d: string) => (d === "lower" ? "越低越好 ↓" : "越高越好 ↑");

const EXPAND_LIMIT = 10; // 展开任务后展示的记录条数（最近的最优记录）

export function Leaderboard({ onOpenRun, onNewResearch, taskName }: Props) {
  const [mode, setMode] = useState<Mode>("global");
  const [filterTask, setFilterTask] = useState<string>("");
  const [records, setRecords] = useState<ResearchRecord[]>([]);
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string>("");
  const [expanded, setExpanded] = useState<string | null>(null);

  // Task dropdown entries: only tasks that actually have records matter, but we
  // offer every registered task for convenience.
  useEffect(() => {
    getBenchmarkTasks()
      .then(setTasks)
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data =
        mode === "global"
          ? await getLeaderboard()
          : await getResearchRecords(filterTask || undefined);
      setRecords(data);
    } catch (e: any) {
      setError(String(e?.message || e));
      setRecords([]);
    } finally {
      setLoading(false);
    }
  }, [mode, filterTask]);

  useEffect(() => {
    load();
  }, [load]);

  const taskDirection = useCallback(
    (taskId: string, fallback: string): "lower" | "higher" => {
      const t = tasks.find((x) => x.task_id === taskId);
      if (t?.direction === "lower" || t?.direction === "higher") return t.direction;
      return fallback === "lower" ? "lower" : "higher";
    },
    [tasks],
  );

  // Group records by task; within each task keep the direction-aware best
  // EXPAND_LIMIT records (最优先、同分新记录在前).
  const groups = useMemo(() => {
    const byTask = new Map<string, ResearchRecord[]>();
    for (const r of records) {
      if (!byTask.has(r.task_id)) byTask.set(r.task_id, []);
      byTask.get(r.task_id)!.push(r);
    }
    const out = [...byTask.entries()].map(([taskId, recs]) => {
      const dir = taskDirection(taskId, recs[0]?.direction || "higher");
      const sorted = [...recs].sort((a, b) => {
        const av = Number.isFinite(a.score) ? a.score : Infinity;
        const bv = Number.isFinite(b.score) ? b.score : Infinity;
        const a2 = dir === "lower" ? av : -av;
        const b2 = dir === "lower" ? bv : -bv;
        if (a2 !== b2) return a2 - b2;
        return (b.created_at || "").localeCompare(a.created_at || "");
      });
      return { taskId, dir, records: sorted.slice(0, EXPAND_LIMIT), total: recs.length };
    });
    // 全局模式：按任务最优成绩排布；同向不可比的任务按名称兜底。
    out.sort((a, b) => {
      const best = (g: typeof a) =>
        (g.records[0]?.score ?? 0) * (g.dir === "lower" ? -1 : 1);
      return best(b) - best(a) || a.taskId.localeCompare(b.taskId);
    });
    return out;
  }, [records, taskDirection]);

  const handleReproduce = async (rec: ResearchRecord, autostart = false) => {
    setBusyId(rec.record_id + (autostart ? ":auto" : ""));
    setError("");
    try {
      const res = await reproduceRecord(rec.record_id, autostart);
      // F3: autostarted runs are already driving; non-autostarted open the new
      // REQUESTED run so the user can inspect/launch it from the dashboard.
      onOpenRun(res.run_id);
    } catch (e: any) {
      setError(`复现失败：${String(e?.message || e)}`);
    } finally {
      setBusyId("");
    }
  };

  const configSummary = (rec: ResearchRecord) => {
    const c = rec.config_snapshot;
    if (!c) return "—";
    const parts: string[] = [];
    const inner = c.inner_loop || c;
    if (inner.model) parts.push(`model=${inner.model}`);
    if (inner.preset) parts.push(`preset=${inner.preset}`);
    if (inner.mode) parts.push(`mode=${inner.mode}`);
    if (c.metric_name) parts.push(`metric=${c.metric_name}`);
    if (inner.fe) parts.push(`fe=${inner.fe}`);
    return parts.length ? parts.join(" · ") : "—";
  };

  const medal = (i: number) => (i < 3 ? ["🥇", "🥈", "🥉"][i] : `${i + 1}`);

  return (
    <div className="card" style={{ marginTop: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>🏆 研究榜单</h2>
          <p className="muted" style={{ marginTop: 6 }}>
            按任务分组的自主研究成绩榜：<b>点击任务名展开</b>，可查看该任务最近的最优{" "}
            <b>{EXPAND_LIMIT}</b> 条研究记录（指标、配置、时间），并逐条 <b>复现</b> / 查看对应 run。
          </p>
        </div>
        <button className="btn primary" onClick={onNewResearch}>
          ＋ 新建研究
        </button>
      </div>

      <div className="row" style={{ marginTop: 12, flexWrap: "wrap", gap: 8 }}>
        <button
          className={`btn ${mode === "global" ? "primary" : ""}`}
          onClick={() => setMode("global")}
        >
          全局跨任务榜
        </button>
        <button
          className={`btn ${mode === "task" ? "primary" : ""}`}
          onClick={() => setMode("task")}
        >
          按任务查看
        </button>
        {mode === "task" && (
          <select
            className="input"
            value={filterTask}
            onChange={(e) => setFilterTask(e.target.value)}
            style={{ minWidth: 240 }}
          >
            <option value="">全部任务</option>
            {tasks.map((t) => (
              <option key={t.task_id} value={t.task_id}>
                {t.name}（{t.task_id}）
              </option>
            ))}
          </select>
        )}
        <button className="btn ghost" onClick={load} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="card error" style={{ marginTop: 12 }}>{error}</div>}

      {groups.length === 0 ? (
        <div className="muted" style={{ padding: 24, textAlign: "center" }}>
          暂无研究记录。完成一次研究（或上报结果指标）后，这里会显示其最优成绩。
        </div>
      ) : (
        <table className="lb-table" style={{ marginTop: 14 }}>
          <thead>
            <tr>
              <th style={{ width: 40 }}>▸</th>
              <th>任务</th>
              <th style={{ width: 130 }}>最优分</th>
              <th style={{ width: 120 }}>指标 / 方向</th>
              <th style={{ width: 110 }}>记录数</th>
              <th style={{ width: 150 }}>最新记录时间</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => {
              const best = g.records[0];
              const isOpen = expanded === g.taskId;
              return (
                <React.Fragment key={g.taskId}>
                  <tr
                    className="rec-row"
                    onClick={() => setExpanded(isOpen ? null : g.taskId)}
                    style={{ cursor: "pointer" }}
                    title="点击展开该任务的最优研究记录"
                  >
                    <td>
                      <span className={`chev ${isOpen ? "collapsed" : ""}`}>▾</span>
                    </td>
                    <td>
                      <div><strong>{taskName(g.taskId)}</strong></div>
                      <div className="muted mono small">{g.taskId}</div>
                    </td>
                    <td>
                      {best ? (
                        <b className="mono">{best.score?.toFixed?.(4) ?? best.score}</b>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="muted small">
                      <code>{best?.metric_name || "—"}</code> · {dirLabel(g.dir)}
                    </td>
                    <td className="muted small">{g.total}</td>
                    <td className="muted small">
                      {best?.created_at
                        ? best.created_at.replace("T", " ").slice(0, 19)
                        : "—"}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={6} style={{ background: "var(--panel-2)", padding: "10px 16px" }}>
                        <table className="lb-table" style={{ margin: 0 }}>
                          <thead>
                            <tr>
                              <th style={{ width: 44 }}>#</th>
                              <th style={{ width: 110 }}>分数</th>
                              <th style={{ width: 100 }}>指标</th>
                              <th>配置摘要</th>
                              <th style={{ width: 170 }}>记录时间</th>
                              <th style={{ width: 240 }}>操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {g.records.map((rec, i) => (
                              <tr key={rec.record_id} className={rec.is_top3 ? "top3" : ""}>
                                <td>
                                  {rec.is_top3 ? (
                                    <span className="medal">{medal(i)}</span>
                                  ) : (
                                    <span className="muted">{i + 1}</span>
                                  )}
                                </td>
                                <td>
                                  <b className="mono">
                                    {typeof rec.score === "number" ? rec.score.toFixed(4) : rec.score}
                                  </b>
                                  {rec.is_top3 && (
                                    <span className="pill ok small" style={{ marginLeft: 6 }}>最优</span>
                                  )}
                                </td>
                                <td><code>{rec.metric_name}</code></td>
                                <td className="muted small">{configSummary(rec)}</td>
                                <td className="muted small">
                                  {rec.created_at
                                    ? rec.created_at.replace("T", " ").slice(0, 19)
                                    : "—"}
                                </td>
                                <td>
                                  <div className="row" style={{ gap: 6 }}>
                                    <button className="btn small" onClick={() => onOpenRun(rec.run_id)}>
                                      查看 run
                                    </button>
                                    <button
                                      className="btn small ghost"
                                      disabled={busyId.startsWith(rec.record_id)}
                                      onClick={() => handleReproduce(rec)}
                                    >
                                      {busyId === rec.record_id ? "建档中…" : "复现"}
                                    </button>
                                    <button
                                      className="btn small primary"
                                      disabled={busyId.startsWith(rec.record_id)}
                                      onClick={() => handleReproduce(rec, true)}
                                      title="按记录的配置快照重新启动一次完整研究"
                                    >
                                      {busyId === `${rec.record_id}:auto` ? "启动中…" : "复现并启动"}
                                    </button>
                                  </div>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
