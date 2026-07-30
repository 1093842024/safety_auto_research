import React, { useEffect, useMemo, useState } from "react";
import { getRuns, getBenchmarkTasks, BenchmarkTask, WorkflowRunSummary } from "./api/client";
import { NewResearch } from "./views/NewResearch";
import { RunDashboard } from "./views/RunDashboard";
import { BenchmarkCatalog } from "./views/BenchmarkCatalog";
import { Leaderboard } from "./views/Leaderboard";
import { CompareRuns } from "./views/CompareRuns";

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  requested: "已请求",
  waiting_approval: "等待审批",
  succeeded: "成功",
  failed: "失败",
  exited_budget: "已完成·已达最大轮数",
  exited_converged: "已完成·审计通过",
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

const CATEGORY_LABELS: Record<string, string> = {
  model_dev: "模型开发",
  system_opt: "系统优化",
  puzzle: "谜题/挑战",
  cuda: "CUDA 内核",
  adversarial: "对抗/越狱",
  efficiency: "效率基准",
  agent_eval: "科研 Agent 评测",
  idea_eval: "想法质量评测",
  tooling: "工具型元评测",
  platform_native: "平台原生(可实跑)",
};
const CAT_FALLBACK = "未分类";

const runCategory = (r: WorkflowRunSummary) =>
  r.objective_snapshot?.category || CAT_FALLBACK;

const runTime = (r: WorkflowRunSummary): number => {
  const t = r.started_at;
  if (!t) return 0;
  const ms = Date.parse(t);
  return Number.isNaN(ms) ? 0 : ms;
};

interface RunGroup {
  category: string;
  label: string;
  runs: WorkflowRunSummary[];
  newest: number;
}

