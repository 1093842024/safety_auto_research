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

export function Leaderboard({ onOpenRun, onNewResearch, taskName }: Props) {
  const [mode, setMode] = useState<Mode>("global");
  const [filterTask, setFilterTask] = useState<string>("");
  const [records, setRecords] = useState<ResearchRecord[]>([]);
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string>("");

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

  const sorted = useMemo(() => {
    // global: data already ranked; task: sort by score & direction.
    if (mode === "global") return records;
    return [...records].sort((a, b) => {
      const dir = a.direction === "lower" ? -1 : 1;
      return (a.score - b.score) * dir;
    });
  }, [records, mode]);

  const handleReproduce = async (rec: ResearchRecord) => {
    setBusyId(rec.record_id);
    setError("");
    try {
      const res = await reproduceRecord(rec.record_id);
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

  return (
    <div className="card" style={{ marginTop: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>🏆 研究榜单</h2>
          <p className="muted" style={{ marginTop: 6 }}>
            每个研究任务自动保存效果最优的 <b>3 次</b> 自主研究记录；下方可回顾历史、按任务筛选、
            跨任务查看全局榜，并一键 <b>复现</b> 重跑。
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

      {sorted.length === 0 ? (
        <div className="muted" style={{ padding: 24, textAlign: "center" }}>
          暂无研究记录。完成一次研究（或上报结果指标）后，这里会显示其最优成绩。
        </div>
      ) : (
        <table className="lb-table" style={{ marginTop: 14 }}>
          <thead>
            <tr>
              <th style={{ width: 56 }}>#</th>
              <th>任务</th>
              <th>指标</th>
              <th style={{ width: 110 }}>分数</th>
              <th>方向</th>
              <th>配置摘要</th>
              <th>记录时间</th>
              <th style={{ width: 180 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((rec, i) => (
              <tr key={rec.record_id} className={rec.is_top3 ? "top3" : ""}>
                <td>
                  {rec.is_top3 ? (
                    <span className="medal">{["🥇", "🥈", "🥉"][i] || "★"}</span>
                  ) : (
                    <span className="muted">{i + 1}</span>
                  )}
                </td>
                <td>
                  <div><strong>{taskName(rec.task_id)}</strong></div>
                  <div className="muted mono small">{rec.task_id}</div>
                </td>
                <td><code>{rec.metric_name}</code></td>
                <td>
                  <b>{typeof rec.score === "number" ? rec.score.toFixed(4) : rec.score}</b>
                  {rec.is_top3 && <span className="pill ok small" style={{ marginLeft: 6 }}>最优3</span>}
                </td>
                <td className="muted small">{dirLabel(rec.direction)}</td>
                <td className="muted small">{configSummary(rec)}</td>
                <td className="muted small">
                  {rec.created_at ? rec.created_at.replace("T", " ").slice(0, 19) : "—"}
                </td>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn small" onClick={() => onOpenRun(rec.run_id)}>
                      查看 run
                    </button>
                    <button
                      className="btn small ghost"
                      disabled={busyId === rec.record_id}
                      onClick={() => handleReproduce(rec)}
                    >
                      {busyId === rec.record_id ? "复现中…" : "复现"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
