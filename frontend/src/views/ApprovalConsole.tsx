import React, { useEffect, useRef, useState } from "react";
import { getEvents, resolveApproval } from "../api/client";

/** HITL approval console: resolve open approval gates (risk-tiered). */
export function ApprovalConsole({ runId }: { runId: string }) {
  const [approvals, setApprovals] = useState<Array<Record<string, any>>>([]);
  const [busy, setBusy] = useState<string>("");
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const load = async () => {
    try {
      const evs = await getEvents(runId);
      if (!aliveRef.current) return;
      setApprovals(evs.filter((e) => e.event_type === "approval_required"));
    } catch (err: any) {
      console.warn("[ApprovalConsole] Failed to fetch approvals:", err);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const resolve = async (approvalId: string, resolution: "approved" | "rejected") => {
    setBusy(approvalId);
    try {
      await resolveApproval(runId, resolution);
      await load();
    } catch (err: any) {
      console.warn("[ApprovalConsole] Failed to resolve approval:", err);
    } finally {
      setBusy("");
    }
  };

  if (!approvals.length)
    return <div className="card muted">无待审批项（HITL 门未触发）。</div>;

  return (
    <div className="card">
      <h2>HITL 审批台</h2>
      {approvals.map((a) => (
        <div key={a.approval_id} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
          <div className="row">
            <span className="pill warn">{a.risk_tier || "risk?"}</span>
            <span className="mono">{a.approval_id}</span>
          </div>
          <div className="muted">{a.reason}</div>
          {a.policy_ref && <div className="mono muted">policy: {a.policy_ref}</div>}
          <div className="row" style={{ marginTop: 6 }}>
            <button
              className="btn primary"
              disabled={busy === a.approval_id}
              onClick={() => resolve(a.approval_id, "approved")}
            >
              批准
            </button>
            <button
              className="btn"
              disabled={busy === a.approval_id}
              onClick={() => {
                if (!window.confirm("确认拒绝该审批请求？此操作不可撤销。")) return;
                resolve(a.approval_id, "rejected");
              }}
            >
              拒绝
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
