import React, { useEffect, useState } from "react";
import { getHypoTree, HypoNode } from "../api/client";

/** Hypothesis tree (Arbor-style): hypotheses ↔ evidence ↔ insight, failed branches kept. */
export function HypothesisTree({ runId }: { runId: string }) {
  const [nodes, setNodes] = useState<HypoNode[]>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const t = await getHypoTree(runId);
        if (alive) setNodes(t.nodes || []);
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for HypothesisTree:", err);
      }
    };
    load();
    const iv = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, [runId]);

  if (!nodes.length) return <div className="card muted">假设树为空（尚未运行双循环）。</div>;

  const byId = new Map(nodes.map((n) => [n.node_id, n]));
  const roots = nodes.filter((n) => !n.parent_id || !byId.has(n.parent_id));

  const renderNode = (n: HypoNode, depth: number) => (
    <div key={n.node_id} className={`tree-node ${n.status}`} style={{ marginLeft: depth ? 6 : 0 }}>
      <div className="row">
        <span className={`pill ${n.status === "merged" ? "ok" : n.status === "pruned" ? "bad" : "accent"}`}>
          {n.status}
        </span>
        <strong>{n.hypothesis}</strong>
        <span className="muted mono">score={n.score}</span>
      </div>
      {n.insight && <div className="muted" style={{ marginTop: 2 }}>↳ {n.insight}</div>}
      {n.evidence_refs?.length > 0 && (
        <div className="mono muted" style={{ marginTop: 2 }}>
          evidence: {n.evidence_refs.join(", ")}
        </div>
      )}
      {nodes
        .filter((c) => c.parent_id === n.node_id)
        .map((c) => renderNode(c, depth + 1))}
    </div>
  );

  return (
    <div className="card">
      <h2>假设树（跨轮累积）</h2>
      <p className="muted">
        绿色=已合并（被外部审计接受）；灰色=已剪枝（保留为垫脚石 / stepping stone，供元循环 archive 复用）。
      </p>
      {roots.map((r) => renderNode(r, 0))}
    </div>
  );
}