export function App() {
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [taskMap, setTaskMap] = useState<Record<string, BenchmarkTask>>({});
  const [runId, setRunId] = useState<string>("");
  const [view, setView] = useState<"welcome" | "new" | "run" | "catalog" | "leaderboard" | "compare">("welcome");
  const [error, setError] = useState<string>("");
  // Per-category collapse state for the research-records sidebar.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // When jumping from the catalog to "new research", pre-select this task.
  const [newInitialTaskId, setNewInitialTaskId] = useState<string | null>(null);

  const refreshRuns = async () => {
    try {
      const r = await getRuns();
      setRuns(r);
      setRunId((prev) => (prev ? prev : r.length ? r[r.length - 1].run_id : prev));
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  };

  useEffect(() => {
    refreshRuns();
    const t = setInterval(refreshRuns, 4000);
    // 任务目录：用于把 target_id 映射成友好名称
    getBenchmarkTasks()
      .then((ts) => {
        const m: Record<string, BenchmarkTask> = {};
        for (const t of ts) m[t.task_id] = t;
        setTaskMap(m);
      })
      .catch((err: any) => {
        console.warn("[App] Failed to load benchmark tasks:", err);
      });
    return () => clearInterval(t);
  }, []);

  const selectRun = (id: string) => {
    setRunId(id);
    setView("run");
  };

  const runName = (r: WorkflowRunSummary) =>
    taskMap[r.target_id]?.name || r.objective_snapshot?.name || r.target_id;

  // Group runs by category, sort each group by start time (newest first), and order the
  // groups by their most-recent run so active categories float to the top.
  const groups = useMemo<RunGroup[]>(() => {
    const m = new Map<string, WorkflowRunSummary[]>();
    for (const r of runs) {
      const c = runCategory(r);
      if (!m.has(c)) m.set(c, []);
      m.get(c)!.push(r);
    }
    const out: RunGroup[] = [];
    for (const [category, list] of m.entries()) {
      const sorted = [...list].sort((a, b) => runTime(b) - runTime(a));
      out.push({
        category,
        label: CATEGORY_LABELS[category] || category,
        runs: sorted,
        newest: sorted.length ? runTime(sorted[0]) : 0,
      });
    }
    out.sort((a, b) => b.newest - a.newest || a.label.localeCompare(b.label));
    return out;
  }, [runs]);

  const toggle = (cat: string) =>
    setCollapsed((prev) => ({ ...prev, [cat]: !prev[cat] }));

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-title">自主研究控制面</div>
          <div className="brand-sub">inner · outer audit · recursive</div>
        </div>

        <button className="btn primary block" onClick={() => setView("new")}>
          ＋ 新建研究
        </button>

        <button className="btn block" onClick={() => setView("catalog")}>
          📚 任务库
        </button>

        <button className="btn block" onClick={() => setView("leaderboard")}>
          🏆 研究榜单
        </button>

        <button className="btn block" onClick={() => setView("compare")}>
          📊 实验对比
        </button>

        <div className="sidebar-label">研究记录 ({runs.length})</div>
        <div className="run-list">
          {groups.length === 0 && <div className="muted small" style={{ padding: 6 }}>暂无记录</div>}
          {groups.map((g) => {
            const isCollapsed = !!collapsed[g.category];
            return (
              <div key={g.category} className="run-group">
                <button
                  className="run-group-head"
                  onClick={() => toggle(g.category)}
                  title={isCollapsed ? "展开" : "折叠"}
                >
                  <span className={`chev ${isCollapsed ? "collapsed" : ""}`}>▾</span>
                  <span className="run-group-label">{g.label}</span>
                  <span className="run-group-count">{g.runs.length}</span>
                </button>
                {!isCollapsed &&
                  g.runs.map((r) => (
                    <button
                      key={r.run_id}
                      className={`run-item ${runId === r.run_id && view === "run" ? "active" : ""}`}
                      onClick={() => selectRun(r.run_id)}
                    >
                      <span className={`dot ${STATUS_CLASS[r.status] || "accent"}`} />
                      <span className="run-item-name">{runName(r)}</span>
                      <span className="run-item-status muted small">{STATUS_LABEL[r.status] || r.status}</span>
                    </button>
                  ))}
              </div>
            );
          })}
        </div>

        <div className="sidebar-foot muted small">
          后端 :8000 · 前端 :5173
        </div>
      </aside>

      <main className="main">
        {error && <div className="card error">加载失败：{error}</div>}

        {view === "new" && (
          <NewResearch
            initialTaskId={newInitialTaskId}
            onCancel={() => {
              setNewInitialTaskId(null);
              setView(runId ? "run" : "welcome");
            }}
            onLaunch={(rid) => {
              setNewInitialTaskId(null);
              setRunId(rid);
              setView("run");
              refreshRuns();
            }}
          />
        )}

        {view === "catalog" && (
          <BenchmarkCatalog
            onUseTask={(taskId) => {
              setNewInitialTaskId(taskId);
              setView("new");
            }}
          />
        )}

        {view === "leaderboard" && (
          <Leaderboard
            onOpenRun={(rid) => {
              setRunId(rid);
              setView("run");
            }}
            onNewResearch={() => setView("new")}
            taskName={(tid) => taskMap[tid]?.name || tid}
          />
        )}

        {view === "compare" && (
          <CompareRuns
            onOpenRun={(rid) => {
              setRunId(rid);
              setView("run");
            }}
          />
        )}

        {view === "run" && runId && <RunDashboard runId={runId} />}

        {view !== "new" && !runId && (
          <div className="welcome">
            <div className="card hero">
              <h1>自主研究控制面板</h1>
              <p className="muted">
                一个 run = 一次自主研究：<b>内循环</b>做实验优化 → <b>外循环审计</b>独立、无偏地评估
                → 未达门槛则 <b>递归改进</b> 后重试。外循环看不到内循环的实验细节，避免自评偏差。
              </p>
                <ol className="tight" style={{ marginTop: 10 }}>
                  <li>点击左侧 <b>＋ 新建研究</b> 或 <b>📚 任务库</b>，选择任务并配置审计严格度 / 预算 / 内循环设置。</li>
                  <li>平台原生任务（Titanic / Spaceship）会直接驱动双循环并实时展示进度与审计结论。</li>
                  <li>其余开源任务已统一改为 <b>agent 模式执行</b>（剥离 docker / Arbor 依赖）；需平台接入远程 Agent，
                    未接入时启动会明确失败并终止。</li>
                </ol>
              <button className="btn primary" style={{ marginTop: 12 }} onClick={() => setView("new")}>
                ＋ 新建研究
              </button>
            </div>

            {runs.length > 0 && (
              <div className="card" style={{ marginTop: 14 }}>
                <h3>最近研究记录</h3>
                <div className="recent-grid">
                  {[...runs]
                    .sort((a, b) => runTime(b) - runTime(a))
                    .slice(0, 6)
                    .map((r) => (
                      <button key={r.run_id} className="recent-card" onClick={() => selectRun(r.run_id)}>
                        <div className="row">
                          <strong>{runName(r)}</strong>
                          <span className={`pill ${STATUS_CLASS[r.status] || "accent"}`}>
                            {STATUS_LABEL[r.status] || r.status}
                          </span>
                        </div>
                        <div className="muted mono small">
                          {CATEGORY_LABELS[runCategory(r)] || runCategory(r)}
                        </div>
                        <div className="muted mono small">{r.run_id}</div>
                      </button>
                    ))}
                </div>
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
