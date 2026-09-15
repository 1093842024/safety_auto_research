import React, { createContext, useCallback, useContext, useRef, useState } from "react";

/**
 * 应用内确认弹窗（替代 window.confirm）。
 *
 * 内嵌 WebView / 部分浏览器环境会拦截或吞掉原生 confirm() 对话框（表现为"点击无反应"），
 * 因此所有删除等不可逆操作统一走本组件的 Promise 化确认。
 *
 * 用法：
 *   const confirmDialog = useConfirm();
 *   if (!(await confirmDialog({ title, message, danger: true }))) return;
 *   ...执行删除
 */

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel?: string;
  /** 危险操作（删除等）：确认按钮标红。 */
  danger?: boolean;
}

const ConfirmContext = createContext<(c: ConfirmOptions) => Promise<boolean>>(
  async () => false,
);

export const useConfirm = () => useContext(ConfirmContext);

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirmDialog = useCallback(
    (c: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        resolver.current = resolve;
        setState(c);
      }),
    [],
  );

  const done = (v: boolean) => {
    resolver.current?.(v);
    resolver.current = null;
    setState(null);
  };

  return (
    <ConfirmContext.Provider value={confirmDialog}>
      {children}
      {state && (
        <div className="modal-overlay" onClick={() => done(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginTop: 0 }}>{state.title}</h3>
            <p style={{ lineHeight: 1.65 }}>{state.message}</p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
              <button className="btn" onClick={() => done(false)}>
                取消
              </button>
              <button
                className="btn"
                style={state.danger ? { background: "var(--bad)", color: "#fff", borderColor: "var(--bad)" } : undefined}
                onClick={() => done(true)}
              >
                {state.confirmLabel || "确认"}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}
