import React, { useEffect, useState } from "react";
import {
  getLlmModels,
  getLlmProvider,
  chatLlm,
  testLlmLabel,
  LlmModelInfo,
  LlmProviderInfo,
} from "../api/client";

/** LLM 标注调试面板：Provider 配置 + 10 模型目录 + 提示词调试 + 标注测试。 */
export function LlmPanel() {
  const [models, setModels] = useState<LlmModelInfo[]>([]);
  const [provider, setProvider] = useState<LlmProviderInfo | null>(null);

  // Per-call override (does NOT persist; submitted alongside each test).
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [temperature, setTemperature] = useState(0.2);
  const [maxTokens, setMaxTokens] = useState(512);

  // Chat-test state.
  const [chatSystem, setChatSystem] = useState("你是一个简洁的中文助手。");
  const [chatUser, setChatUser] = useState("用一句话介绍数据飞轮。");
  const [chatResult, setChatResult] = useState<{ ok: boolean; text?: string; error?: string } | null>(null);
  const [chatLoading, setChatLoading] = useState(false);

  // Label-test state.
  const [labelSystem, setLabelSystem] = useState(
    "你是一个精确的数据标注员。给定一条样本（Titanic 乘客特征），仅输出 JSON {\"label\": 0|1}（0=未生还，1=生还）。不要解释。",
  );
  const [labelTemplate, setLabelTemplate] = useState(
    "样本（{columns}）：\n{row}\n\n请输出真实标签。返回 JSON {\"label\": <value>}}。",
  );
  const [labelRows, setLabelRows] = useState<Array<Record<string, string>>>([
    { Pclass: "1", Sex: "female", Age: "29", Fare: "211.34" },
    { Pclass: "3", Sex: "male", Age: "25", Fare: "7.25" },
  ]);
  const [labelResults, setLabelResults] = useState<Array<{ row: Record<string, unknown>; label: string; raw: string; error?: string | null }> | null>(null);
  const [labelLoading, setLabelLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await getLlmModels();
        if (!alive) return;
        setModels(r.models);
        setProvider(r.default);
        if (r.default.model) setModel(r.default.model);
      } catch (e: any) {
        setError(String(e?.message || e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const providerRequest = () => ({
    base_url: baseUrl || undefined,
    api_key: apiKey || undefined,
    model: model || undefined,
    temperature,
    max_tokens: maxTokens,
  });

  const doChat = async () => {
    setChatLoading(true);
    setError("");
    try {
      const r = await chatLlm({ system: chatSystem, user: chatUser, provider: providerRequest() });
      setChatResult({ ok: r.ok, text: r.text, error: r.error });
    } catch (e: any) {
      setChatResult({ ok: false, error: String(e?.message || e) });
    } finally {
      setChatLoading(false);
    }
  };

  const doLabel = async () => {
    setLabelLoading(true);
    setError("");
    try {
      const r = await testLlmLabel({
        rows: labelRows,
        system_prompt: labelSystem,
        user_template: labelTemplate,
        provider: providerRequest(),
      });
      setLabelResults(r.labels);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLabelLoading(false);
    }
  };

  const setRow = (idx: number, key: string, value: string) => {
    setLabelRows((prev) => {
      const next = prev.slice();
      next[idx] = { ...next[idx], [key]: value };
      return next;
    });
  };
  const addRow = () =>
    setLabelRows((prev) => [...prev, { Pclass: "", Sex: "", Age: "", Fare: "" }]);
  const removeRow = (idx: number) =>
    setLabelRows((prev) => prev.filter((_, i) => i !== idx));

  return (
    <div className="llm">
      <div className="card">
        <h2>🧠 LLM 自动标注 · 提示词调试</h2>
        <p className="muted">
          默认走 Venus 代理（OpenAI 兼容协议），下表列出 10 个支持的模型。
          下方表单可临时覆盖 <code>base_url</code> / <code>api_key</code> / <code>model</code>
          用于提示词调试与单次实验；实际飞轮重训会读取 <code>auto_label</code> 能力传入的覆盖值。
        </p>

        <div className="row" style={{ marginTop: 10, gap: 14, alignItems: "flex-end" }}>
          <div className="field" style={{ minWidth: 220, flex: 1 }}>
            <label>Provider base_url</label>
            <input
              type="text"
              value={baseUrl}
              placeholder={provider?.base_url || "http://v2.open.venus.oa.com/llmproxy"}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
          <div className="field" style={{ minWidth: 200 }}>
            <label>API Key</label>
            <input
              type="password"
              value={apiKey}
              placeholder={provider ? `使用默认 (${provider.api_key_masked})` : "sk-..."}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>
          <div className="field" style={{ minWidth: 240 }}>
            <label>模型</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.label} ({m.family})</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ width: 100 }}>
            <label>temperature</label>
            <input type="number" step="0.05" min="0" max="2" value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} />
          </div>
          <div className="field" style={{ width: 100 }}>
            <label>max_tokens</label>
            <input type="number" step="32" min="32" max="4096" value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} />
          </div>
        </div>
      </div>

      <div className="card">
        <h2>💬 单轮对话测试（提示词调试）</h2>
        <div className="grid2">
          <div>
            <h3>System</h3>
            <textarea
              rows={4}
              value={chatSystem}
              onChange={(e) => setChatSystem(e.target.value)}
              style={{ width: "100%" }}
            />
            <h3 style={{ marginTop: 10 }}>User</h3>
            <textarea
              rows={3}
              value={chatUser}
              onChange={(e) => setChatUser(e.target.value)}
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <h3>模型回复</h3>
            <div className="card" style={{ minHeight: 140, background: "var(--bg)" }}>
              {chatLoading ? (
                <span className="muted">调用中…</span>
              ) : chatResult ? (
                chatResult.ok ? (
                  <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontSize: 13 }}>{chatResult.text}</pre>
                ) : (
                  <div className="error">错误：{chatResult.error}</div>
                )
              ) : (
                <span className="muted">点击「测试」开始</span>
              )}
            </div>
            <button className="btn primary" style={{ marginTop: 10 }} onClick={doChat} disabled={chatLoading}>
              {chatLoading ? "调用中…" : "测试对话"}
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>🏷️ 批量标注测试（auto_label 调试）</h2>
        <p className="muted">
          用 <code>{"{row}"}</code> / <code>{"{columns}"}</code> 占位符把每行渲染到 user 提示词。
          模型返回被解析为 JSON <code>{"{"}"label": ...{"}"}</code>（失败回退到首行/首词）。
        </p>
        <div className="grid2">
          <div>
            <h3>System 提示词</h3>
            <textarea
              rows={4}
              value={labelSystem}
              onChange={(e) => setLabelSystem(e.target.value)}
              style={{ width: "100%" }}
            />
            <h3 style={{ marginTop: 10 }}>User 模板</h3>
            <textarea
              rows={3}
              value={labelTemplate}
              onChange={(e) => setLabelTemplate(e.target.value)}
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <h3>样本行（编辑后点击「测试标注」）</h3>
            <div className="card" style={{ background: "var(--bg)" }}>
              {labelRows.map((r, i) => (
                <div key={i} className="row" style={{ marginBottom: 6 }}>
                  {Object.entries(r).map(([k, v]) => (
                    <input
                      key={k}
                      className="input"
                      style={{ width: 120 }}
                      value={v}
                      placeholder={k}
                      onChange={(e) => setRow(i, k, e.target.value)}
                    />
                  ))}
                  <button className="btn tiny" onClick={() => removeRow(i)}>删除</button>
                </div>
              ))}
              <button className="btn tiny" onClick={addRow}>＋ 添加行</button>
            </div>
            <button className="btn primary" style={{ marginTop: 10 }} onClick={doLabel} disabled={labelLoading}>
              {labelLoading ? "调用中…" : "测试标注"}
            </button>
          </div>
        </div>
        {error && <div className="card error" style={{ marginTop: 10 }}>错误：{error}</div>}

        {labelResults && labelResults.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <h3>标注结果</h3>
            <table className="lb-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>行</th>
                  <th>解析 label</th>
                  <th>原始回复</th>
                </tr>
              </thead>
              <tbody>
                {labelResults.map((r, i) => (
                  <tr key={i}>
                    <td>{i + 1}</td>
                    <td className="mono small">{JSON.stringify(r.row)}</td>
                    <td>
                      {r.error ? (
                        <span className="pill bad">{r.error}</span>
                      ) : (
                        <span className="pill ok">{r.label || "(空)"}</span>
                      )}
                    </td>
                    <td className="mono small" style={{ maxWidth: 320 }}>
                      <code>{r.raw}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
