import React, { useEffect, useState } from "react";
import {
  activateApiKey,
  AgentModels,
  AgentSettings,
  ApiKeyValidateResult,
  CliLoginState,
  deleteApiKey,
  getAgentModels,
  getAgentSettings,
  getCliLoginState,
  getSystemEnvironment,
  saveApiKey,
  SystemEnvironment,
  updateAgentSettings,
  validateApiKey,
} from "../api/client";
import { useToast } from "../components/Toast";
import { useConfirm } from "../components/ConfirmDialog";

const SOURCE_LABEL: Record<string, string> = {
  "cli-probe": "已从 agent CLI 实时拉取",
  cache: "缓存（10 分钟内有效）",
  fallback: "拉取失败，显示内置列表",
};

/** CLI 模型目录里的 ``[1m]`` 是「支持 1M 上下文」的展示标记，不是模型名的一部分。 */
const stripMarker = (m: string) => m.replace(/\[1m\]$/i, "").trim();

const mb = (v: number | null | undefined): string =>
  v == null ? "—" : `${(v / 1024).toFixed(1)} GB`;

const fmtMem = (v: number | null | undefined): string =>
  v == null ? "—" : `${v} GB`;

/** 环境状态 pill：ok=绿 / warn=黄 / bad=红。 */
function EnvPill({ state, text }: { state: "ok" | "warn" | "bad"; text: string }) {
  return (
    <span className={`pill ${state === "ok" ? "ok" : state === "warn" ? "warn" : "bad"}`}>
      {text}
    </span>
  );
}

/** 校验结果展示行。 */
function ValidateResultView({ result }: { result: ApiKeyValidateResult | null }) {
  if (!result) return null;
  return (
    <div className="muted small" style={{ marginTop: 6 }}>
      {result.valid === true && (
        <span className="pill ok">✓ 有效</span>
      )}
      {result.valid === false && (
        <span className="pill bad">✗ 无效</span>
      )}
      {result.valid === null && (
        <span className="pill warn">? 无法确认</span>
      )}
      <span style={{ marginLeft: 8 }}>{result.detail}</span>
      {result.latency_ms != null && (
        <span className="mono" style={{ marginLeft: 8 }}>
          ({(result.latency_ms / 1000).toFixed(1)}s)
        </span>
      )}
    </div>
  );
}

/**
 * API Key 账号管理：保存多个 tclaude 账号（API key），测试验证有效性，
 * 并启用其中一个（启用后所有 agent CLI 调用注入该 key，下一次调用即生效）。
 */
