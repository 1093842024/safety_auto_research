import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getRuns, getBenchmarkTasks, BenchmarkTask, WorkflowRunSummary, STATUS_LABEL, STATUS_CLASS, categoryLabel, categoryGroup, setSchemaViolationHandler } from "./api/client";
import { ToastProvider, useToast } from "./components/Toast";
import { ConfirmProvider } from "./components/ConfirmDialog";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { NewResearch } from "./views/NewResearch";
import { RunDashboard } from "./views/RunDashboard";
import { BenchmarkCatalog } from "./views/BenchmarkCatalog";
import { ResearchRecords } from "./views/ResearchRecords";
import { Leaderboard } from "./views/Leaderboard";
import { CompareRuns } from "./views/CompareRuns";
import { FlywheelPanel } from "./views/FlywheelPanel";
import { LlmPanel } from "./views/LlmPanel";
import { SystemSettings } from "./views/SystemSettings";
import { DatasetManager } from "./views/DatasetManager";
import { MetricCatalogView } from "./views/MetricCatalog";
import { ResearchSkills } from "./views/ResearchSkills";

const CAT_FALLBACK = "未分类";

const runCategory = (r: WorkflowRunSummary) =>
  categoryGroup(r.objective_snapshot?.category || CAT_FALLBACK);

const runTime = (r: WorkflowRunSummary): number => {
  const t = r.started_at;
  if (!t) return 0;
  const ms = Date.parse(t);
  return Number.isNaN(ms) ? 0 : ms;
};

export function App() {
  return (
    <ToastProvider>
      <ConfirmProvider>
        <ErrorBoundary>
          <AppBody />
        </ErrorBoundary>
      </ConfirmProvider>
    </ToastProvider>
  );
}

function AppBody() {
  const { push } = useToast();
  // Surface backend schema-drift warnings as toasts (P2 frontend fix).
  useEffect(() => {
    setSchemaViolationHandler((msg) => push(msg, "warn"));
    return () => setSchemaViolationHandler(null);
  }, [push]);

  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [taskMap, setTaskMap] = useState<Record<string, BenchmarkTask>>({});
  const [runId, setRunId] = useState<string>("");
  const [view, setView] = useState<
  | "welcome"
  | "new"
  | "run"
  | "catalog"
  | "records"
  | "leaderboard"
  | "compare"
  | "flywheel"
  | "llm"
  | "settings"
  | "datasets"
  | "metrics"
  | "skills"
>("welcome");
  const [error, setError] = useState<string>("");
  // When jumping from the catalog to "new research", pre-select this task.
  const [newInitialTaskId, setNewInitialTaskId] = useState<string | null>(null);
  // When jumping from a run's detail page to the catalog, auto-expand this task.
  const [catalogExpandTaskId, setCatalogExpandTaskId] = useState<string | null>(null);
  // UI1: distinguish "first load in progress" from "backend unreachable after we
  // already had data" — the latter should keep showing cached runs instead of a hard error.
  const [loading, setLoading] = useState(true);
  const [backendDown, setBackendDown] = useState(false);
  const loadedOnceRef = useRef(false);

  const refreshRuns = useCallback(async () => {
    try {
      const r = await getRuns();
      setRuns(r);
      setRunId((prev) => (prev ? prev : r.length ? r[r.length - 1].run_id : prev));
      setBackendDown(false);
      setError("");
    } catch (e: any) {
      // If we already had data, keep showing it and just flag the backend as
      // unreachable (silent retry). Otherwise surface a hard error on first load.
      if (loadedOnceRef.current) {
        setBackendDown(true);
      } else {
        setError(String(e?.message || e));
      }
    } finally {
      loadedOnceRef.current = true;
      setLoading(false);
    }
  }, []);

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
  }, [refreshRuns]);

  const selectRun = (id: string) => {
    setRunId(id);
    setView("run");
  };

  const runName = (r: WorkflowRunSummary) => {
    const known = !!taskMap[r.target_id];
    const base =
      taskMap[r.target_id]?.name || r.objective_snapshot?.name || r.target_id;
    // 任务已不在任务库中（被删除/测试目标）时明确标注，避免无意义名称引起困惑。
    return known || r.objective_snapshot?.name ? base : `${base}（未注册任务）`;
  };

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

        <button className="btn block" onClick={() => setView("records")}>
          📋 研究记录
        </button>

        <button className="btn block" onClick={() => setView("leaderboard")}>
          🏆 研究榜单
        </button>

        <button className="btn block" onClick={() => setView("compare")}>
          📊 实验对比
        </button>

        <button className="btn block" onClick={() => setView("flywheel")}>
          🔄 数据飞轮
        </button>

        <button className="btn block" onClick={() => setView("datasets")}>
          🗂 数据集管理
        </button>

        <button className="btn block" onClick={() => setView("metrics")}>
          📏 评估指标
        </button>

        <button className="btn block" onClick={() => setView("skills")}>
          🧩 研究 Skill
        </button>

        <button className="btn block" onClick={() => setView("settings")}>
          ⚙ 系统设置
        </button>

        <div className="sidebar-spacer" />

        <div className="sidebar-foot muted small">
          后端 :8000 · 前端 :5173
        </div>
      </aside>

      <main className="main">
        {error && (
          <div className="card error">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <span>加载失败：{error}</span>
              <button className="btn tiny" onClick={refreshRuns}>重试</button>
            </div>
          </div>
        )}

        {backendDown && (
          <div className="card warn-banner">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <span>⚠ 无法连接到后端 (:8000)，当前显示的是上次缓存的数据。正在自动重试…</span>
              <button className="btn tiny" onClick={refreshRuns}>立即重试</button>
            </div>
          </div>
        )}

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
            initialExpandedTaskId={catalogExpandTaskId}
            onUseTask={(taskId) => {
              setNewInitialTaskId(taskId);
              setView("new");
            }}
          />
        )}

        {view === "records" && (
          <ResearchRecords
            runs={runs}
            nameOf={runName}
            loading={loading}
            onOpenRun={selectRun}
            onNewResearch={() => setView("new")}
            onDeleted={refreshRuns}
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

        {view === "flywheel" && <FlywheelPanel />}

        {view === "llm" && <LlmPanel />}

        {view === "settings" && <SystemSettings />}

        {view === "datasets" && <DatasetManager />}

        {view === "metrics" && <MetricCatalogView />}

        {view === "skills" && <ResearchSkills />}

        {view === "run" && runId && (
          <RunDashboard
            runId={runId}
            onBack={() => setView("records")}
            onDeleted={refreshRuns}
            onOpenTask={(taskId) => {
              if (!taskId) return;
              setCatalogExpandTaskId(taskId);
              setView("catalog");
            }}
          />
        )}

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
                          {categoryLabel(runCategory(r))}
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
