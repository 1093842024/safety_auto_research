import React, { useEffect, useMemo, useState } from "react";
import {
  BenchmarkTask,
  LaunchConfig,
  InnerLoopConfig,
  CapabilityInfo,
  deleteBenchmarkTask,
  getBenchmarkTasks,
  getProtocol,
  launchBenchmarkTask,
  CATEGORY_LABELS,
} from "../api/client";
import { RegisterTask } from "./RegisterTask";

// Inner-loop agents may NOT drive the reserved OUTER-loop capabilities (they would
// let the inner loop judge its own work). Mirror of harness.OUTER_LOOP_RESERVED_CAPS.
const RESERVED_CAPS = new Set([
  "layer_11_external_audit",
  "external_audit",
  "audit",
  "layer_09_self_iterative_evolution",
  "self_iterative_evolution",
  "self_evolution",
]);

// UI4: map the audit-threshold slider value to a human-readable strictness tier so
// users get immediate textual feedback instead of a bare number.
function auditTierLabel(v: number): string {
  if (v <= 0.65) return "宽松";
  if (v <= 0.82) return "中等";
  return "严格";
}

const MODEL_OPTIONS = [
  { value: "gbm", label: "gbm（梯度提升基线）" },
  { value: "gbm-strong", label: "gbm-strong（HistGB，更强）" },
  { value: "rf", label: "rf（随机森林）" },
  { value: "logreg", label: "logreg（逻辑回归）" },
];

const dirText = (d: string) =>
  d === "lower" ? "越低越好 ↓" : d === "higher" ? "越高越好 ↑" : d;

// Isolation badge for agent-mode tasks (F3): mirrors the same mapping used in BenchmarkCatalog.
function isoBadge(iso?: string) {
  switch (iso) {
    case "container-hard":
      return (
        <span className="pill iso" title="Docker 容器硬隔离：数据只读挂载 + 无网络，结果回写 EvalCompletedEvent">
          🐳 容器隔离
        </span>
      );
    case "container-soft":
      return (
        <span className="pill iso-soft" title="Docker 不可用，退回宿主软隔离（同一条研究命令，依赖/目录隔离，无 syscall/网络沙箱）">
          🛡️ 软隔离
        </span>
      );
    case "host":
      return (
        <span className="pill warn" title="未设置 AGENT_SANDBOX，agent 将在宿主直接运行，无隔离">
          ⚠️ 无隔离
        </span>
      );
    default:
      return null;
  }
}

function metricPill(t: BenchmarkTask) {
  return (
    <div className="metrics">
      <span className="pill accent">指标 {t.eval_metric}</span>
      <span className="muted">{dirText(t.direction)}</span>
      {t.baseline !== null && <span className="muted mono">baseline={t.baseline}</span>}
      {t.reference !== null && <span className="muted mono">ref={t.reference}</span>}
      {Object.keys(t.gates || {}).length > 0 && (
        <span className="muted mono">门限: {JSON.stringify(t.gates || {})}</span>
      )}
    </div>
  );
}

