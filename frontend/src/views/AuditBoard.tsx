import React, { useEffect, useState } from "react";
import {
  getAudit,
  getAuditFollowups,
  followupAudit,
  AuditEvent,
  AuditFollowup,
} from "../api/client";

const STATUS_PILL: Record<string, string> = {
  verified: "ok",
  partial: "warn",
  conflict: "bad",
  missing: "bad",
};

/**
 * R29 fix: every audit card renders its constraints, follow-ups, unresolved
 * claims and rejected candidates. A long outer loop accumulates dozens of
 * audits, and the 3s poll re-rendered all of them. Show the newest page first.
 */
const AUDIT_PAGE_SIZE = 10;

/** Audit board: constraint-wise audit with accept/refine/restart decision + F6 follow-ups. */
export function AuditBoard({ runId }: { runId: string }) {
  const [audits, setAudits] = useState<AuditEvent[]>([]);
  const [followupsByAudit, setFollowupsByAudit] = useState<Record<string, AuditFollowup[]>>({});

  // Which (audit, constraint) row has its follow-up form open.
  const [activeForm, setActiveForm] = useState<{ auditId: string; constraintId: string } | null>(null);
  const [clarification, setClarification] = useState("");
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [visible, setVisible] = useState(AUDIT_PAGE_SIZE);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const a = await getAudit(runId);
        if (!alive) return;
        setAudits(a);
        // R28 fix: fetch every audit's follow-ups in parallel (was a serial
        // await inside a for-loop = N+1 round-trips on every 3s poll).
        const settled = await Promise.allSettled(
          a.map((audit) => getAuditFollowups(runId, audit.audit_id)),
        );
        if (!alive) return;
        const failedFollowups = settled.filter((r) => r.status === "rejected").length;
        // R18 fix: an empty array used to be indistinguishable from "backend is
        // down" — keep the last known follow-ups (functional update, so the 3s
        // poll never reads a stale closure) and surface the failure instead.
        setFollowupsByAudit((prev) => {
          const byAudit: Record<string, AuditFollowup[]> = {};
          a.forEach((audit, i) => {
            const r = settled[i];
            byAudit[audit.audit_id] =
              r && r.status === "fulfilled" ? r.value : prev[audit.audit_id] || [];
          });
          return byAudit;
        });
        setLoadError(
          failedFollowups > 0
            ? `${failedFollowups} 条审计的追问记录加载失败（后端不可用），显示的可能不是最新数据`
            : "",
        );
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for AuditBoard:", err);
        // R18 fix: a failed audit fetch used to render as "尚无外部审计事件"
        // — a false negative that looks like a clean run.
        if (alive) setLoadError("审计数据加载失败（后端未启动或端点不可用）");
      }
    };
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  const openForm = (auditId: string, constraintId: string) => {
    setActiveForm({ auditId, constraintId });
    setClarification("");
    setQuestion("");
    setFormError(null);
  };

  const submitFollowup = async (auditId: string, constraintId: string) => {
    if (!clarification.trim() && !question.trim()) {
      setFormError("请填写澄清说明或追问问题");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await followupAudit(runId, auditId, {
        constraint_id: constraintId,
        clarification: clarification.trim() || null,
        question: question.trim() || null,
      });
      setActiveForm(null);
      // Refresh follow-ups immediately.
      const f = await getAuditFollowups(runId, auditId);
      setFollowupsByAudit((prev) => ({ ...prev, [auditId]: f }));
    } catch (err: any) {
      setFormError(err?.message || "追问提交失败");
    } finally {
      setBusy(false);
    }
  };

  if (!audits.length) {
    return loadError ? (
      <div className="card error">{loadError}</div>
    ) : (
      <div className="card muted">尚无外部审计事件。</div>
    );
  }

  // Newest audits first, capped to one page (R29).
  const ordered = audits.slice().reverse();
  const shown = ordered.slice(0, visible);
  const remaining = ordered.length - shown.length;

  return (
    <>
      {loadError && <div className="card error">{loadError}</div>}
      {remaining > 0 && (
        <div className="card muted" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span>
            共 {ordered.length} 条审计，已显示最新 {shown.length} 条
          </span>
          <button onClick={() => setVisible((v) => v + AUDIT_PAGE_SIZE)}>
            显示更早的 {Math.min(AUDIT_PAGE_SIZE, remaining)} 条
          </button>
          <button onClick={() => setVisible(ordered.length)}>全部展开</button>
        </div>
      )}
      {shown.map((a) => {
        // Keep the human-facing number stable regardless of paging direction.
        const idx = audits.indexOf(a);
        const followups = followupsByAudit[a.audit_id] || [];
        return (
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
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {a.constraints.map((c) => {
                  const rowFollowups = followups.filter((f) => f.constraint_id === c.id);
                  const formOpen =
                    activeForm?.auditId === a.audit_id && activeForm?.constraintId === c.id;
                  return (
                    <React.Fragment key={c.id}>
                      <tr style={{ borderTop: "1px solid var(--border)" }}>
                        <td>{c.description}</td>
                        <td>
                          <span className={`pill ${STATUS_PILL[c.status] || "accent"}`}>{c.status}</span>
                        </td>
                        <td className="mono">{c.score ?? "—"}</td>
                        <td className="muted">{c.note || ""}</td>
                        <td style={{ textAlign: "right" }}>
                          <button
                            className="btn tiny"
                            onClick={() => (formOpen ? setActiveForm(null) : openForm(a.audit_id, c.id))}
                          >
                            {formOpen ? "收起" : "追问/澄清"}
                          </button>
                        </td>
                      </tr>
                      {formOpen && (
                        <tr>
                          <td colSpan={5} style={{ background: "rgba(128,128,128,0.08)", padding: 10 }}>
                            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                              <textarea
                                className="input"
                                rows={2}
                                placeholder="澄清说明（例如：补充证据/新数据，使该约束达到 verified）"
                                value={clarification}
                                onChange={(e) => setClarification(e.target.value)}
                              />
                              <input
                                className="input"
                                placeholder="追问问题（可选）"
                                value={question}
                                onChange={(e) => setQuestion(e.target.value)}
                              />
                              {formError && <span className="pill bad">{formError}</span>}
                              <div style={{ display: "flex", gap: 6 }}>
                                <button
                                  className="btn tiny"
                                  disabled={busy}
                                  onClick={() => submitFollowup(a.audit_id, c.id)}
                                >
                                  {busy ? "提交中…" : "提交追问"}
                                </button>
                                <span className="muted" style={{ fontSize: 11 }}>
                                  补充证据可重算该约束；原审计结论不变。
                                </span>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                      {rowFollowups.length > 0 && (
                        <tr>
                          <td colSpan={5} style={{ paddingLeft: 16 }}>
                            <ul className="tight">
                              {rowFollowups.map((f) => (
                                <li key={f.event_id} className="muted" style={{ fontSize: 12 }}>
                                  <span className={`pill ${f.resolved ? "ok" : "warn"}`}>
                                    {f.resolved ? "已澄清" : f.question ? "已追问" : "未解决"}
                                  </span>{" "}
                                  {f.clarification && <>澄清：{f.clarification} </>}
                                  {f.question && <>追问：{f.question} </>}
                                  <span className="mono">
                                    （{f.prior_status} → {f.new_status}, score={f.new_score}）
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
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
        );
      })}
    </>
  );
}
