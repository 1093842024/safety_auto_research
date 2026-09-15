import React, { useCallback, useEffect, useState } from "react";
import {
  createWorkflowRun,
  runFlywheel,
  getFlywheelIterations,
  getResearchRecords,
  ResearchRecord,
  FlywheelIteration,
  FlywheelResult,
  startFlywheelScheduler,
  getFlywheelScheduler,
  stopFlywheelScheduler,
  FlywheelSchedulerStatus,
} from "../api/client";

const PRESETS = [
  { value: "titanic", label: "Titanic" },
  { value: "spaceship", label: "Spaceship-Titanic" },
  { value: "iris", label: "Iris" },
  { value: "wine", label: "Wine" },
  { value: "breast_cancer", label: "Breast Cancer" },
];

const MODELS = [
  { value: "gbm", label: "GradientBoosting" },
  { value: "hgb", label: "HistGradientBoosting" },
  { value: "rf", label: "RandomForest" },
  { value: "logreg", label: "LogisticRegression" },
];

const METRICS = [
  { value: "accuracy", label: "accuracy" },
  { value: "f1_macro", label: "f1_macro" },
];

// 研究记录 task_id → 飞轮 preset（记录可作为基线的平台原生任务）。
const TASK_PRESET: Record<string, string> = {
  "platform.titanic": "titanic",
  "platform.spaceship": "spaceship",
  "platform.iris": "iris",
  "platform.wine": "wine",
  "platform.breast_cancer": "breast_cancer",
};