function TaskCard({
  t,
  selected,
  onSelect,
  onDelete,
}: {
  t: BenchmarkTask;
  selected: boolean;
  onSelect: () => void;
  onDelete?: () => void;
}) {
  const isCustom = t.task_id.startsWith("custom.");
  return (
    <div
      className={`task-card ${selected ? "selected" : ""}`}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onSelect(); }}
      role="button"
      tabIndex={0}
    >
      <div className="row">
        <strong>{t.name}</strong>
        {t.supported_by_platform && <span className="pill ok">可实跑</span>}
        {t.execution_mode === "agent" && <span className="pill warn">agent 模式</span>}
        {isoBadge(t.sandbox_isolation)}
        {isCustom && <span className="pill accent">自定义</span>}
        {isCustom && onDelete && (
          <span
            role="button"
            tabIndex={0}
            className="muted small"
            style={{ marginLeft: "auto", cursor: "pointer" }}
            title="删除该自定义任务"
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.stopPropagation();
                onDelete();
              }
            }}
          >
            ✕ 删除
          </span>
        )}
      </div>
      <div className="muted mono">{t.task_id}</div>
      <div className="muted">来源：{t.source_project} · {t.modality}</div>
      {metricPill(t)}
      <p className="desc">{t.dataset_desc}</p>
      <div className="tags">
        {t.tags.map((tag) => (
          <span key={tag} className="tag">#{tag}</span>
        ))}
      </div>

      {/* Hover tooltip (Task 2): data / model / baseline at a glance. */}
      <div className="task-tip" role="tooltip">
        <div className="tt-title">数据 / 模型 / 基线</div>
        <div className="tt-row"><span className="muted">数据</span><span>{t.dataset_desc}</span></div>
        <div className="tt-row"><span className="muted">指标</span><span className="mono">{t.eval_metric} · {dirText(t.direction)}</span></div>
        <div className="tt-row"><span className="muted">基线</span><span className="mono">baseline={t.baseline ?? "—"} · ref={t.reference ?? "—"}</span></div>
        <div className="tt-row"><span className="muted">门限</span><span className="mono">{Object.keys(t.gates || {}).length ? JSON.stringify(t.gates) : "无"}</span></div>
        <div className="tt-row"><span className="muted">执行</span><span>{t.execution_mode === "agent" ? "agent 模式（已剥离 docker/Arbor）" : "平台原生双循环"}</span></div>
        <div className="tt-row"><span className="muted">隔离</span><span>{t.sandbox_isolation === "container-hard" ? "Docker 容器硬隔离（只读数据 + 无网络）" : t.sandbox_isolation === "container-soft" ? "软隔离（Docker 不可用）" : t.sandbox_isolation === "host" ? "无隔离（宿主运行）" : "无需沙箱（平台原生）"}</span></div>
      </div>
    </div>
  );
}

function SkillTags({
  skills,
  setSkills,
}: {
  skills: string[];
  setSkills: (s: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (v && !skills.includes(v)) setSkills([...skills, v]);
    setDraft("");
  };
  return (
    <div className="skill-tags">
      {skills.map((s) => (
        <span key={s} className="chip">
          {s}
          <button type="button" className="chip-x" onClick={() => setSkills(skills.filter((x) => x !== s))}>
            ×
          </button>
        </span>
      ))}
      <input
        className="chip-input"
        value={draft}
        placeholder="添加技能名后回车"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          }
        }}
      />
    </div>
  );
}

function StepPlanEditor({
  plan,
  setPlan,
  options,
}: {
  plan: string[];
  setPlan: (p: string[]) => void;
  options: CapabilityInfo[];
}) {
  const used = new Set(plan);
  return (
    <div className="step-plan">
      {plan.length === 0 && <div className="muted small">尚未编排步骤（留空则交由内循环 Agent 自主决定）。</div>}
      {plan.map((cid, i) => (
        <div key={`${cid}-${i}`} className="step-row">
          <span className="step-idx">{i + 1}</span>
          <span className="step-name">{options.find((o) => o.capability_id === cid)?.title || cid}</span>
          <span className="muted mono small">{cid}</span>
          <span className="step-btns">
            <button type="button" className="btn tiny" disabled={i === 0} onClick={() => {
              const n = [...plan];
              [n[i - 1], n[i]] = [n[i], n[i - 1]];
              setPlan(n);
            }}>↑</button>
            <button type="button" className="btn tiny" disabled={i === plan.length - 1} onClick={() => {
              const n = [...plan];
              [n[i + 1], n[i]] = [n[i], n[i + 1]];
              setPlan(n);
            }}>↓</button>
            <button type="button" className="btn tiny" onClick={() => setPlan(plan.filter((_, j) => j !== i))}>✕</button>
          </span>
        </div>
      ))}
      <select
        className="step-add"
        value=""
        onChange={(e) => {
          const v = e.target.value;
          if (v && !used.has(v)) setPlan([...plan, v]);
        }}
      >
        <option value="">+ 添加步骤</option>
        {options
          .filter((o) => !used.has(o.capability_id))
          .map((o) => (
            <option key={o.capability_id} value={o.capability_id}>
              {o.title} · {o.capability_id}
            </option>
          ))}
      </select>
    </div>
  );
}

