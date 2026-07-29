import React, { useEffect, useMemo, useState } from "react";
import { BenchmarkTask, getBenchmarkTasks, launchBenchmarkTask } from "../api/client";

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

function MetricLine({ t }: { t: BenchmarkTask }) {
  const dir = t.direction === "lower" ? "越低越好 ↓" : t.direction === "higher" ? "越高越好 ↑" : t.direction;
  return (
    <div className="metrics">
      <span className="pill accent">指标 {t.eval_metric}</span>
      <span className="muted">{dir}</span>
      {t.baseline !== null && <span className="muted mono">baseline={t.baseline}</span>}
      {t.reference !== null && <span className="muted mono">ref={t.reference}</span>}
      {Object.keys(t.gates).length > 0 && (
        <span className="muted mono">gates: {JSON.stringify(t.gates)}</span>
      )}
    </div>
  );
}

export function BenchmarkTasks({ onLaunch }: { onLaunch: (runId: string) => void }) {
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [launched, setLaunched] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const t = await getBenchmarkTasks();
        if (alive) setTasks(t);
      } catch (e: any) {
        if (alive) setError(String(e?.message || e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const grouped = useMemo(() => {
    const m = new Map<string, BenchmarkTask[]>();
    for (const t of tasks) {
      if (!m.has(t.category)) m.set(t.category, []);
      m.get(t.category)!.push(t);
    }
    return m;
  }, [tasks]);

  const handleLaunch = async (t: BenchmarkTask) => {
    setBusy(t.task_id);
    setError("");
    try {
      const r = await launchBenchmarkTask(t.task_id);
      setLaunched(r.run_id);
      onLaunch(r.run_id);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  };

  if (error && !tasks.length) return <div className="card error">加载失败：{error}</div>;
  if (!tasks.length) return <div className="card muted">可选研究任务目录为空。</div>;

  return (
    <div className="card">
      <h2>可选研究任务 / Benchmark</h2>
      <p className="muted">
        从项目下的开源项目（autolab / claudini / Arbor / AutoResearchClaw / ARA / Auto-claude / MLEvolve）
        中整理出的、具备明确数据集与评测指标的任务。点击「启动研究」即作为一次 workflow run 进入平台；
        <span className="pill ok">可实跑</span> 的任务（Titanic / Spaceship）会直接驱动双循环。
      </p>
      {error && <div className="card error" style={{ marginTop: 8 }}>操作失败：{error}</div>}
      {launched && (
        <div className="card ok" style={{ marginTop: 8 }}>
          已创建 run <span className="mono">{launched}</span>，已切换到该 run 的双循环视图。
        </div>
      )}

      {[...grouped.entries()].map(([cat, items]) => (
        <div key={cat} style={{ marginTop: 14 }}>
          <h3 style={{ borderBottom: "1px solid var(--border)", paddingBottom: 4 }}>
            {CATEGORY_LABELS[cat] || cat} <span className="muted">({items.length})</span>
          </h3>
          <div className="task-grid">
            {items.map((t) => (
              <div key={t.task_id} className="task-card">
                <div className="row">
                  <strong>{t.name}</strong>
                  {t.supported_by_platform && <span className="pill ok">可实跑</span>}
                </div>
                <div className="muted mono">{t.task_id}</div>
                <div className="muted">来源：{t.source_project} · {t.modality}</div>
                <MetricLine t={t} />
                <p className="desc">{t.dataset_desc}</p>
                <div className="tags">
                  {t.tags.map((tag) => (
                    <span key={tag} className="tag">#{tag}</span>
                  ))}
                </div>
                <div className="mono muted small">harness: {t.harness}</div>
                <pre className="cmd">{t.run_command}</pre>
                {t.note && <div className="muted small">ⓘ {t.note}</div>}
                <button
                  className="btn"
                  disabled={busy === t.task_id}
                  onClick={() => handleLaunch(t)}
                >
                  {busy === t.task_id ? "启动中…" : t.supported_by_platform ? "启动研究(双循环)" : "启动研究"}
                </button>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