function ApiKeyPanel() {
  const { push } = useToast();
  const confirmDialog = useConfirm();
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState("");
  const [validating, setValidating] = useState(false);
  const [result, setResult] = useState<ApiKeyValidateResult | null>(null);
  const [error, setError] = useState("");
  const [loginState, setLoginState] = useState<CliLoginState | null>(null);
  const [loginChecking, setLoginChecking] = useState(false);

  const reload = () =>
    getAgentSettings()
      .then(setSettings)
      .catch((e) => setError(String(e?.message || e)));

  const checkLogin = (refresh = false) => {
    setLoginChecking(true);
    getCliLoginState(refresh)
      .then(setLoginState)
      .catch(() => {
        /* 登录态检测失败不阻塞页面 */
      })
      .finally(() => setLoginChecking(false));
  };

  useEffect(() => {
    reload();
    // 未启用任何 key 时 agent 走 CLI 自身登录态，打开页面即检测一次（60s 后端缓存）。
    getAgentSettings()
      .then((s) => {
        if (!s.api_key_set) checkLogin();
      })
      .catch(() => {});
  }, []);

  const savedKeys = settings?.saved_keys || [];

  const runValidate = async (params: { label?: string; api_key?: string; base_url?: string }) => {
    setValidating(true);
    setResult(null);
    try {
      setResult(await validateApiKey(params));
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setValidating(false);
    }
  };

  const saveAndActivate = async () => {
    if (!key.trim()) return;
    // 标签可选：留空时自动生成，避免「按钮无反应」的困惑。
    const effectiveLabel =
      label.trim() ||
      `账号-${new Date().toISOString().slice(5, 16).replace("T", " ")}`;
    setBusy("save");
    setError("");
    try {
      // base_url is stored per-account and applied automatically on activate:
      // enabling an account deactivates the previous one (单选语义).
      await saveApiKey(effectiveLabel, key.trim(), baseUrl.trim() || undefined);
      await activateApiKey(effectiveLabel);
      setKey("");
      setLabel("");
      setBaseUrl("");
      await reload();
      push(`已保存并启用账号 ${effectiveLabel}`, "ok");
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="card" style={{ padding: "18px 20px", marginTop: 16 }}>
      <h2 style={{ margin: 0 }}>🔑 Agent API Key · tclaude 账号</h2>
      <p className="muted" style={{ marginTop: 6 }}>
        账号即 API Key（认证走 <code>TCLAUDE_AUTH_TOKEN</code>，无需账号名称层面的登录切换）：
        保存多把 Key，<b>测试验证</b>有效性，<b>启用</b>其中一把即可完成账号切换——
        原账号自动变为未启用，可随时一键切回。启用后所有 agent CLI 调用
        （双循环 agent 模式、MEA 角色代理）注入该 Key，下一次调用即生效。
        Key 仅保存在本机 <code>data/system_settings.json</code>，接口只返回掩码。
      </p>

      {error && <div className="card error" style={{ marginTop: 10 }}>{error}</div>}

      {/* ---- 当前使用状态（检测并展示） ---- */}
      <div className="kv" style={{ marginTop: 12, background: "var(--panel-2, rgba(0,0,0,0.06))", padding: "10px 12px", borderRadius: 8 }}>
        <span className="muted">当前使用账号</span>
        <span>
          {settings?.api_key_set ? (
            <>
              <b>{settings.active_account?.label || "（未命名）"}</b>
              <span className="mono muted" style={{ marginLeft: 8 }}>{settings.active_account?.masked}</span>
            </>
          ) : (
            <b>CLI 自身登录态（未启用任何 Key）</b>
          )}
        </span>
        <span className="muted" style={{ marginTop: 6 }}>网关 Base URL</span>
        <span className="mono">
          {settings?.api_base_url || "默认（tclaude 守护进程网关 127.0.0.1:…）"}
        </span>
        <span className="muted" style={{ marginTop: 6 }}>CLI 登录态（tclaude login）</span>
        <span>
          {loginChecking ? (
            <span className="muted">检测中…（真实请求约 5-10s）</span>
          ) : loginState ? (
            <>
              {loginState.logged_in === true && <span className="pill ok">已登录 · 可用</span>}
              {loginState.logged_in === false && <span className="pill bad">未登录 / 不可用</span>}
              {loginState.logged_in === null && <span className="pill warn">无法确认</span>}
              <span className="muted small" style={{ marginLeft: 8 }} title={loginState.detail}>
                {loginState.detail}
              </span>
              <button
                className="btn tiny"
                style={{ marginLeft: 8 }}
                disabled={loginChecking}
                title="重新发起一次真实 CLI 请求检测登录态（结果缓存 60s）"
                onClick={() => checkLogin(true)}
              >
                重新检测
              </button>
            </>
          ) : (
            <>
              <span className="muted small">未检测</span>
              <button className="btn tiny" style={{ marginLeft: 8 }} disabled={loginChecking} onClick={() => checkLogin(true)}>
                检测登录态
              </button>
            </>
          )}
        </span>
      </div>

      {/* ---- 已保存账号列表 ---- */}
      <div style={{ marginTop: 12 }}>
        <h3 style={{ margin: "0 0 6px", fontSize: 14 }}>已保存账号</h3>
        {savedKeys.length === 0 && <div className="muted small">暂无保存的账号。</div>}
        {savedKeys.map((k) => (
          <div key={k.label} className="row" style={{ gap: 10, padding: "6px 0", alignItems: "center" }}>
            <span className="mono">{k.label}</span>
            <span className="muted mono small">{k.masked}</span>
            {k.base_url && (
              <span className="muted mono small" title="该账号绑定的网关 Base URL">
                🌐 {k.base_url}
              </span>
            )}
            {k.active ? <span className="pill ok">当前使用</span> : <span className="pill gray">未启用</span>}
            <span style={{ flex: 1 }} />
            <button
              className="btn tiny"
              disabled={validating || busy !== ""}
              title="用该账号的 Key 发送一次真实最小请求验证有效性"
              onClick={async () => {
                setResult(null);
                await runValidate({ label: k.label });
              }}
            >
              {validating ? "检测中…" : "测试验证"}
            </button>
            {!k.active && (
              <button
                className="btn tiny primary"
                disabled={busy !== ""}
                title="一键切换：停用当前账号并启用此账号（应用其绑定的网关 Base URL）"
                onClick={async () => {
                  setBusy(`act:${k.label}`);
                  setError("");
                  try {
                    await activateApiKey(k.label);
                    await reload();
                    push(`已切换到账号 ${k.label}`, "ok");
                  } catch (e: any) {
                    setError(String(e?.message || e));
                  } finally {
                    setBusy("");
                  }
                }}
              >
                {busy === `act:${k.label}` ? "切换中…" : "一键切换启用"}
              </button>
            )}
            <button
              className="btn tiny"
              disabled={busy !== ""}
              title="删除该账号（若为当前使用账号则回退到 CLI 登录态）"
              onClick={async () => {
                if (!(await confirmDialog({
                  title: "删除账号",
                  message: `确认删除账号 ${k.label}？（若为当前使用账号则回退到 CLI 登录态）`,
                  confirmLabel: "删除",
                  danger: true,
                }))) return;
                setBusy(`del:${k.label}`);
                setError("");
                try {
                  await deleteApiKey(k.label);
                  await reload();
                } catch (e: any) {
                  setError(String(e?.message || e));
                } finally {
                  setBusy("");
                }
              }}
            >
              删除
            </button>
          </div>
        ))}
        <ValidateResultView result={result} />
      </div>

      {/* ---- 新增账号 ---- */}
      <div style={{ marginTop: 16 }}>
        <h3 style={{ margin: "0 0 6px", fontSize: 14 }}>新增账号</h3>
        <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input
            className="search"
            style={{ flex: "0 1 160px" }}
            placeholder="账号标签（可选，留空自动生成）"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <input
            className="search"
            style={{ flex: "1 1 280px", fontFamily: "monospace" }}
            type="password"
            placeholder="API Key（ck-… / sk-…）"
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          <input
            className="search"
            style={{ flex: "0 1 220px", fontFamily: "monospace" }}
            placeholder="网关 Base URL（可选）"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <button
            className="btn"
            disabled={(!key.trim() && !label.trim()) || validating}
            title={key.trim() ? "用输入框中的 Key 发送一次真实最小请求（不保存）" : "验证 CLI 当前登录态"}
            onClick={() => runValidate({ api_key: key.trim() || undefined, base_url: baseUrl.trim() || undefined })}
          >
            {validating ? "验证中…" : "测试验证"}
          </button>
          <button
            className="btn primary"
            disabled={!key.trim() || busy !== ""}
            title={key.trim() ? "保存该账号并立即启用（原启用账号自动变为未启用）" : "请先填写 API Key"}
            onClick={saveAndActivate}
          >
            {busy === "save" ? "保存中…" : "保存并启用"}
          </button>
        </div>
        <div className="muted small" style={{ marginTop: 6 }}>
          验证原理：向 tclaude 网关守护进程发送一次携带该 Key 的最小请求
          （<code>x-api-key</code> / <code>Authorization: Bearer</code>），按 HTTP 状态判定——
          401/403 即无效，200 且返回真实模型响应即有效；耗时通常 &lt;1s。
          未填 Key 时改为通过 CLI 真实请求验证其自身登录态。
        </div>
      </div>
    </div>
  );
}

/** 本地机器环境检测：Docker / GPU / agent CLI + 主机概况。 */
function EnvironmentPanel() {
  const [env, setEnv] = useState<SystemEnvironment | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);

  const load = () => {
    setChecking(true);
    setError("");
    getSystemEnvironment()
      .then(setEnv)
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setChecking(false));
  };

  useEffect(load, []);

  const docker = env?.docker;
  const gpu = env?.gpu;
  const cli = env?.agent_cli;

  return (
    <div className="card" style={{ padding: "18px 20px", marginTop: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>🖥 本地环境检测</h2>
        <button className="btn tiny" disabled={checking} onClick={load}>
          {checking ? "检测中…" : "重新检测"}
        </button>
      </div>
      <p className="muted" style={{ marginTop: 6 }}>
        检测本机 Docker（CLI + 守护进程）、GPU（nvidia-smi）与 agent CLI 环境，以及主机基本信息。
        检测为只读探测，不修改任何配置。
      </p>

      {error && <div className="card error" style={{ marginTop: 10 }}>检测失败：{error}</div>}
      {!env && !error && <div className="muted" style={{ marginTop: 10 }}>检测中…</div>}

      {env && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12, marginTop: 12 }}>
          {/* ---- Docker ---- */}
          <div className="card" style={{ padding: "12px 14px", margin: 0 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>🐳 Docker</strong>
              {docker && docker.available && docker.daemon_running ? (
                <EnvPill state="ok" text="可用" />
              ) : docker && docker.available ? (
                <EnvPill state="warn" text="已安装 · 守护进程未运行" />
              ) : (
                <EnvPill state="bad" text="不可用" />
              )}
            </div>
            {docker && (
              <div className="muted small" style={{ marginTop: 8 }}>
                {docker.available ? (
                  <>
                    <div className="mono">{docker.version}</div>
                    {docker.daemon_running && <div>Server: <span className="mono">{docker.server_version}</span></div>}
                    {docker.path && <div className="mono small">{docker.path}</div>}
                    {!docker.daemon_running && <div style={{ marginTop: 4 }}>{docker.reason}</div>}
                  </>
                ) : (
                  <div>{docker.reason}</div>
                )}
              </div>
            )}
          </div>

          {/* ---- GPU ---- */}
          <div className="card" style={{ padding: "12px 14px", margin: 0 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>🎛 GPU</strong>
              {gpu && gpu.available ? (
                <EnvPill state="ok" text={`可用 · ${gpu.devices?.length ?? 0} 卡`} />
              ) : (
                <EnvPill state="bad" text="不可用" />
              )}
            </div>
            {gpu && (
              <div className="muted small" style={{ marginTop: 8 }}>
                {gpu.available ? (
                  <>
                    {(gpu.devices || []).map((d, i) => (
                      <div key={i} style={{ marginTop: i ? 6 : 0 }}>
                        <div className="mono">{d.name}</div>
                        <div>
                          显存 {mb(d.memory_used_mb)} / {mb(d.memory_total_mb)}
                          {d.utilization_pct != null && ` · 利用率 ${d.utilization_pct}%`}
                          {d.temperature_c != null && ` · ${d.temperature_c}°C`}
                        </div>
                        <div className="mono small">driver {d.driver_version}</div>
                      </div>
                    ))}
                    {gpu.cuda_version && <div className="mono small" style={{ marginTop: 4 }}>{gpu.cuda_version}</div>}
                  </>
                ) : (
                  <div>{gpu.reason}</div>
                )}
              </div>
            )}
          </div>

          {/* ---- Agent CLI ---- */}
          <div className="card" style={{ padding: "12px 14px", margin: 0 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>🤖 Agent CLI</strong>
              {cli && cli.configured_available ? (
                <EnvPill state="ok" text="已就绪" />
              ) : (
                <EnvPill state="bad" text="未找到" />
              )}
            </div>
            {cli && (
              <div className="muted small" style={{ marginTop: 8 }}>
                <div>
                  配置 CLI：<span className="mono">{cli.configured_label || cli.configured_cli}</span>
                  {cli.agent_command_set && <span className="pill warn" style={{ marginLeft: 6 }}>AGENT_COMMAND 已设置</span>}
                </div>
                {cli.clis.filter((c) => c.available).map((c) => (
                  <div key={c.name} style={{ marginTop: 6 }}>
                    <span className="mono">{c.name}</span>
                    {c.configured && <span className="pill accent" style={{ marginLeft: 6 }}>当前使用</span>}
                    {c.version && <div className="mono small">{c.version}</div>}
                    {c.path && <div className="mono small">{c.path}</div>}
                  </div>
                ))}
                {cli.clis.filter((c) => !c.available).length > 0 && (
                  <div style={{ marginTop: 6 }}>
                    未安装：{cli.clis.filter((c) => !c.available).map((c) => c.name).join("、")}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {env && (
        <div className="muted small" style={{ marginTop: 10 }}>
          主机：{env.host.os} · Python {env.host.python} · {env.host.cpu_count ?? "—"} CPU
          · 内存 {fmtMem(env.host.mem_total_gb)}
        </div>
      )}
    </div>
  );
}

/** 系统设置页：agent CLI（tclaude）底层模型的查询与选择。 */
export function SystemSettings() {
  const { push } = useToast();
  const confirmDialog = useConfirm();
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [models, setModels] = useState<AgentModels | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getAgentSettings()
      .then((s) => {
        setSettings(s);
        setSelected(s.model || "");
      })
      .catch((e) => setError(String(e?.message || e)));
    getAgentModels()
      .then(setModels)
      .catch(() => {
        /* 模型列表失败不阻塞页面，显示 fallback 逻辑在后端已兜底 */
      });
  }, []);

  const save = async (model: string) => {
    setBusy(true);
    setError("");
    try {
      await updateAgentSettings(model);
      setSelected(model);
      push(model ? `已保存：agent 将使用模型 ${model}` : "已保存：恢复 CLI 默认模型", "ok");
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const dirty = settings ? selected !== (settings.model || "") : false;

  return (
    <>
    <div className="card" style={{ padding: "18px 20px" }}>
      <h2 style={{ margin: 0 }}>⚙ 系统设置 · Agent 模型</h2>
      <p className="muted" style={{ marginTop: 6 }}>
        选择 agent CLI（tclaude / Claude Code）底层使用的模型。未指定时使用 CLI 默认
        （<b>Claude-Opus-5</b> 主模型 + <b>Claude-Haiku-4.5</b> 兜底）。模型列表通过 CLI 自身
        实时拉取（发送一次校验探测，解析其接受目录），与当前登录网关完全一致。保存后
        <b>无需重启</b>：agent 下一次调用即生效（双循环 agent 模式与 MEA 角色代理均生效）。
      </p>

      {error && <div className="card error" style={{ marginTop: 10 }}>加载/保存失败：{error}</div>}

      {!settings && !error && <div className="muted">加载中…</div>}

      {settings && (
        <>
          <div className="kv" style={{ marginTop: 12 }}>
            <span className="muted">Agent CLI</span>
            <span className="mono">{settings.cli_label || settings.cli}</span>
          </div>
          <div className="kv">
            <span className="muted">当前生效模型</span>
            <span className="mono">
              {settings.model_explicit ? settings.model : "CLI 默认（Claude-Opus-5 / Claude-Haiku-4.5）"}
            </span>
          </div>

          <h3 style={{ margin: "16px 0 6px", fontSize: 14 }}>选择模型</h3>
          {models && (
            <div className="muted small" style={{ marginBottom: 8 }}>
              可选模型（{SOURCE_LABEL[models.source] || models.source}）：
              {models.models.length} 个
            </div>
          )}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <button
              className={`chip ${selected === "" ? "chip-on" : ""}`}
              onClick={() => setSelected("")}
              title="不传 --model，使用 CLI 默认模型对"
            >
              CLI 默认（Opus-5 / Haiku-4.5）
            </button>
            {(models?.models || []).map((m) => {
              const name = stripMarker(m);
              return (
                <button
                  key={m}
                  className={`chip mono ${selected === name ? "chip-on" : ""}`}
                  onClick={() => setSelected(name)}
                  title={m.includes("[1m]") ? `${m}（支持 1M 上下文）` : m}
                >
                  {name}
                </button>
              );
            })}
          </div>

          <div className="row" style={{ marginTop: 12, gap: 8, alignItems: "center" }}>
            <input
              className="search"
              style={{ flex: "0 1 320px", fontFamily: "monospace" }}
              placeholder="或手动输入模型名（完整 ID）"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
            />
            <button
              className="btn"
              disabled={!custom.trim() || busy}
              onClick={() => {
                setSelected(custom.trim());
                setCustom("");
              }}
            >
              使用该模型
            </button>
          </div>

          <div className="row" style={{ marginTop: 16, gap: 10, alignItems: "center" }}>
            <button
              className="btn primary"
              disabled={busy || !dirty}
              onClick={() => save(selected)}
            >
              {busy ? "保存中…" : dirty ? `保存并生效：${selected || "CLI 默认"}` : "已保存（无改动）"}
            </button>
            {settings.model_explicit && (
              <button className="btn" disabled={busy} onClick={() => save("")}>
                恢复 CLI 默认
              </button>
            )}
          </div>
        </>
      )}
    </div>
    <ApiKeyPanel />
    <EnvironmentPanel />
    </>
  );
}
