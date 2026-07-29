import React, { useEffect, useState } from "react";
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

/** Event stream: color-coded timeline of all platform events for the run. */
export function EventStream({ runId }: { runId: string }) {
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const e = await getEvents(runId);
        if (alive) setEvents(e);
      } catch (err: any) {
        console.warn("[Polling] Failed to fetch data for EventStream:", err);
      }
    };
    load();
    const t = setInterval(load, 2500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  return (
    <div className="card">
      <h2>事件追踪（实时）</h2>
      <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: 14 }}>
        {events
          .slice()
          .reverse()
          .map((e, i) => (
            <div key={e.event_id || i} style={{ marginBottom: 8 }}>
              <span className={`pill ${COLOR[e.event_type] || "accent"}`}>{e.event_type}</span>{" "}
              <span className="mono muted">{e.event_id}</span>
            </div>
          ))}
      </div>
    </div>
  );
}
