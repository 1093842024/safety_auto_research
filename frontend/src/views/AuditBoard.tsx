import React, { useEffect, useState } from "react";
import { getAudit, AuditEvent } from "../api/client";

const STATUS_PILL: Record<string, string> = {
  verified: "ok",
  partial: "warn",
  conflict: "bad",
  missing: "bad",
};

/** Audit board: constraint-wise audit with accept/refine/restart decision. */
export function AuditBoard({ runId }: { runId: string }) {
  const [audits, setAudits] = useState<AuditEvent[]>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const a = await getAudit(runId);
        if (alive) setAudits(a);
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

  if (!audits.length) return <div className="card muted">尚无外部审计事件。</div>;

  return (
    <>
      {audits.map((a, idx) => (
        <div className="card" key={a.audit_id}>
        <div className="refresh">
          <h2>审计 #{idx + 1}　<span className="muted mono">{a.audit_id}</span></h2>
          <span className={`pill ${
            a.unresolved_claims.length ? "warn" : a.gate_passed ? "ok" : "bad"
          }`}>
            {a.unresolved_claims.length
              ? "需复核 · REFINE"
              : a.gate_passed ? "通过 · ACCEPT" : "未通过"}
          </span>
        </div>
        <p className="muted" style={{ marginTop: 4 }}>
          外部审计层结论（约束级检查 + 未解声明）；router 最终裁决见「双循环总览」。
        </p>

          <div className="kv">
            <span className="muted">审计目标</span>
            <span>{a.audited_target}</span>
          </div>
          <div className="kv">
            <span className="muted">置信度 s</span>
            <span>
              <span className="bar" style={{ width: 120, display: "inline-block", verticalAlign: "middle" }}>
                <span style={{ width: `${Math.round(a.confidence * 100)}%` }} />
              </span>{" "}
              {a.confidence}
            </span>
          </div>
          <div className="kv">
            <span className="muted">可恢复性 v</span>
            <span className={`pill ${a.recoverable ? "ok" : "bad"}`}>
              {a.recoverable ? "recoverable" : "not recoverable"}
            </span>
          </div>

          <h3 style={{ marginTop: 10 }}>约束级检查</h3>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr className="muted" style={{ textAlign: "left", fontSize: 12 }}>
                <th>约束</th>
                <th>状态</th>
                <th>评分</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {a.constraints.map((c) => (
                <tr key={c.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{c.description}</td>
                  <td>
                    <span className={`pill ${STATUS_PILL[c.status] || "accent"}`}>{c.status}</span>
                  </td>
                  <td className="mono">{c.score ?? "—"}</td>
                  <td className="muted">{c.note || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {a.unresolved_claims.length > 0 && (
            <>
              <h3>未解声明</h3>
              <ul className="tight">
                {a.unresolved_claims.map((u, i) => (
                  <li key={i}>{u}</li>
                ))}
              </ul>
            </>
          )}
          {a.rejected_candidates.length > 0 && (
            <>
              <h3>被拒候选（保留为垫脚石）</h3>
              <ul className="tight">
                {a.rejected_candidates.map((r, i) => (
                  <li key={i} className="muted">
                    {r}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ))}
    </>
  );
}
