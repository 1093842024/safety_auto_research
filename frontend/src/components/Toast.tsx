import React, { createContext, useCallback, useContext, useRef, useState } from "react";

export type ToastKind = "info" | "error" | "warn" | "ok";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  push: (message: string, kind?: ToastKind) => void;
}

const ToastContext = createContext<ToastApi>({ push: () => {} });

/** Access the global toast API from any component. */
export function useToast(): ToastApi {
  return useContext(ToastContext);
}

/**
 * Global toast provider. Renders a bottom-right stack of transient notifications.
 * Pairs with the React error boundary and the client's schema-violation handler so
 * that backend contract drift, request timeouts, and uncaught UI errors all become
 * visible to the user instead of failing silently.
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const idRef = useRef(0);

  const push = useCallback((message: string, kind: ToastKind = "info") => {
    const id = ++idRef.current;
    setItems((prev) => [...prev, { id, kind, message }]);
    // Errors linger longer; everything else auto-dismisses.
    const ttl = kind === "error" ? 9000 : 5000;
    window.setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, ttl);
  }, []);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="toast-host" aria-live="polite">
        {items.map((t) => (
          <div
            key={t.id}
            className={`toast toast-${t.kind}`}
            role="status"
            onClick={() => dismiss(t.id)}
            title="点击关闭"
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
