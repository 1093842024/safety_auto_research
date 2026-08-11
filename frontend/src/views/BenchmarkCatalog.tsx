import React, { useEffect, useMemo, useState } from "react";
import {
  BenchmarkTask,
  getBenchmarkTasks,
  CATEGORY_LABELS,
} from "../api/client";

const dirText = (d: string) =>
  d === "lower" ? "越低越好 ↓" : d === "higher" ? "越高越好 ↑" : d;

// Isolation badge for agent-mode tasks (F3): shows how the task will execute.
const isoBadge = (iso?: string) => {
  switch (iso) {
    case "container-hard":
      return (
        <span className="pill iso" title="Docker 容器硬隔离：数据只读挂载 + 无网络，结果回写 EvalCompletedEvent">
          🐳 容器隔离
        </span>
      );
    case "container-soft":
      return (
        <span className="pill iso-soft" title="Docker 不可用，退回宿主软隔离（同一条研究命令，依赖/目录隔离，无 syscall/网络沙箱）">
          🛡️ 软隔离
        </span>
      );
    case "host":
      return (
        <span className="pill warn" title="未设置 AGENT_SANDBOX，agent 将在宿主直接运行，无隔离">
          ⚠️ 无隔离
        </span>
      );
    default:
      return null;
  }
};

function TaskDetail({ t }: { t: BenchmarkTask }) {
  return (
    <div className="catalog-detail">
      <div className="kv">
        <span className="muted">任务目标</span>
        <span>{t.goal || t.name}</span>
      </div>
      <div className="kv">
        <span className="muted">来源 / 模态</span>
        <span className="mono">{t.source_project} · {t.modality}</span>
      </div>
      <div className="kv">
        <span className="muted">执行方式</span>
        <span>
          {t.execution_mode === "agent" ? (
            <span className="pill warn">agent 模式（已剥离 docker/Arbor 依赖）</span>
          ) : (
            <span className="pill ok">平台原生（双循环直接执行）</span>
          )}{" "}
          {isoBadge(t.sandbox_isolation)}
        </span>
      </div>
      <div className="kv">
        <span className="muted">评测指标</span>
        <span className="mono">{t.eval_metric} · {dirText(t.direction)}</span>
      </div>
      <div className="kv">
        <span className="muted">基线 / 参考</span>
        <span className="mono">baseline={t.baseline ?? "—"} · reference={t.reference ?? "—"}</span>
      </div>
      <div className="kv">
        <span className="muted">通过门限</span>
        <span className="mono">{Object.keys(t.gates || {}).length ? JSON.stringify(t.gates) : "无"}</span>
      </div>
      <div className="kv">
        <span className="muted">数据 / 定义</span>
        <span>{t.dataset_desc}</span>
      </div>
      <div className="kv">
        <span className="muted">评估方式</span>
        <span>{t.eval_method}</span>
      </div>
      <div className="kv">
        <span className="muted">原始 harness</span>
        <span className="mono small">{t.harness}</span>
      </div>
      <div className="kv">
        <span className="muted">原始运行命令（参考）</span>
        <span><pre className="cmd">{t.run_command}</pre></span>
      </div>
      <div className="kv">
        <span className="muted">源路径</span>
        <span className="mono small">{t.source_path}</span>
      </div>
      <div className="tags">
        {t.tags.map((tag) => (
          <span key={tag} className="tag">#{tag}</span>
        ))}
      </div>
      {t.note && <p className="muted small" style={{ marginTop: 8 }}>{t.note}</p>}
    </div>
  );
}

