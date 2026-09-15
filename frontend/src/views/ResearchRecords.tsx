import React, { useEffect, useMemo, useState } from "react";
import {
  cancelRun,
  deleteRun,
  getResearchRecords,
  getRunsLiveness,
  ResearchRecord,
  RunLiveness,
  STATUS_LABEL,
  STATUS_CLASS,
  TERMINAL_RUN_STATUSES,
  categoryLabel,
  categoryGroup,
  WorkflowRunSummary,
} from "../api/client";
import { useConfirm } from "../components/ConfirmDialog";

const CAT_FALLBACK = "未分类";

const pad = (n: number) => String(n).padStart(2, "0");

/** Compact "MM-DD HH:mm" formatting for the records table. */
const fmtTime = (iso?: string | null): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
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

interface Props {
  runs: WorkflowRunSummary[];
  /** Resolve a run's display name: 任务库名 → objective_snapshot.name → target_id. */
  nameOf: (r: WorkflowRunSummary) => string;
  loading: boolean;
  onOpenRun: (runId: string) => void;
  onNewResearch: () => void;
  /** Called after a run is deleted so the parent refreshes its run list. */
  onDeleted?: () => void;
}

export function ResearchRecords({ runs, nameOf, loading, onOpenRun, onNewResearch, onDeleted }: Props) {
  const confirmDialog = useConfirm();
  const [records, setRecords] = useState<ResearchRecord[]>([]);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  // 运行中 run 的活性信息（真在运行 / 疑似卡死 / 僵尸），30s 轮询刷新。
  const [liveness, setLiveness] = useState<Record<string, RunLiveness>>({});
  const [cancelBusyId, setCancelBusyId] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () =>
      getRunsLiveness()
        .then((snap) => {
          if (alive) setLiveness(snap);
        })
        .catch(() => {
          /* liveness column degrades silently */
        });
    load();
    const t = setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runs.length]);

  // Research records carry the captured best metric per run — join by run_id so the
  // table can show the outcome score next to the execution status.
  useEffect(() => {
    let alive = true;
    getResearchRecords()
      .then((rs) => {
        if (alive) setRecords(rs);
      })
      .catch(() => {
        /* metric column degrades to "—" when records are unavailable */
      });
    return () => {
      alive = false;
    };
  }, [runs.length]);

  const metricByRun = useMemo(() => {
    const m = new Map<string, ResearchRecord>();
    for (const r of records) {
      // A run may have several records; prefer the top3-flagged one.
      const prev = m.get(r.run_id);
      if (!prev || (r.is_top3 && !prev.is_top3)) m.set(r.run_id, r);
    }
    return m;
  }, [records]);

  const categories = useMemo(() => {
    const s = new Set<string>();
    for (const r of runs)
      s.add(categoryGroup(r.objective_snapshot?.category || CAT_FALLBACK));
    return [...s].sort();
  }, [runs]);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const startMs = (r: WorkflowRunSummary) =>
      r.started_at ? Date.parse(r.started_at) || 0 : 0;
    return [...runs]
      .filter((r) => {
        if (statusFilter !== "all" && r.status !== statusFilter) return false;
        if (
          categoryFilter !== "all" &&
          categoryGroup(r.objective_snapshot?.category || CAT_FALLBACK) !== categoryFilter
        )
          return false;
        if (q) {
          const haystack = [
            nameOf(r),
            r.target_id,
            r.objective_snapshot?.name,
          ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
          if (!haystack.includes(q) && !r.run_id.toLowerCase().includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => startMs(b) - startMs(a));
  }, [runs, search, statusFilter, categoryFilter, nameOf]);

  const statusCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of runs) c[r.status] = (c[r.status] || 0) + 1;
    return c;
  }, [runs]);

  // 按任务聚合：同一任务（target_id）的全部 run 收进一个分组。
  const startMsOf = (r: WorkflowRunSummary) =>
    r.started_at ? Date.parse(r.started_at) || 0 : 0;

  const groups = useMemo(() => {
    const m = new Map<string, WorkflowRunSummary[]>();
    for (const r of rows) {
      if (!m.has(r.target_id)) m.set(r.target_id, []);
      m.get(r.target_id)!.push(r);
    }
    // 分组按组内最新开始时间倒序；组内 run 同样倒序。
    const out = [...m.entries()].map(([taskId, items]) => {
      const sortedItems = [...items].sort((a, b) => startMsOf(b) - startMsOf(a));
      const statusTally: Record<string, number> = {};
      for (const r of sortedItems) statusTally[r.status] = (statusTally[r.status] || 0) + 1;
      const best = sortedItems
        .map((r) => metricByRun.get(r.run_id))
        .filter(Boolean)
        .sort((a: any, b: any) => {
          if (!a || !b) return 0;
          const dir = a.direction === "lower" ? -1 : 1;
          return (a.score - b.score) * dir;
        })[0];
      return {
        taskId,
        name: nameOf(sortedItems[0]),
        runs: sortedItems,
        statusTally,
        best,
        latest: sortedItems[0],
      };
    });
    out.sort((a, b) => startMsOf(b.latest) - startMsOf(a.latest));
    return out;
  }, [rows, metricByRun, nameOf]);

  const [groupedView, setGroupedView] = useState(true);
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set());
  const [deleteBusyId, setDeleteBusyId] = useState("");
  const [deleteError, setDeleteError] = useState("");

  const toggleTask = (taskId: string) =>
    setExpandedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });

  /** 一条 run 的行内容（聚合展开与平铺视图共用）。 */
  const renderRunCells = (r: WorkflowRunSummary) => {
    const rec = metricByRun.get(r.run_id);
    const ended = r.ended_at
      ? fmtTime(r.ended_at)
      : ["running", "waiting_approval"].includes(r.status)
      ? "进行中"
      : "—";
    const dur = fmtDuration(r.started_at, r.ended_at);
    return (
      <>
        <td>
          <span className={`pill ${STATUS_CLASS[r.status] || "accent"}`}>
            {STATUS_LABEL[r.status] || r.status}
          </span>
          {r.status_detail && (
            <div className="muted" style={{ fontSize: 11, marginTop: 4, maxWidth: 220 }}
                 title={r.status_detail}>
              {r.status_detail.length > 48 ? `${r.status_detail.slice(0, 48)}…` : r.status_detail}
            </div>
          )}
          {r.status === "running" && (() => {
            const lv = liveness[r.run_id];
            if (!lv) return null;
            return (
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }} title={lv.reason}>
                {lv.verdict === "zombie" && <span className="pill bad">僵尸进程</span>}
                {lv.verdict === "stale" && <span className="pill warn">疑似卡死</span>}
                {lv.stale_minutes != null && (
                  <span style={{ marginLeft: lv.verdict === "stale" || lv.verdict === "zombie" ? 6 : 0 }}>
                    最后活动{" "}
                    {lv.stale_minutes < 1
                      ? "刚刚"
                      : `${Math.round(lv.stale_minutes)} 分钟前`}
                  </span>
                )}
              </div>
            );
          })()}
        </td>
        <td>
          <strong>{nameOf(r)}</strong>
          <div className="muted mono" style={{ fontSize: 12 }}>
            {r.target_id}
            {r.objective_snapshot?.config?.inner_loop?.mode === "agent" && (
              <span className="pill accent" style={{ marginLeft: 6, fontSize: 11 }}>agent</span>
            )}
          </div>
        </td>
        <td className="muted small">
          {r.objective_snapshot?.category
            ? categoryLabel(r.objective_snapshot.category)
            : CAT_FALLBACK}
        </td>
        <td className="mono" style={{ fontSize: 12 }}>
          {fmtTime(r.started_at)} → {ended}
          {dur && <div className="muted" style={{ fontSize: 11 }}>耗时 {dur}</div>}
        </td>
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
        <td className="mono muted" style={{ fontSize: 12 }}>{r.run_id}</td>
        <td onClick={(e) => e.stopPropagation()}>
          {r.status === "running" && (
            <button
              className="btn tiny"
              disabled={cancelBusyId === r.run_id}
              title="请求终止该 run（对运行中的执行线程发出取消信号并置为已取消）"
              onClick={async () => {
                if (!(await confirmDialog({
                  title: "终止运行中的研究",
                  message: `确认终止运行中的研究 ${r.run_id}？将向执行线程发出取消信号并置为已取消。`,
                  confirmLabel: "终止",
                  danger: true,
                }))) return;
                setCancelBusyId(r.run_id);
                try {
                  await cancelRun(r.run_id);
                  onDeleted?.();
                } catch (e: any) {
                  setDeleteError(`终止失败：${e?.message || e}`);
                } finally {
                  setCancelBusyId("");
                }
              }}
            >
              {cancelBusyId === r.run_id ? "终止中…" : "终止"}
            </button>
          )}
          {TERMINAL_RUN_STATUSES.has(r.status) && (
            <button
              className="btn tiny"
              disabled={deleteBusyId === r.run_id}
              title="删除该研究及其底层持久化记录与沙箱临时文件"
              onClick={async () => {
                if (!(await confirmDialog({
                  title: "删除研究",
                  message: `确认删除研究 ${r.run_id}？将同时清理 stages / 事件 / 产物 / 沙箱临时文件等底层记录，不可恢复（不影响任务数据集）。`,
                  confirmLabel: "删除",
                  danger: true,
                }))) return;
                setDeleteBusyId(r.run_id);
                try {
                  await deleteRun(r.run_id);
                  onDeleted?.();
                } catch (e: any) {
                  setDeleteError(`删除失败：${e?.message || e}`);
                } finally {
                  setDeleteBusyId("");
                }
              }}
            >
              {deleteBusyId === r.run_id ? "删除中…" : "删除"}
            </button>
          )}
        </td>
      </>
    );
  };

  return (
    <div className="card" style={{ padding: "18px 20px" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>📋 研究记录</h2>
          <p className="muted" style={{ marginTop: 6 }}>
            全部历史研究的执行情况一览：起止时间、任务、执行状态与关键指标。点击任意一行进入该研究的详情页
            （总览 / 审计结论 / 假设树 / 事件流水）。
          </p>
        </div>
        <button className="btn primary" onClick={onNewResearch}>＋ 新建研究</button>
      </div>

      <div className="row" style={{ marginTop: 12, flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <input
          className="sidebar-search"
          style={{ minWidth: 220 }}
          placeholder="搜索任务名或 run id…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="sidebar-status"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          title="按运行状态筛选"
        >
          <option value="all">全部状态 ({runs.length})</option>
          {Object.keys(STATUS_LABEL)
            .filter((s) => statusCounts[s])
            .map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]} ({statusCounts[s]})
              </option>
            ))}
        </select>
        <select
          className="sidebar-status"
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          title="按任务类别筛选"
        >
          <option value="all">全部类别</option>
          {categories.map((c) => (
            <option key={c} value={c}>{categoryLabel(c)}</option>
          ))}
        </select>
        <span className="muted small">
          共 {rows.length} 条记录
        </span>
        <button
          className={`btn small ${groupedView ? "primary" : ""}`}
          onClick={() => setGroupedView(true)}
          title="同一任务的所有 run 聚合为一行，点击展开"
        >
          按任务聚合
        </button>
        <button
          className={`btn small ${!groupedView ? "primary" : ""}`}
          onClick={() => setGroupedView(false)}
        >
          平铺列表
        </button>
      </div>

      {rows.length === 0 ? (
        <div className="muted" style={{ padding: "28px 0", textAlign: "center" }}>
          {loading ? "加载中…" : search || statusFilter !== "all" || categoryFilter !== "all"
            ? "没有匹配的研究记录，调整筛选条件试试。"
            : "还没有研究记录——点击右上角「＋ 新建研究」启动第一次研究。"}
        </div>
      ) : (
        <>
          {deleteError && (
            <div className="card error" style={{ marginTop: 10 }}>{deleteError}</div>
          )}
          {groupedView ? (
        /* ==================== 按任务聚合视图 ==================== */
        <table className="lb-table rec-table" style={{ marginTop: 14 }}>
          <thead>
            <tr>
              <th style={{ width: 36 }}>▸</th>
              <th>任务名称</th>
              <th style={{ width: 220 }}>执行情况</th>
              <th style={{ width: 110 }}>run 数</th>
              <th style={{ width: 170 }}>最新活动</th>
              <th style={{ width: 160 }}>最优指标</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => {
              const isOpen = expandedTasks.has(g.taskId);
              return (
                <React.Fragment key={g.taskId}>
                  <tr
                    className="rec-row"
                    style={{ cursor: "pointer" }}
                    onClick={() => toggleTask(g.taskId)}
                    title="点击展开该任务的全部研究记录"
                  >
                    <td><span className={`chev ${isOpen ? "collapsed" : ""}`}>▾</span></td>
                    <td>
                      <strong>{g.name}</strong>
                      <div className="muted mono small">{g.taskId}</div>
                    </td>
                    <td>
                      <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                        {Object.entries(g.statusTally).map(([s, n]) => (
                          <span key={s} className={`pill ${STATUS_CLASS[s] || "accent"}`}>
                            {STATUS_LABEL[s] || s} ×{n}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="mono">{g.runs.length}</td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {fmtTime(g.latest?.started_at)}
                    </td>
                    <td>
                      {g.best ? (
                        <span className="mono">
                          {g.best.metric_name} = <strong>{g.best.score?.toFixed?.(4) ?? g.best.score}</strong>
                          {g.best.is_top3 && <span title="该任务最优 3 条记录之一"> 🏅</span>}
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={6} style={{ background: "var(--panel-2)", padding: "10px 16px" }}>
                        <table className="lb-table" style={{ margin: 0 }}>
                          <thead>
                            <tr>
                              <th style={{ width: 90 }}>状态</th>
                              <th>任务名称</th>
                              <th style={{ width: 110 }}>类别</th>
                              <th style={{ width: 230 }}>起止时间</th>
                              <th style={{ width: 130 }}>关键指标</th>
                              <th style={{ width: 190 }}>run id</th>
<th style={{ width: 90 }}>操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {g.runs.map((r) => (
                              <tr
                                key={r.run_id}
                                className="rec-row"
                                style={{ cursor: "pointer", background: "var(--panel)" }}
                                onClick={() => onOpenRun(r.run_id)}
                                title="点击查看该研究的详情页"
                              >
                                {renderRunCells(r)}
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
      ) : (
        /* ==================== 平铺列表视图 ==================== */
        <table className="lb-table rec-table" style={{ marginTop: 14 }}>
          <thead>
            <tr>
              <th style={{ width: 90 }}>状态</th>
              <th>任务名称</th>
              <th style={{ width: 110 }}>类别</th>
              <th style={{ width: 230 }}>起止时间</th>
              <th style={{ width: 130 }}>关键指标</th>
              <th style={{ width: 190 }}>run id</th>
              <th style={{ width: 90 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.run_id}
                className="rec-row"
                onClick={() => onOpenRun(r.run_id)}
                title="点击查看该研究的详情页"
              >
                {renderRunCells(r)}
              </tr>
            ))}
          </tbody>
        </table>
        )}
        </>
      )}
    </div>
  );
}
