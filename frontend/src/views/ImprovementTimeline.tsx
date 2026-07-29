import React, { useEffect, useState } from "react";
import { getImprovements, ImprovementEvent } from "../api/client";

/** Recursive-improvement timeline: how the research *process* evolved (with rollback). */
export function ImprovementTimeline({ runId }: { runId: string }) {
  const [items, setItems] = useState<ImprovementEvent[]>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const i = await getImprovements(runId);
        if (alive) setItems(i);
      } catch {
        /* ignore */
      }
    };
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  if (!items.length) return <div className="card muted">尚无递归改进事件。</div>;

  return (
    <div className="card">
      <h2>递归改进时间线（元循环）</h2>
      <p className="muted">改进作用于「研究过程」而非「工件」；带 rollback_id 可回滚。</p>
      <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: 14 }}>
        {items.map((it, i) => (
          <div key={it.improvement_id} style={{ marginBottom: 14 }}>
            <div className="row">
              <span className="mono">{i + 1}.</span>
              <span className="pill accent">{it.target_mechanism}</span>
              {it.reverted ? (
                <span className="pill bad">已回滚</span>
              ) : (
                <span className="pill ok">已应用</span>
              )}
              <span className="muted mono">rollback={it.rollback_id}</span>
            </div>
            <div className="kv">
              <span className="muted">held-out 验证</span>
              <span className={`pill ${it.validated_heldout ? "ok" : "bad"}`}>
                {it.validated_heldout ? "通过" : "未通过"}
              </span>
            </div>
            <div className="kv">
              <span className="muted">before → after</span>
              <span className="mono">
                {JSON.stringify(it.metrics_before)} → {JSON.stringify(it.metrics_after)}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
