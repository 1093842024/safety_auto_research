import React, { useEffect, useMemo, useState } from "react";
import { getEvents } from "../api/client";

const COLOR: Record<string, string> = {
  eval_completed: "ok",
  audit_completed: "accent",
  improvement_applied: "accent",
  lesson_promoted: "ok",
  approval_required: "warn",
  approval_resolved: "ok",
  gate_passed: "ok",
  gate_failed: "bad",
  decision_issued: "accent",
};

/**
 * R29 fix: this view used to render EVERY event of a run, re-mounting the whole
 * list on each 2.5s poll. A long dual-loop run emits thousands of events, so the
 * DOM grew without bound and the poll turned into a visible stall. Render a
 * newest-first page and let the operator ask for more.
 */
const PAGE_SIZE = 50;

/** Event stream: color-coded timeline of all platform events for the run. */
export function EventStream({ runId }: { runId: string }) {
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(PAGE_SIZE);

  useEffect(() => {
    // A different run starts a fresh page window.
    setVisible(PAGE_SIZE);
  }, [runId]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const e = await getEvents(runId);
        if (!alive) return;
        setEvents(e);
        setError(null);
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for EventStream:", err);
        // Keep the last good list on screen, but stop pretending it is current.
        if (alive) setError(err?.message || "事件拉取失败");
      }
    };
    load();
    const t = setInterval(load, 2500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  const ordered = useMemo(() => events.slice().reverse(), [events]);
  const shown = ordered.slice(0, visible);
  const remaining = ordered.length - shown.length;

  return (
    <div className="card">
      <h2>
        事件追踪（实时）{" "}
        <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
          共 {ordered.length} 条{remaining > 0 ? `，已显示最新 ${shown.length} 条` : ""}
        </span>
      </h2>

      {error && (
        <div className="pill bad" style={{ marginBottom: 10 }}>
          {error}（显示的是最后一次成功拉取的数据）
        </div>
      )}

      <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: 14 }}>
        {shown.map((e, i) => (
          <div key={e.event_id || i} style={{ marginBottom: 8 }}>
            <span className={`pill ${COLOR[e.event_type] || "accent"}`}>{e.event_type}</span>{" "}
            <span className="mono muted">{e.event_id}</span>
          </div>
        ))}
        {ordered.length === 0 && !error && <div className="muted">暂无事件</div>}
      </div>

      {remaining > 0 && (
        <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
          <button onClick={() => setVisible((v) => v + PAGE_SIZE)}>
            显示更早的 {Math.min(PAGE_SIZE, remaining)} 条（还剩 {remaining}）
          </button>
          <button onClick={() => setVisible(ordered.length)}>全部展开</button>
        </div>
      )}
      {visible > PAGE_SIZE && (
        <div style={{ marginTop: 10 }}>
          <button onClick={() => setVisible(PAGE_SIZE)}>收起</button>
        </div>
      )}
    </div>
  );
}