export function BenchmarkCatalog({ onUseTask }: { onUseTask: (taskId: string) => void }) {
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [error, setError] = useState("");
  const [cat, setCat] = useState<string>("");
  const [q, setQ] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    getBenchmarkTasks()
      .then(setTasks)
      .catch((e) => setError(String(e?.message || e)));
  }, []);

  const cats = useMemo(() => {
    const s = new Set(tasks.map((t) => t.category));
    return [...s];
  }, [tasks]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return tasks.filter((t) => {
      if (cat && t.category !== cat) return false;
      if (!kw) return true;
      return (
        t.name.toLowerCase().includes(kw) ||
        t.task_id.toLowerCase().includes(kw) ||
        (t.dataset_desc || "").toLowerCase().includes(kw) ||
        (t.tags || []).some((tag) => tag.toLowerCase().includes(kw))
      );
    });
  }, [tasks, cat, q]);

  const grouped = useMemo(() => {
    const m = new Map<string, BenchmarkTask[]>();
    for (const t of filtered) {
      if (!m.has(t.category)) m.set(t.category, []);
      m.get(t.category)!.push(t);
    }
    return [...m.entries()];
  }, [filtered]);

  return (
    <div className="catalog">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>Benchmark 任务库</h1>
        <span className="muted">{tasks.length} 个内置研究任务</span>
      </div>
      <p className="muted" style={{ marginTop: 6 }}>
        内置研究任务的完整信息：目标、定义、数据、评估方式、指标与基线、执行方式。
        需要 docker / Arbor / Harbor 的任务已统一改为 <b>agent 模式执行</b>（仅保留核心信息，剥离外部依赖）；
        训练/评测数据体积 <b>&gt;1GB</b>、依赖外部兄弟仓库或基座模型权重 &gt;1GB 的任务已<b>置灰（⛔ 不可用）</b>，
        不参与端到端评测与训练优化。
      </p>

      <div className="row" style={{ gap: 10, marginTop: 10, flexWrap: "wrap" }}>
        <input
          className="search"
          placeholder="搜索任务名 / ID / 标签 / 数据描述…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, minWidth: 240 }}
        />
        <button className={`chip ${cat === "" ? "chip-on" : ""}`} onClick={() => setCat("")}>全部</button>
        {cats.map((c) => (
          <button key={c} className={`chip ${cat === c ? "chip-on" : ""}`} onClick={() => setCat(c)}>
            {CATEGORY_LABELS[c] || c}
          </button>
        ))}
      </div>

      {error && <div className="card error" style={{ marginTop: 10 }}>加载失败：{error}</div>}

      <div className="catalog-list" style={{ marginTop: 14 }}>
        {grouped.length === 0 && <div className="muted">无匹配任务</div>}
        {grouped.map(([c, items]) => (
          <div key={c} className="catalog-group">
            <h3 style={{ borderBottom: "1px solid var(--border)", paddingBottom: 4 }}>
              {CATEGORY_LABELS[c] || c} <span className="muted">({items.length})</span>
            </h3>
            {items.map((t) => {
              const disabled = t.enabled === false;
              return (
              <div key={t.task_id} className={`catalog-card ${expanded === t.task_id ? "open" : ""} ${disabled ? "disabled" : ""}`}>
                <div className="row catalog-head">
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => setExpanded(expanded === t.task_id ? null : t.task_id)}
                  >
                    <span className={`chev ${expanded === t.task_id ? "collapsed" : ""}`}>▾</span>
                    <strong>{t.name}</strong>
                  </button>
                  <span className="muted mono small">{t.task_id}</span>
                  {disabled ? (
                    <span className="pill gray" title={t.unavailable_reason}>⛔ 已置灰（不可用）</span>
                  ) : t.execution_mode === "agent" ? (
                    <span className="pill warn">agent 模式</span>
                  ) : (
                    <span className="pill ok">平台原生</span>
                  )}{" "}
                  {!disabled && isoBadge(t.sandbox_isolation)}
                  <button
                    className="btn tiny primary"
                    disabled={disabled}
                    title={disabled ? t.unavailable_reason : undefined}
                    onClick={() => onUseTask(t.task_id)}
                  >
                    用此任务新建研究 →
                  </button>
                </div>
                <div className="muted mono small" style={{ margin: "2px 0 6px 18px" }}>
                  指标 {t.eval_metric} · {dirText(t.direction)} · baseline={t.baseline ?? "—"}
                </div>
                {disabled && t.unavailable_reason && (
                  <div className="muted small" style={{ margin: "0 0 6px 18px", color: "var(--warn, #b26a00)" }}>
                    不可用原因：{t.unavailable_reason}
                  </div>
                )}
                {expanded === t.task_id && <TaskDetail t={t} />}
              </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