function ConfigForm({
  task,
  config,
  setConfig,
  inner,
  setInner,
  caps,
}: {
  task: BenchmarkTask;
  config: LaunchConfig;
  setConfig: (c: LaunchConfig) => void;
  inner: InnerLoopConfig;
  setInner: (c: InnerLoopConfig) => void;
  caps: CapabilityInfo[];
}) {
  const isPlatform = task.task_id.startsWith("platform.");
  const patch = (p: Partial<LaunchConfig>) => setConfig({ ...config, ...p });
  const patchInner = (p: Partial<InnerLoopConfig>) => setInner({ ...inner, ...p });
  const innerCaps = useMemo(() => caps.filter((c) => !RESERVED_CAPS.has(c.capability_id)), [caps]);

  return (
    <div className="config-form">
      <div className="config-help">
        <h3>研究设定</h3>
        <p className="muted">
          这些参数决定自主研究的「预算」与「严格度」。修改后会随 run 一起保存，可随时在仪表盘回看。
        </p>
      </div>

      {/* ---------------- Outer loop ---------------- */}
      <fieldset className="cfg-block">
        <legend>外循环 · 审计与预算</legend>
        <div className="field">
          <label>审计严格度（外层可接受门槛）</label>
          <div className="row" style={{ gap: 12 }}>
            <input
              type="range"
              min={0.5}
              max={0.95}
              step={0.05}
              value={config.audit_threshold ?? 0.8}
              onChange={(e) => patch({ audit_threshold: Number(e.target.value) })}
              style={{ flex: 1 }}
            />
            <span className="mono" style={{ minWidth: 44, textAlign: "right" }}>
              {(config.audit_threshold ?? 0.8).toFixed(2)}
            </span>
            <span className="muted small" style={{ minWidth: 40 }}>
              （{auditTierLabel(config.audit_threshold ?? 0.8)}）
            </span>
          </div>
          <p className="muted small">
            外循环审计判定「改进可被接受」所需的置信门槛。越高越严格——越不容易给出 ACCEPT，
            更可能在预算内持续 REFINE（更保守、更可信）。
          </p>
        </div>

        <div className="field">
          <label>最大外部迭代轮数（预算）</label>
          <div className="row" style={{ gap: 12 }}>
            <input
              type="number"
              min={1}
              max={10}
              value={config.max_outer_iters ?? 3}
              onChange={(e) => patch({ max_outer_iters: Math.max(1, Number(e.target.value)) })}
              style={{ width: 80 }}
            />
            <span className="muted small">轮</span>
          </div>
          <p className="muted small">外循环最多复核几轮。达到后无论是否 ACCEPT 都停止（终态：预算耗尽）。</p>
        </div>
      </fieldset>

      {/* ---------------- Inner loop — data / method ---------------- */}
      <fieldset className="cfg-block">
        <legend>内循环 · 数据与方法</legend>

        <div className="field">
          <label>数据集 / 竞赛预设</label>
          <select value={inner.preset ?? ""} onChange={(e) => patchInner({ preset: e.target.value || null })}>
            <option value="">自动（按任务）</option>
            <option value="titanic">titanic</option>
            <option value="spaceship">spaceship</option>
          </select>
          <p className="muted small">选择内循环实验所用的数据集；留「自动」则按当前任务决定（平台原生任务自动匹配）。</p>
        </div>

        <div className="field">
          <label>基础模型 / 方法</label>
          <select value={inner.model ?? "gbm"} onChange={(e) => patchInner({ model: e.target.value })}>
            {MODEL_OPTIONS.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>特征工程（FE）</label>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className={`btn tiny ${inner.fe !== "rich" ? "primary" : ""}`}
              onClick={() => patchInner({ fe: "basic" })}
            >基础</button>
            <button
              type="button"
              className={`btn tiny ${inner.fe === "rich" ? "primary" : ""}`}
              onClick={() => patchInner({ fe: "rich" })}
            >增强</button>
            <span className="muted small">是否启用自动特征工程作为内循环优化方向。</span>
          </div>
        </div>

        <div className="field">
          <label>交叉验证折数（cv_folds）</label>
          <input
            type="number"
            min={1}
            max={20}
            value={inner.cv_folds ?? 5}
            onChange={(e) => patchInner({ cv_folds: Math.max(1, Number(e.target.value)) })}
            style={{ width: 80 }}
          />
        </div>

        <div className="field">
          <label>目标门限（threshold）</label>
          <div className="row" style={{ gap: 8 }}>
            <input
              type="number"
              step={0.01}
              value={inner.threshold ?? ""}
              placeholder="留空=任务默认"
              onChange={(e) =>
                patchInner({ threshold: e.target.value === "" ? null : Number(e.target.value) })
              }
              style={{ width: 120 }}
            />
            <span className="muted small">
              内循环达到该指标才视为通过（如 Titanic 默认 0.82、Spaceship 默认 0.80）。
            </span>
          </div>
        </div>

        <div className="field">
          <label>丢弃列（drop_cols，逗号分隔，可空）</label>
          <input
            type="text"
            value={(inner.drop_cols || []).join(", ")}
            placeholder="如 PassengerId, Ticket"
            onChange={(e) =>
              patchInner({
                drop_cols: e.target.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
          />
          <p className="muted small">高阶：自定义从训练集中剔除的列，控制内循环可用特征。</p>
        </div>

        <div className="field">
          <label>数据目录覆盖（data_dir，可空）</label>
          <input
            type="text"
            value={inner.data_dir ?? ""}
            placeholder="留空=按预设默认路径"
            onChange={(e) => patchInner({ data_dir: e.target.value || null })}
          />
          <p className="muted small">指向自定义数据集目录（需含 train.csv）。</p>
        </div>
      </fieldset>

      {/* ---------------- Inner loop — agent mode ---------------- */}
      <fieldset className="cfg-block">
        <legend>内循环 · 自主 Agent 模式</legend>

        {inner.mode === "agent" && (
          <div className="card warn-banner" style={{ marginBottom: 12 }}>
            <strong>⚠ 自主 Agent 模式前置要求</strong>
            <p className="muted" style={{ marginTop: 6, marginBottom: 0 }}>
              需平台已接入远程 Agent：选择 <b>Codex / Claude Code</b> 可直连对应 CLI；选 <b>auto</b> 则依赖环境变量
              <code> AGENT_COMMAND</code>。未完成接入时点击「开始研究」会<b>明确失败并终止</b>，不会静默回退脚本化。
            </p>
          </div>
        )}

        <div className="field">
          <label>内循环执行方式</label>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className={`btn tiny ${inner.mode !== "agent" ? "primary" : ""}`}
              onClick={() => patchInner({ mode: "scripted" })}
            >脚本化（确定性）</button>
            <button
              type="button"
              className={`btn tiny ${inner.mode === "agent" ? "primary" : ""}`}
              onClick={() => patchInner({ mode: "agent" })}
            >自主 Agent</button>
          </div>
          <p className="muted small">
            脚本化：内循环由确定性执行器（如 kaggle_eval）完成，仅平台原生任务可用。自主 Agent：把内循环交给外部
            Agent，按下方提示词 / 技能 / 工具 / 步骤执行。<b>注意：agent 模式必须接入远程 Agent（RemoteAgentHarness），
            未接入时启动会明确失败并终止</b>（不会静默回退脚本化）。
          </p>
        </div>

        {inner.mode === "agent" && (
          <>
            <div className="field">
              <label>Agent CLI（驱动内循环的外部 Agent）</label>
              <div className="row" style={{ gap: 8 }}>
                <button
                  type="button"
                  className={`btn tiny ${(inner.agent_cli ?? "auto") === "auto" ? "primary" : ""}`}
                  onClick={() => patchInner({ agent_cli: "auto" })}
                >auto（环境变量 AGENT_COMMAND）</button>
                <button
                  type="button"
                  className={`btn tiny ${inner.agent_cli === "codex" ? "primary" : ""}`}
                  onClick={() => patchInner({ agent_cli: "codex" })}
                >Codex</button>
                <button
                  type="button"
                  className={`btn tiny ${inner.agent_cli === "claude_code" ? "primary" : ""}`}
                  onClick={() => patchInner({ agent_cli: "claude_code" })}
                >Claude Code</button>
              </div>
              <p className="muted small">
                选择「Codex / Claude Code」可在未设置全局 AGENT_COMMAND 的情况下直接驱动对应的 CLI Agent；
                每一次工具调用与最终答案都会作为「Agent 执行流水」写入任务记录，可在仪表盘展开/收起查看。
                选「auto」则复用环境变量 AGENT_COMMAND 接入的通用 Agent（未设置将启动失败）。
              </p>
            </div>
          </>
        )}

        {inner.mode === "agent" && (
          <>
            <div className="field">
              <label>系统提示词 / 内循环指令</label>
              <textarea
                rows={4}
                value={inner.system_prompt ?? ""}
                placeholder="例如：请以内循环方式优化本任务的评测指标，优先尝试特征工程与集成模型，并记录每次实验的假设与证据。"
                onChange={(e) => patchInner({ system_prompt: e.target.value })}
              />
            </div>

            <div className="field">
              <label>技能（skills）</label>
              <SkillTags skills={inner.skills || []} setSkills={(s) => patchInner({ skills: s })} />
              <p className="muted small">内循环 Agent 允许加载的技能（自由填写技能名，如 idea_spark、brainstorming）。</p>
            </div>

            <div className="field">
              <label>可用工具 / 能力（tools）</label>
              <div className="tool-list">
                {innerCaps.length === 0 && <div className="muted small">加载能力目录中…</div>}
                {innerCaps.map((c) => {
                  const checked = (inner.tools || []).includes(c.capability_id);
                  return (
                    <label key={c.capability_id} className="tool-item">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          const cur = new Set(inner.tools || []);
                          if (e.target.checked) cur.add(c.capability_id);
                          else cur.delete(c.capability_id);
                          patchInner({ tools: [...cur] });
                        }}
                      />
                      <span>
                        <b>{c.title}</b>
                        <span className="muted mono small"> {c.capability_id}</span>
                      </span>
                    </label>
                  );
                })}
              </div>
              <p className="muted small">勾选内循环 Agent 可调用/编排的能力（已排除外环保留能力 layer_11 / layer_09）。</p>
            </div>

            <div className="field">
              <label>步骤编排（step_plan，有序）</label>
              <StepPlanEditor
                plan={inner.step_plan || []}
                setPlan={(p) => patchInner({ step_plan: p })}
                options={innerCaps}
              />
              <p className="muted small">按顺序指定内循环要执行的步骤；留空则由 Agent 自主决定。</p>
            </div>
          </>
        )}
      </fieldset>

      {/* ---------------- Collaboration mode ---------------- */}
      <fieldset className="cfg-block">
        <legend>人机协作模式</legend>
        <div className="field">
          <label>执行方式</label>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className={`btn tiny ${(inner.collaboration_mode || "autonomous") === "autonomous" ? "primary" : ""}`}
              onClick={() => patchInner({ collaboration_mode: "autonomous" })}
            >完全自主</button>
            <button
              type="button"
              className={`btn tiny ${inner.collaboration_mode === "step_confirm" ? "primary" : ""}`}
              onClick={() => patchInner({ collaboration_mode: "step_confirm" })}
            >每步确认</button>
            <button
              type="button"
              className={`btn tiny ${inner.collaboration_mode === "outer_confirm" ? "primary" : ""}`}
              onClick={() => patchInner({ collaboration_mode: "outer_confirm" })}
            >外循环确认</button>
          </div>
          <p className="muted small">
            <b>完全自主</b>：全自动运行，无人工干预。<b>每步确认</b>：内循环、外审计、每轮结束后均暂停，等待人工确认与调整后继续。
            <b>外循环确认</b>：仅每轮外循环完成后暂停，可调整参数（模型/特征/门限）再进入下一轮。
          </p>
        </div>
      </fieldset>

      {isPlatform && inner.mode === "agent" && (
        <div className="card note">
          <strong>平台原生任务 · 以自主 Agent 模式执行</strong>
          <p className="muted" style={{ marginTop: 6 }}>
            该平台原生任务（{task.name}）除脚本化双循环外，也可选择<b>自主 Agent 模式</b>运行：内循环交给
            所选 CLI Agent（Codex / Claude Code / auto）执行，其每一步工具调用与最终答案都会记录为
            「Agent 执行流水」，可在研究仪表盘中展开/收起查看。若所选 Agent CLI 不可用，启动会<b>明确失败并终止</b>。
          </p>
        </div>
      )}

      {!isPlatform && (
        <div className="card note">
          <strong>本任务由 agent 模式执行（已剥离 docker / Arbor 依赖）</strong>
          <p className="muted" style={{ marginTop: 6 }}>
            「{task.name}」原本依赖 {task.harness}（如 Harbor / Arbor）在外部沙箱运行。现已将执行环境
            统一替换为<b>内循环 Agent</b>：平台仅保留任务目标、定义、数据、评估方式与指标等核心信息，
            交由 agent 自主执行，不再依赖 docker / Arbor。点击「开始研究」即尝试以 agent 模式运行；
            若平台未接入远程 Agent（RemoteAgentHarness），启动会<b>明确失败并终止</b>（请设置环境变量
            AGENT_COMMAND 接入远程 Agent，详见 README）。
          </p>
          <pre className="cmd">{task.run_command}</pre>
          <p className="muted small">↑ 上方为原始 harness 运行命令（仅作参考，已不再用于执行）。</p>
        </div>
      )}
    </div>
  );
}

export function NewResearch({
  initialTaskId,
  onCancel,
  onLaunch,
}: {
  initialTaskId?: string | null;
  onCancel: () => void;
  onLaunch: (runId: string) => void;
}) {
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [caps, setCaps] = useState<CapabilityInfo[]>([]);
  const [error, setError] = useState("");
  const [step, setStep] = useState<1 | 2>(1);
  const [selected, setSelected] = useState<BenchmarkTask | null>(null);
  const [config, setConfig] = useState<LaunchConfig>({
    audit_threshold: 0.8,
    max_outer_iters: 3,
  });
  const [inner, setInner] = useState<InnerLoopConfig>({
    mode: "scripted",
    model: "gbm",
    fe: "basic",
    cv_folds: 5,
    threshold: null,
    drop_cols: [],
    preset: null,
    data_dir: null,
    system_prompt: "",
    skills: [],
    tools: [],
    step_plan: [],
    collaboration_mode: "autonomous",
  });
  const [autoRun, setAutoRun] = useState(true);
  const [busy, setBusy] = useState(false);
  const [registering, setRegistering] = useState(false);

  const refreshTasks = async () => {
    try {
      setTasks(await getBenchmarkTasks());
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  };

  const handleDeleteTask = async (t: BenchmarkTask) => {
    if (!window.confirm(`确认删除自定义任务「${t.name}」？已创建的研究记录不受影响。`)) return;
    try {
      await deleteBenchmarkTask(t.task_id);
      if (selected?.task_id === t.task_id) setSelected(null);
      await refreshTasks();
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [t, proto] = await Promise.all([getBenchmarkTasks(), getProtocol()]);
        if (!alive) return;
        setTasks(t);
        const list = (proto?.capabilities || []) as Array<Record<string, any>>;
        setCaps(
          list.map((c) => ({
            capability_id: c.capability_id,
            title: c.title || c.layer_name || c.capability_id,
            layer_name: c.layer_name,
            description: c.description,
          })),
        );
      } catch (e: any) {
        if (alive) setError(String(e?.message || e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // Pre-select a task when arriving from the catalog (Task 3: "用此任务新建研究").
  useEffect(() => {
    if (!initialTaskId || tasks.length === 0) return;
    const match = tasks.find((t) => t.task_id === initialTaskId);
    if (match) {
      setSelected(match);
      setStep(2);
    }
  }, [initialTaskId, tasks]);

  const grouped = useMemo(() => {
    const m = new Map<string, BenchmarkTask[]>();
    for (const t of tasks) {
      if (!m.has(t.category)) m.set(t.category, []);
      m.get(t.category)!.push(t);
    }
    return m;
  }, [tasks]);

  const handleLaunch = async () => {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      // inner_loop is the single source of truth for model/fe/agent_cli/collaboration;
      // top-level duplicates are only kept in the backend as a fallback for legacy payloads.
      const payload: LaunchConfig = {
        audit_threshold: config.audit_threshold,
        max_outer_iters: config.max_outer_iters,
        inner_loop: {
          ...inner,
          collaboration_mode: inner.collaboration_mode || "autonomous",
          agent_cli: inner.mode === "agent" ? (inner.agent_cli ?? null) : null,
        },
        // Legacy fallback fields — kept for backward compat with older backend versions that
        // construct InnerLoopConfig from cfg.model/cfg.fe when cfg.inner_loop is missing.
        model: inner.model,
        fe: inner.fe === "rich",
        agent_cli: inner.mode === "agent" ? (inner.agent_cli ?? null) : null,
      };
      const r = await launchBenchmarkTask(selected.task_id, payload, !autoRun);
      // Agent-mode launch with no remote agent configured ends as a clear FAILED run
      // (Task 1): surface the reason and keep the user on this screen.
      if (r.status === "failed") {
        setError(r.message || "启动失败：内循环 agent 模式需要接入远程 Agent。");
        setBusy(false);
        return;
      }
      onLaunch(r.run_id);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (registering) {
    return (
      <RegisterTask
        onCancel={() => setRegistering(false)}
        onDone={async (task) => {
          setRegistering(false);
          await refreshTasks();
          setSelected(task);
          setStep(2);
        }}
      />
    );
  }

  return (
    <div className="new-research">
      <div className="wizard-steps">
        <span className={`step ${step === 1 ? "active" : ""}`}>1 · 选择研究任务</span>
        <span className="step-arrow">→</span>
        <span className={`step ${step === 2 ? "active" : ""}`}>2 · 配置与确认</span>
      </div>

      {error && <div className="card error" style={{ marginTop: 10 }}>操作失败：{error}</div>}

      {step === 1 && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
            <p className="muted" style={{ flex: 1 }}>
              从项目下的开源项目（autolab / claudini / Arbor / AutoResearchClaw / ARA / Auto-claude / MLEvolve）
              中整理出的、具备明确数据集与评测指标的任务。
              <span className="pill ok">可实跑</span> 的任务（Titanic / Spaceship 及自定义表格分类）会直接驱动双循环；
              其余任务由各自 harness 在外部运行，平台做追踪记录。也可
              <b>注册自定义任务</b>（文本/图像/音频分类、LLM SFT/RL/OPD、Embedding 等）。
            </p>
            <button className="btn primary" onClick={() => setRegistering(true)}>
              ＋ 注册新任务
            </button>
          </div>
          {[...grouped.entries()].map(([cat, items]) => (
            <div key={cat} style={{ marginTop: 14 }}>
              <h3 style={{ borderBottom: "1px solid var(--border)", paddingBottom: 4 }}>
                {CATEGORY_LABELS[cat] || cat} <span className="muted">({items.length})</span>
              </h3>
              <div className="task-grid">
                {items.map((t) => (
                  <TaskCard
                    key={t.task_id}
                    t={t}
                    selected={selected?.task_id === t.task_id}
                    onSelect={() => setSelected(t)}
                    onDelete={() => handleDeleteTask(t)}
                  />
                ))}
              </div>
            </div>
          ))}
          <div className="row" style={{ marginTop: 16, justifyContent: "flex-end", gap: 10 }}>
            <button className="btn" onClick={onCancel}>
              取消
            </button>
            <button
              className="btn primary"
              disabled={!selected}
              onClick={() => setStep(2)}
            >
              下一步：配置
            </button>
          </div>
        </div>
      )}

      {step === 2 && selected && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <h2>{selected.name}</h2>
              <div className="muted mono">{selected.task_id}</div>
            </div>
            <button className="btn tiny" onClick={() => setStep(1)}>
              ← 重选任务
            </button>
          </div>

          <div className="grid2" style={{ marginTop: 12 }}>
            <div className="card">
              <h3>任务目标（参考）</h3>
              <div className="kv">
                <span className="muted">评测指标</span>
                <span className="mono">{selected.eval_metric} · {dirText(selected.direction)}</span>
              </div>
              <div className="kv">
                <span className="muted">baseline</span>
                <span className="mono">{selected.baseline}</span>
              </div>
              <div className="kv">
                <span className="muted">reference</span>
                <span className="mono">{selected.reference}</span>
              </div>
              <div className="kv">
                <span className="muted">通过门限</span>
                <span className="mono">{JSON.stringify(selected.gates || {})}</span>
              </div>
              <div className="kv">
                <span className="muted">数据集</span>
                <span>{selected.dataset_desc}</span>
              </div>
              <div className="kv">
                <span className="muted">运行方式</span>
                <span className="mono small">{selected.harness}</span>
              </div>
            </div>

            <ConfigForm
              task={selected}
              config={config}
              setConfig={setConfig}
              inner={inner}
              setInner={setInner}
              caps={caps}
            />
          </div>

          {selected.sandbox_isolation === "container-hard" && (
            <div className="iso-banner">
              <span>🐳</span>
              <span>
                <b>该任务将以容器隔离方式执行</b>：研究命令在一次性 Docker 容器内运行，数据只读挂载、禁用外网，
                最终结果作为 <b>EvalCompletedEvent</b> 回写控制平面（F3 硬隔离）。
              </span>
            </div>
          )}
          {selected.sandbox_isolation === "container-soft" && (
            <div className="iso-banner warn">
              <span>🛡️</span>
              <span>
                <b>该任务将以软隔离方式执行</b>（当前 Docker 不可用）：同一研究命令在宿主运行，仅做依赖/目录隔离，无 syscall/网络沙箱。
              </span>
            </div>
          )}
          {selected.sandbox_isolation === "host" && (
            <div className="iso-banner warn">
              <span>⚠️</span>
              <span>
                <b>当前未开启沙箱</b>（环境变量 AGENT_SANDBOX 未设置），该任务将在宿主直接运行，无隔离。
                如需容器隔离，请在启动后端时设置 <b>AGENT_SANDBOX=1</b> 并确保 Docker 可用。
              </span>
            </div>
          )}

          <div className="row" style={{ marginTop: 16, justifyContent: "flex-end", gap: 10 }}>
            <div style={{ marginRight: "auto", display: "flex", flexDirection: "column", gap: 4 }}>
              <label className="tool-item">
                <input type="checkbox" checked={autoRun} onChange={(e) => setAutoRun(e.target.checked)} />
                <span><b>立即运行完整实验</b></span>
              </label>
              <span className="muted small" style={{ paddingLeft: 24 }}>
                {autoRun
                  ? "创建后自动启动双循环：内循环实验 → 外审计验证 → 递归改进，直到通过或被预算耗尽。"
                  : "仅创建研究任务（状态：已请求）。可在仪表盘「调试」面板中先验证内/外循环，确认无误后再手动启动。"
                }
              </span>
            </div>
            <button className="btn" onClick={() => setStep(1)}>
              返回选择
            </button>
            <button className="btn primary" disabled={busy} onClick={handleLaunch}>
              {busy ? "启动中…" : autoRun ? "开始研究 →" : "创建并进入调试 →"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
