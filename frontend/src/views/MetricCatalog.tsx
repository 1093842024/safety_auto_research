import React, { useEffect, useMemo, useState } from "react";
import { MetricInfo, getMetricCatalog } from "../api/client";

const dirText = (d: string) =>
  d === "lower" ? "越低越好 ↓" : d === "higher" ? "越高越好 ↑" : d;

/** 分组：按指标适用的任务域聚合，便于浏览。 */
const GROUPS: Array<{ key: string; label: string; match: (m: MetricInfo) => boolean }> = [
  {
    key: "classification",
    label: "分类指标",
    match: (m) =>
      ["cv_accuracy", "accuracy", "f1_macro", "f1", "roc_auc", "heldout_accuracy", "generalization_gap"].includes(m.metric_id),
  },
  {
    key: "system",
    label: "系统性能 / 效率",
    match: (m) =>
      ["runtime_seconds", "runtime_ms", "ref_step_latency_s", "speedup", "total_params", "bits_per_byte", "serving_score"].includes(m.metric_id),
  },
  {
    key: "safety",
    label: "对抗 / 安全",
    match: (m) => m.metric_id === "asr",
  },
  {
    key: "agent",
    label: "Agent 评测",
    match: (m) =>
      ["success_rate", "medal_rate", "any_medal_percentage", "trigger_rate", "rubric_weighted_score", "absolute_correctness_success_rate", "max_abs_error", "recall_at_10"].includes(m.metric_id),
  },
];

/** 评估指标页：全部指标的详细介绍、计算方式与标准参考实现。 */
export function MetricCatalogView() {
  const [metrics, setMetrics] = useState<MetricInfo[]>([]);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    getMetricCatalog()
      .then(setMetrics)
      .catch((e) => setError(String(e?.message || e)));
  }, []);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    if (!kw) return metrics;
    return metrics.filter(
      (m) =>
        m.metric_id.toLowerCase().includes(kw) ||
        m.name.toLowerCase().includes(kw) ||
        m.description.toLowerCase().includes(kw),
    );
  }, [metrics, q]);

  const grouped = useMemo(() => {
    const used = new Set<string>();
    const out: Array<[string, MetricInfo[]]> = [];
    for (const g of GROUPS) {
      const items = filtered.filter((m) => g.match(m) && !used.has(m.metric_id));
      items.forEach((m) => used.add(m.metric_id));
      if (items.length) out.push([g.label, items]);
    }
    const rest = filtered.filter((m) => !used.has(m.metric_id));
    if (rest.length) out.push(["其他指标", rest]);
    return out;
  }, [filtered]);

  return (
    <div className="card" style={{ padding: "18px 20px" }}>
      <h2 style={{ margin: 0 }}>📏 评估指标目录</h2>
      <p className="muted" style={{ marginTop: 6 }}>
        平台所有研究任务使用的优化指标：每个指标都有<b>含义解释、计算方式/公式</b>与一套
        <b>通用标准的参考实现</b>（Python 源码，位于 <code>benchmark_tasks/metric_lib</code>，
        可直接 import 复用）。新建研究时可从本目录选择优化指标（覆盖任务默认指标）。
      </p>

      <div className="row" style={{ marginTop: 12, gap: 10 }}>
        <input
          className="search"
          placeholder="搜索指标名 / ID / 描述…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, minWidth: 240 }}
        />
        <span className="muted">{filtered.length} 个指标</span>
      </div>

      {error && <div className="card error" style={{ marginTop: 10 }}>加载失败：{error}</div>}

      {grouped.map(([label, items]) => (
        <div key={label} style={{ marginTop: 16 }}>
          <h3 style={{ borderBottom: "1px solid var(--border)", paddingBottom: 4, fontSize: 14 }}>
            {label}
          </h3>
          {items.map((m) => {
            const isOpen = expanded === m.metric_id;
            return (
              <div key={m.metric_id} className={`catalog-card ${isOpen ? "open" : ""}`}>
                <div className="row catalog-head">
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => setExpanded(isOpen ? null : m.metric_id)}
                  >
                    <span className={`chev ${isOpen ? "collapsed" : ""}`}>▾</span>
                    <strong>{m.name}</strong>
                  </button>
                  <span className="muted mono small">{m.metric_id}</span>
                  <span className={`pill ${m.direction === "higher" ? "ok" : "warn"}`}>
                    {dirText(m.direction)}
                  </span>
                </div>
                <div className="muted small" style={{ margin: "2px 0 4px 18px" }}>
                  {m.description.length > 80 ? `${m.description.slice(0, 80)}…` : m.description}
                </div>
                {isOpen && (
                  <div className="catalog-detail">
                    <div className="kv" style={{ alignItems: "flex-start" }}>
                      <span className="muted">指标含义</span>
                      <span style={{ lineHeight: 1.65 }}>{m.description}</span>
                    </div>
                    <div className="kv" style={{ alignItems: "flex-start" }}>
                      <span className="muted">计算方式</span>
                      <span style={{ lineHeight: 1.65 }}>{m.computation}</span>
                    </div>
                    <div className="kv">
                      <span className="muted">常见范围</span>
                      <span className="mono small">{m.typical_range}</span>
                    </div>
                    <div className="kv" style={{ alignItems: "flex-start" }}>
                      <span className="muted">适用任务</span>
                      <div className="tags">
                        {m.applicable.map((a) => (
                          <span key={a} className="tag">#{a}</span>
                        ))}
                      </div>
                    </div>
                    {m.implementation && (
                      <div style={{ marginTop: 8 }}>
                        <strong style={{ fontSize: 13 }}>
                          标准参考实现（{m.library}.{m.metric_id}）
                        </strong>
                        <pre
                          className="cmd"
                          style={{
                            marginTop: 4,
                            background: "var(--bg)",
                            padding: 10,
                            borderRadius: 8,
                            overflowX: "auto",
                            fontSize: 11,
                          }}
                        >
                          {m.implementation}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