/** B 飞轮型控制台：选定已研究过的记录作为基线版本 → badcase 回流 → 回放重训 → 回归门。 */
export function FlywheelPanel() {
  const [preset, setPreset] = useState("titanic");
  const [model, setModel] = useState("gbm");
  const [evalMetric, setEvalMetric] = useState("accuracy");
  const [badcaseRatio, setBadcaseRatio] = useState(0.3);
  const [regressionTol, setRegressionTol] = useState(0.0);

  // 基线版本：从历史研究记录中选取（其配置快照即冻结的基础算法/模型方案）。
  const [baselineRecords, setBaselineRecords] = useState<ResearchRecord[]>([]);
  const [baselineId, setBaselineId] = useState<string>("");

  const [runId, setRunId] = useState<string>("");
  const [iterations, setIterations] = useState<FlywheelIteration[]>([]);
  const [lastResult, setLastResult] = useState<FlywheelResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  // Scheduler (event-driven auto-trigger) state.
  const [badcasePath, setBadcasePath] = useState("");
  const [threshold, setThreshold] = useState(50);
  const [pollIntervalSec, setPollIntervalSec] = useState(30);
  const [autoClear, setAutoClear] = useState(true);
  const [schedulerStatus, setSchedulerStatus] = useState<FlywheelSchedulerStatus | null>(null);
  const [schedulerError, setSchedulerError] = useState("");
  const [schedulerBusy, setSchedulerBusy] = useState(false);

  const refreshIterations = useCallback(async (rid: string) => {
    const iters = await getFlywheelIterations(rid);
    setIterations(iters);
  }, []);

  // 基线候选：有配置快照且任务映射到飞轮支持的数据集的历史研究记录。
  useEffect(() => {
    getResearchRecords()
      .then((recs) => {
        setBaselineRecords(
          recs.filter((r) => TASK_PRESET[r.task_id] && r.config_snapshot),
        );
      })
      .catch(() => {
        /* 无记录时选择器自然为空，手动配置仍可用 */
      });
  }, []);

  const applyBaseline = (recordId: string) => {
    setBaselineId(recordId);
    const rec = baselineRecords.find((r) => r.record_id === recordId);
    if (!rec) return;
    // 记录的配置快照 = 冻结的基础算法/模型版本。
    const p = TASK_PRESET[rec.task_id];
    if (p) setPreset(p);
    const inner = rec.config_snapshot?.inner_loop || rec.config_snapshot || {};
    if (MODELS.some((m) => m.value === inner.model)) setModel(inner.model);
    if (String(rec.metric_name).includes("f1")) setEvalMetric("f1_macro");
    else if (String(rec.metric_name).includes("acc")) setEvalMetric("accuracy");
  };

  const refreshScheduler = useCallback(async (rid: string) => {
    try {
      const s = await getFlywheelScheduler(rid);
      setSchedulerStatus(s);
      setSchedulerError("");
    } catch (e: any) {
      setSchedulerError(String(e?.message || e));
    }
  }, []);

  const doIteration = async (rid: string) => {
    const res = await runFlywheel(rid, {
      preset,
      model,
      eval_metric: evalMetric,
      badcase_ratio: badcaseRatio,
      regression_tol: regressionTol,
    });
    setLastResult(res);
    await refreshIterations(rid);
  };

  const start = async () => {
    setRunning(true);
    setError("");
    try {
      // Spin up a fresh FLYWHEEL run, then run one iteration on it.
      const created = await createWorkflowRun({
        program_id: "flywheel",
        run_type: "flywheel",
        entry_stage: "badcase_retrain",
        target_id: `flywheel-${preset}`,
        objective_snapshot: { name: `飞轮 · ${preset}`, eval_metric: evalMetric },
      });
      setRunId(created.run_id);
      await doIteration(created.run_id);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setRunning(false);
    }
  };

  const iterateAgain = async () => {
    if (!runId) return;
    setRunning(true);
    setError("");
    try {
      await doIteration(runId);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setRunning(false);
    }
  };

  // ---- Scheduler handlers ----
  const startScheduler = async () => {
    if (!runId) {
      setSchedulerError("请先启动飞轮（创建一个 run）");
      return;
    }
    if (!badcasePath.trim()) {
      setSchedulerError("badcase_path 必填（指向将被追加坏例的 CSV 文件）");
      return;
    }
    setSchedulerBusy(true);
    setSchedulerError("");
    try {
      await startFlywheelScheduler(runId, {
        badcase_path: badcasePath.trim(),
        threshold,
        poll_interval_sec: pollIntervalSec,
        auto_clear_after_trigger: autoClear,
        preset,
        model,
        eval_metric: evalMetric,
        badcase_ratio: badcaseRatio,
        regression_tol: regressionTol,
      });
      await refreshScheduler(runId);
      await refreshIterations(runId);
    } catch (e: any) {
      setSchedulerError(String(e?.message || e));
    } finally {
      setSchedulerBusy(false);
    }
  };

  const stopScheduler = async () => {
    if (!runId) return;
    setSchedulerBusy(true);
    setSchedulerError("");
    try {
      await stopFlywheelScheduler(runId);
      await refreshScheduler(runId);
      await refreshIterations(runId);
    } catch (e: any) {
      setSchedulerError(String(e?.message || e));
    } finally {
      setSchedulerBusy(false);
    }
  };

  // Poll scheduler status while it's running (cheap read).
  useEffect(() => {
    if (!runId || !schedulerStatus?.scheduled) return;
    let alive = true;
    const t = setInterval(() => {
      if (!alive) return;
      refreshScheduler(runId).then(() => {
        if (!alive) return;
        // While the scheduler fires, the iteration list grows too.
        refreshIterations(runId).catch(() => {
          /* swallow — status card already shows the truth */
        });
      });
    }, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId, schedulerStatus?.scheduled, refreshScheduler, refreshIterations]);

  const fmt = (v: number | undefined, digits = 4) =>
    typeof v === "number" ? v.toFixed(digits) : "—";

  const latestBadcaseAcc = iterations.length
    ? iterations[iterations.length - 1].metrics.retrained_badcase_acc
    : undefined;

  return (
    <div className="flywheel">
      <div className="card">
        <h2>🔄 数据飞轮（B 飞轮型）</h2>
        <p className="muted">
          选择一个已研究过的研究记录作为<b>基础算法/模型版本</b>（其配置快照即冻结方案）→
          每轮自动采集坏例 → 按 <code>badcase:original</code> 配比回放重训 →
          在冻结的原始评测集上做<b>回归门</b>（不退化护栏）。坏例召回提升是收益，回归不退化是硬约束。
        </p>

        {/* ---- 基线版本选择：以历史研究记录为起点 ---- */}
        <div className="row" style={{ marginTop: 12, gap: 14, alignItems: "flex-end", flexWrap: "wrap" }}>
          <div className="field" style={{ minWidth: 340, flex: 1 }}>
            <label>基线版本（从已研究过的记录中选择）</label>
            <select value={baselineId} onChange={(e) => applyBaseline(e.target.value)}>
              <option value="">手动配置（不使用历史记录）</option>
              {baselineRecords.map((r) => (
                <option key={r.record_id} value={r.record_id}>
                  {r.task_id} · {r.metric_name}={r.score} · {r.created_at?.slice(0, 16).replace("T", " ")}
                </option>
              ))}
            </select>
          </div>
          {baselineId && (
            <span className="pill ok" style={{ marginBottom: 6 }}>
              已选定基线：{TASK_PRESET[baselineRecords.find((r) => r.record_id === baselineId)?.task_id || ""]}
            </span>
          )}
        </div>

        <div className="row" style={{ marginTop: 12, gap: 14, alignItems: "flex-end" }}>
          <div className="field">
            <label>数据集</label>
            <select value={preset} onChange={(e) => setPreset(e.target.value)}>
              {PRESETS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>冻结模型方案</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {MODELS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>回归指标</label>
            <select value={evalMetric} onChange={(e) => setEvalMetric(e.target.value)}>
              {METRICS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>badcase 配比 (0.05–0.9)</label>
            <input
              type="number"
              step="0.05"
              min="0.05"
              max="0.9"
              value={badcaseRatio}
              onChange={(e) => setBadcaseRatio(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label>回归容差 tol</label>
            <input
              type="number"
              step="0.01"
              min="0"
              value={regressionTol}
              onChange={(e) => setRegressionTol(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label>&nbsp;</label>
            {!runId ? (
              <button className="btn primary" onClick={start} disabled={running}>
                {running ? "运行中..." : "启动飞轮"}
              </button>
            ) : (
              <button className="btn primary" onClick={iterateAgain} disabled={running}>
                {running ? "运行中..." : "再跑一轮"}
              </button>
            )}
          </div>
        </div>

        {error && (
          <div className="card error" style={{ marginTop: 12 }}>运行失败：{error}</div>
        )}
      </div>

      {/* ============================================================== */}
      {/* Event-driven scheduler: poll a badcase CSV, auto-fire iteration */}
      {/* ============================================================== */}
      <div className="card">
        <h2>⏰ 事件驱动调度（auto-trigger）</h2>
        <p className="muted">
          外部业务把坏例持续 append 到 <code>badcase_path</code>（CSV）→ 当行数 ≥ <code>threshold</code>，
          调度器自动跑一轮 <code>badcase_retrain</code>（沿用上方数据集 / 模型 / 回归指标 / 配比 / 容差）。
          调度器与飞轮 run 一对一，启动前需先点击"启动飞轮"创建 run。
        </p>

        <div className="row" style={{ marginTop: 10, gap: 14, alignItems: "flex-end", flexWrap: "wrap" }}>
          <div className="field" style={{ minWidth: 320, flex: 1 }}>
            <label>badcase_path（持续追加坏例的 CSV）</label>
            <input
              type="text"
              value={badcasePath}
              placeholder="/abs/path/to/badcase.csv"
              onChange={(e) => setBadcasePath(e.target.value)}
            />
          </div>
          <div className="field" style={{ width: 120 }}>
            <label>threshold（行数）</label>
            <input
              type="number"
              step="1"
              min="1"
              value={threshold}
              onChange={(e) => setThreshold(Math.max(1, Number(e.target.value)))}
            />
          </div>
          <div className="field" style={{ width: 140 }}>
            <label>poll_interval_sec</label>
            <input
              type="number"
              step="1"
              min="1"
              max="3600"
              value={pollIntervalSec}
              onChange={(e) => setPollIntervalSec(Math.max(1, Number(e.target.value)))}
            />
          </div>
          <div className="field" style={{ width: 200 }}>
            <label>触发后清空 CSV</label>
            <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={autoClear}
                onChange={(e) => setAutoClear(e.target.checked)}
              />
              <span className="muted small">auto_clear_after_trigger</span>
            </label>
          </div>
          <div className="field">
            <label>&nbsp;</label>
            <div className="row" style={{ gap: 6 }}>
              {!schedulerStatus?.scheduled ? (
                <button
                  className="btn primary"
                  onClick={startScheduler}
                  disabled={!runId || schedulerBusy}
                  title={!runId ? "先启动飞轮" : ""}
                >
                  {schedulerBusy ? "启动中..." : "▶ 启动调度"}
                </button>
              ) : (
                <button
                  className="btn"
                  onClick={stopScheduler}
                  disabled={schedulerBusy}
                >
                  {schedulerBusy ? "停止中..." : "■ 停止调度"}
                </button>
              )}
              <button className="btn tiny" onClick={() => runId && refreshScheduler(runId)}>
                刷新状态
              </button>
            </div>
          </div>
        </div>

        {schedulerError && (
          <div className="card error" style={{ marginTop: 10 }}>调度错误：{schedulerError}</div>
        )}

        {schedulerStatus && (
          <div style={{ marginTop: 14 }}>
            <div className="row" style={{ gap: 8, marginBottom: 8 }}>
              <span className={`pill ${schedulerStatus.scheduled ? "ok" : "warn"}`}>
                {schedulerStatus.scheduled ? "RUNNING · 监听中" : "STOPPED · 未运行"}
              </span>
              {typeof schedulerStatus.trigger_count === "number" && (
                <span className="pill gray">已触发 {schedulerStatus.trigger_count} 次</span>
              )}
              {schedulerStatus.last_verdict && (
                <span
                  className={`pill ${
                    schedulerStatus.last_verdict === "ACCEPT" ? "ok" : "bad"
                  }`}
                >
                  最近：{schedulerStatus.last_verdict}
                </span>
              )}
              {schedulerStatus.error && (
                <span className="pill bad">err: {schedulerStatus.error.slice(0, 60)}</span>
              )}
            </div>
            <div className="grid2">
              <div>
                <div className="kv">
                  <span className="muted">run_id</span>
                  <span className="mono small">{schedulerStatus.run_id}</span>
                </div>
                <div className="kv">
                  <span className="muted">started_at</span>
                  <span className="mono small">
                    {schedulerStatus.started_at
                      ? new Date(schedulerStatus.started_at * 1000).toLocaleString()
                      : "—"}
                  </span>
                </div>
                <div className="kv">
                  <span className="muted">last_check_at</span>
                  <span className="mono small">
                    {schedulerStatus.last_check_at
                      ? new Date(schedulerStatus.last_check_at * 1000).toLocaleString()
                      : "—"}
                  </span>
                </div>
              </div>
              <div>
                <div className="kv">
                  <span className="muted">last_badcase_count</span>
                  <span className="mono">{schedulerStatus.last_badcase_count ?? "—"}</span>
                </div>
                <div className="kv">
                  <span className="muted">last_trigger_at</span>
                  <span className="mono small">
                    {schedulerStatus.last_trigger_at
                      ? new Date(schedulerStatus.last_trigger_at * 1000).toLocaleString()
                      : "—"}
                  </span>
                </div>
                <div className="kv">
                  <span className="muted">last_detail</span>
                  <span className="mono small">
                    {schedulerStatus.last_detail?.slice(0, 80) || "—"}
                  </span>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {lastResult && (
        <div className="card">
          <h2>本轮结果</h2>
          <div className="row" style={{ marginBottom: 8 }}>
            <span className={`pill ${lastResult.verdict === "ACCEPT" ? "ok" : "bad"}`}>
              {lastResult.verdict === "ACCEPT" ? "ACCEPT · 接受新模型" : "REJECT · 拒绝新模型"}
            </span>
            <span className={`pill ${lastResult.regression_passed ? "ok" : "bad"}`}>
              回归门 {lastResult.regression_passed ? "通过（不退化）" : "失败（退化）"}
            </span>
            <span className={`pill ${lastResult.badcase_improved ? "ok" : "warn"}`}>
              坏例召回 {lastResult.badcase_improved ? "提升" : "未提升"}
            </span>
            {lastResult.badcase_collected && <span className="pill gray">坏例自动采集</span>}
          </div>
          <div className="grid2">
            <div>
              <h3>坏例召回（badcase accuracy）</h3>
              <div className="kv">
                <span className="muted">基线</span>
                <span className="mono">{fmt(lastResult.metrics.baseline_badcase_acc)}</span>
              </div>
              <div className="kv">
                <span className="muted">重训后</span>
                <span className="mono">{fmt(lastResult.metrics.retrained_badcase_acc)}</span>
              </div>
              <div className="kv">
                <span className="muted">坏例数量</span>
                <span className="mono">{lastResult.metrics.badcase_count ?? "—"}</span>
              </div>
            </div>
            <div>
              <h3>回归指标（{evalMetric}，冻结原始集）</h3>
              <div className="kv">
                <span className="muted">基线</span>
                <span className="mono">{fmt(lastResult.metrics.baseline_primary)}</span>
              </div>
              <div className="kv">
                <span className="muted">重训后</span>
                <span className="mono">{fmt(lastResult.metrics.retrained_primary)}</span>
              </div>
              <div className="kv">
                <span className="muted">Δ（不退化护栏）</span>
                <span className="mono">
                  {fmt((lastResult.metrics.retrained_primary ?? 0) - (lastResult.metrics.baseline_primary ?? 0))}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}

      {iterations.length > 0 && (
        <div className="card">
          <h2>飞轮历史 · 坏例覆盖率曲线</h2>
          <p className="muted">
            每轮重训后的坏例召回（badcase accuracy）。回归门失败的轮次会被拒绝、不计入改进。
            调度器自动触发的轮次与人工"再跑一轮"产生的轮次并列出现在同一条曲线。
          </p>
          <div className="compare-chart" style={{ marginTop: 10 }}>
            {iterations.map((it, i) => {
              const acc = it.metrics.retrained_badcase_acc ?? 0;
              const pct = Math.max(0, Math.min(100, acc * 100));
              return (
                <div key={it.event_id} className="row" style={{ gap: 12 }}>
                  <span className="mono small" style={{ width: 46 }}>第 {i + 1} 轮</span>
                  <div className="bar" style={{ flex: 1 }}>
                    <span style={{ width: `${pct}%`, background: it.gate_passed ? "var(--ok)" : "var(--bad)" }} />
                  </div>
                  <span className="mono small" style={{ width: 72, textAlign: "right" }}>
                    {fmt(acc)}
                  </span>
                  <span className={`pill ${it.gate_passed ? "ok" : "bad"}`}>
                    {it.gate_passed ? "ACCEPT" : "REJECT"}
                  </span>
                </div>
              );
            })}
          </div>
          {typeof latestBadcaseAcc === "number" && (
            <div className="muted small" style={{ marginTop: 10 }}>
              最新坏例召回：<b className="mono">{fmt(latestBadcaseAcc)}</b>（{iterations.length} 轮）
            </div>
          )}
        </div>
      )}
    </div>
  );
}