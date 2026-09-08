import React, { useCallback, useState } from "react";
import {
  createWorkflowRun,
  runFlywheel,
  getFlywheelIterations,
  FlywheelIteration,
  FlywheelResult,
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

/** B 飞轮型控制台：冻结方案 → badcase 回流 → 回放重训 → 回归门（不退化护栏）。 */
export function FlywheelPanel() {
  const [preset, setPreset] = useState("titanic");
  const [model, setModel] = useState("gbm");
  const [evalMetric, setEvalMetric] = useState("accuracy");
  const [badcaseRatio, setBadcaseRatio] = useState(0.3);
  const [regressionTol, setRegressionTol] = useState(0.0);

  const [runId, setRunId] = useState<string>("");
  const [iterations, setIterations] = useState<FlywheelIteration[]>([]);
  const [lastResult, setLastResult] = useState<FlywheelResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const refreshIterations = useCallback(async (rid: string) => {
    const iters = await getFlywheelIterations(rid);
    setIterations(iters);
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
          方案冻结、数据飞轮：每轮自动采集坏例 → 按 <code>badcase:original</code> 配比回放重训 →
          在冻结的原始评测集上做<b>回归门</b>（不退化护栏）。坏例召回提升是收益，回归不退化是硬约束。
        </p>

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
                {running ? "运行中…" : "启动飞轮"}
              </button>
            ) : (
              <button className="btn primary" onClick={iterateAgain} disabled={running}>
                {running ? "运行中…" : "再跑一轮"}
              </button>
            )}
          </div>
        </div>

        {error && (
          <div className="card error" style={{ marginTop: 12 }}>运行失败：{error}</div>
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
