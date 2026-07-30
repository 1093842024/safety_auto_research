/** React hook: subscribe to dual-loop progress events via Server-Sent Events.

Usage:

    const { connected, latestEvent } = useSSE(runId);

- ``connected``  → true once the EventSource handshake completes
- ``latestEvent`` → the most recent progress event (null until data arrives)

The hook reconnects automatically when ``runId`` changes and cleans up on unmount.
*/

import { useEffect, useRef, useState } from "react";

export interface ProgressEvent {
    kind:
      | "inner_done"
      | "audit_done"
      | "finished"
      | "connected"
      | "generation_done"
      | "budget_exceeded";
    iter: number;
    max_iters: number;
    accuracy?: number;
    confidence?: number;
    recommendation?: string;
    gate_passed?: boolean;
    reason?: string;
    detail?: string;
    metrics?: Record<string, number>;
    generation?: number;
    champion_fitness?: number;
    ts?: number;
}

export function useSSE(runId: string | undefined) {
    const [connected, setConnected] = useState(false);
    const [latestEvent, setLatestEvent] = useState<ProgressEvent | null>(null);
    const sourceRef = useRef<EventSource | null>(null);

    useEffect(() => {
        if (!runId) return;

        const es = new EventSource(`/api/workflow-runs/${encodeURIComponent(runId)}/stream`);
        sourceRef.current = es;

        es.addEventListener("connected", () => {
            setConnected(true);
        });

        es.addEventListener("progress", (msg: MessageEvent) => {
            try {
                const data = JSON.parse(msg.data) as ProgressEvent;
                setLatestEvent(data);
            } catch {
                // ignore malformed events
            }
        });

        es.onerror = () => {
            setConnected(false);
            // EventSource auto-reconnects; we just flip the flag.
        };

        return () => {
            es.close();
            sourceRef.current = null;
            setConnected(false);
        };
    }, [runId]);

    return { connected, latestEvent };
}
