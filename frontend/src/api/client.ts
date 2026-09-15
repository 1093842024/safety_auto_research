// Thin client over the control-plane REST API. All paths are relative to the /api
// proxy configured in vite.config.ts (which forwards to the FastAPI server).

import { z } from "zod";
import { WorkflowStatus } from "../contracts";

// Re-export the canonical platform status enum so views can pull the single
// source of truth from the client module instead of the generated file.
export { WorkflowStatus };

// ---------------------------------------------------------------------------
// Runtime validation — catches backend schema drift before it corrupts the UI.
// When a Zod parse fails we log a loud warning in dev/staging but still return
// the raw data so the UI doesn't break (the mismatch is likely to surface as a
// visible glitch rather than a hard crash).
// ---------------------------------------------------------------------------

/** Optional sink for schema-drift warnings; wired to the toast UI by App. */
type SchemaViolationHandler = ((msg: string) => void) | null;
let _schemaViolationHandler: SchemaViolationHandler = null;

/** Register a handler to surface schema violations in the UI (e.g. a toast). */
export function setSchemaViolationHandler(fn: SchemaViolationHandler) {
  _schemaViolationHandler = fn;
}

/**
 * Non-blocking parse: warns on schema drift but returns the raw data anyway.
 * Drift is surfaced via {@link setSchemaViolationHandler} so the UI shows a toast
 * instead of silently degrading into a glitching view.
 */
function safeParse<T>(schema: z.ZodType<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    const msg = `[Schema Violation] ${label}: ${result.error.issues
      .map((i) => `${i.path.join(".") || "?"} ${i.message}`)
      .join("; ")}`;
    console.error(msg, result.error.issues);
    if (_schemaViolationHandler) _schemaViolationHandler(msg);
    // Return the raw data — the caller keeps working with a (possibly partial) view.
    return data as T;
  }
  return result.data;
}

// Default request timeout. A hung backend (e.g. a long-running loop that should
// have been backgrounded) must not leave the UI in perpetual loading.
export const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Shared fetch plumbing: prepends the /api proxy and enforces a timeout via
 * AbortController (P2 frontend fix). Aborted requests surface a clear timeout
 * error instead of hanging forever.
 */
async function apiFetch(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`/api${path}`, { ...init, signal: controller.signal });
  } catch (err: any) {
    if (err?.name === "AbortError") {
      throw new Error(`请求超时（>${timeoutMs}ms）: ${path}`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function _parseJson<T>(res: Response, path: string): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  try {
    return (await res.json()) as T;
  } catch (err) {
    console.error(`[API] Invalid JSON response from ${path}:`, err);
    throw new Error(`Invalid response from ${path}`);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  return _parseJson<T>(await apiFetch(path, { headers: { Accept: "application/json" } }), path);
}

export async function apiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return _parseJson<T>(
    await apiFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    }),
    path,
  );
}

export interface WorkflowRunSummary {
  run_id: string;
  program_id: string;
  run_type: string;
  target_id: string;
  status: string;
  started_at?: string;
  ended_at?: string | null;
  status_detail?: string | null;
  objective_snapshot?: {
    name?: string;
    category?: string;
    config?: Record<string, any>;
    [key: string]: any;
  };
}

/** A single infrastructure-layer capability, as discovered via /agent/protocol. */
export interface CapabilityInfo {
  capability_id: string;
  title: string;
  layer_name?: string;
  description?: string;
}

export interface AuditEvent {
  run_id: string;
  audit_id: string;
  audited_target: string;
  confidence: number;
  recoverable: boolean;
  gate_passed: boolean;
  recommendation?: string;
  unresolved_claims: string[];
  rejected_candidates: string[];
  /** Built-in constraints PLUS one entry per rubric criterion (``id="rubric:<Cn>"``).
   *  The rubric entries carry the extra `criterion_id` / `dimension` / `priority` /
   *  `evaluated_by` fields so the console can show *which* criterion failed and how it
   *  was judged (programmatically vs by an LLM judge). */
  constraints: Array<{
    id: string;
    description: string;
    status: string;
    score?: number;
    note?: string;
    criterion_id?: string;
    dimension?: string;
    priority?: string;
    satisfaction_condition?: string;
    check_kind?: string;
    evaluated_by?: string;
    blocked_reason?: string;
    weight?: number;
  }>;
}

/** A researcher follow-up (clarification / question) on a single audit constraint (F6). */
export interface AuditFollowup {
  event_id: string;
  run_id: string;
  audit_id: string;
  constraint_id: string;
  question?: string | null;
  clarification?: string | null;
  prior_status: string;
  new_status: string;
  new_score: number;
  response?: string | null;
  confidence: number;
  recommendation: string;
  resolved: boolean;
  occurred_at?: string;
}

/** Payload for POST .../audits/{audit_id}/followup. */
export interface AuditFollowupPayload {
  constraint_id: string;
  question?: string | null;
  clarification?: string | null;
}

export interface ImprovementEvent {
  improvement_id: string;
  target_mechanism: string;
  rollback_id: string;
  validated_heldout: boolean;
  reverted: boolean;
  metrics_before: Record<string, number>;
  metrics_after: Record<string, number>;
}

export interface HypoNode {
  node_id: string;
  parent_id: string | null;
  hypothesis: string;
  evidence_refs: string[];
  insight: string;
  score: number;
  status: string;
  branch: string;
}

export interface ExperienceEntry {
  entry_id: string;
  kind: string;
  context: string;
  lesson: string;
  applicable_stages: string[];
  confidence: number;
}

// NOTE: zod strips fields not declared here on a SUCCESSFUL parse, so every field
// the UI renders must be declared explicitly (missing one silently blanks the UI —
// e.g. run names / categories once disappeared because only run_id+status were kept).
const runSummarySchema = z.object({
  run_id: z.string(),
  status: z.string(),
  target_id: z.string().optional(),
  started_at: z.string().optional(),
  ended_at: z.string().nullable().optional(),
  status_detail: z.string().nullable().optional(),
  objective_snapshot: z.any().optional(),
});

export const getRuns = async () => {
  const data = await apiGet<any[]>("/workflow-runs");
  // Non-blocking schema check (logs drift, never throws) via safeParse.
  return data.map((r) => safeParse(runSummarySchema, r, "getRuns.item")) as WorkflowRunSummary[];
};
export const getRun = (id: string) =>
  apiGet<
    WorkflowRunSummary & {
      objective_snapshot?: any;
      started_at?: string;
      ended_at?: string;
    }
  >(`/workflow-runs/${encodeURIComponent(id)}`);
export const getEvents = (runId: string) =>
  apiGet<Array<Record<string, any>>>(`/events?run_id=${encodeURIComponent(runId)}`);
const auditItemSchema = z.object({ audit_id: z.string() });

export const getAudit = async (runId: string) => {
  const data = await apiGet<any[]>(`/workflow-runs/${runId}/audit`);
  return data.map((a) => safeParse(auditItemSchema, a, "getAudit.item")) as AuditEvent[];
};

// ----- F6: audit follow-up interaction protocol -----
export const followupAudit = (runId: string, auditId: string, payload: AuditFollowupPayload) =>
  apiPost<AuditFollowup>(
    `/workflow-runs/${encodeURIComponent(runId)}/audits/${encodeURIComponent(auditId)}/followup`,
    payload as unknown as Record<string, unknown>,
  );

export const getAuditFollowups = (runId: string, auditId: string) =>
  apiGet<AuditFollowup[]>(
    `/workflow-runs/${encodeURIComponent(runId)}/audits/${encodeURIComponent(auditId)}/followups`,
  );
export const getImprovements = (runId: string) =>
  apiGet<ImprovementEvent[]>(`/workflow-runs/${runId}/improvements`);
export const getHypoTree = (runId: string) =>
  apiGet<{ nodes: HypoNode[] }>(`/workflow-runs/${runId}/hypo-tree`);
export const getProtocol = () => apiGet<Record<string, any>>("/agent/protocol");

// ----- Benchmark task catalog (mined from the OSS projects) -----
export interface BenchmarkTask {
  task_id: string;
  name: string;
  source_project: string;
  category: string;
  sub_category?: string;
  modality: string;
  dataset_desc: string;
  eval_metric: string;
  direction: string;
  baseline: number | null;
  reference: number | null;
  gates: Record<string, number | string>;
  harness: string;
  run_command: string;
  source_path: string;
  tags: string[];
  supported_by_platform: boolean;
  note: string;
  /** How the task is evaluated (script / procedure); kept as core info for agent execution. */
  eval_method?: string;
  /** "platform" = runs natively on the dual loop; "agent" = executed by the agent (docker/Arbor deps stripped). */
  execution_mode?: string;
  /** Isolation level at launch: "none" | "container-hard" | "container-soft" | "host". */
  sandbox_isolation?: string;
  /** Concise task objective, composed from core fields. */
  goal?: string;
  /** Research background / motivation: what studying this task actually means. */
  background?: string;
  /** 指标详解：含义 / 计算方式 / 参考实现（评估指标目录条目）。 */
  metric_detail?: MetricInfo | null;
  /** custom-registered tasks only */
  task_type?: string;
  type_config?: Record<string, any> | null;
  /** Availability: false => grayed out (data >1 GiB / external dep / LLM weights). */
  enabled?: boolean;
  /** Why the task is unavailable (shown when enabled === false). */
  unavailable_reason?: string;
  /** Lower bound on train+eval data volume in bytes (null = unknown/external). */
  data_size_bytes?: number | null;
}

// ----- Evaluation-metric catalog (评估指标 tab / 任务指标详解) -----
export interface MetricInfo {
  metric_id: string;
  name: string;
  /** 指标含义：度量什么、为什么这样度量 */
  description: string;
  /** 计算方式：公式 / 计算流程 / 评测协议 */
  computation: string;
  direction: string;
  typical_range: string;
  applicable: string[];
  /** metric_lib 标准参考实现的 Python 源码 */
  implementation: string;
  library: string;
}

export const getMetricCatalog = () =>
  apiGet<MetricInfo[]>("/benchmark-metrics");

export const getMetricDetail = (metricId: string) =>
  apiGet<MetricInfo>(`/benchmark-metrics/${encodeURIComponent(metricId)}`);

export const getBenchmarkTasks = async () => {
  const data = await apiGet<any[]>("/benchmark-tasks");
  for (const t of data) {
    if (typeof t?.task_id !== "string" || typeof t?.name !== "string") {
      console.error("[Schema Violation] getBenchmarkTasks: item missing task_id/name", t);
    }
  }
  return data as BenchmarkTask[];
}

// ----- Task data preview (sample cases + volume stats, read-only) -----
export interface DataPreviewFile {
  path: string;
  size_bytes: number;
  error?: string;
  preview?: {
    kind: "csv" | "jsonl" | "json" | "npz";
    columns?: string[] | null;
    dtypes?: string[] | null;
    rows?: number | null;
    samples?: any[][];
    arrays?: Array<{ name: string; shape: number[]; dtype: string }>;
    kv_counts?: Record<string, number>;
  } | null;
}

export interface TaskDataPreview {
  task_id: string;
  found: boolean;
  dirs: string[];
  files: DataPreviewFile[];
  file_count?: number;
  total_bytes: number;
  note?: string;
}

export const getTaskDataPreview = (taskId: string) =>
  apiGet<TaskDataPreview>(
    `/benchmark-tasks/${encodeURIComponent(taskId)}/data-preview`,
  );

// ----- System settings: agent-CLI model query / selection (系统设置) -----
export interface AgentSettings {
  cli: string;
  cli_label: string;
  /** Empty string = use the CLI's own default (Opus main + Haiku fallback). */
  model: string;
  model_explicit: boolean;
  /** Active API key state (raw keys never leave the backend). */
  api_key_set?: boolean;
  api_key_masked?: string;
  api_base_url?: string;
  /** Currently enabled account resolved from the saved list ("" = CLI 登录态). */
  active_account?: { label: string; masked: string; base_url: string };
  saved_keys?: ApiKeyEntry[];
  note?: string;
}

/** A saved tclaude account (API key), masked for display. */
export interface ApiKeyEntry {
  label: string;
  masked: string;
  base_url?: string;
  active: boolean;
}

/** Result of a live API-key probe (minimal real turn through the CLI). */
export interface ApiKeyValidateResult {
  /** true = valid / false = invalid / null = inconclusive (e.g. timeout). */
  valid: boolean | null;
  detail: string;
  latency_ms: number | null;
}

/** CLI 自身登录态检测（tclaude login 的同步结果；身份本地不可见，仅测可用性）。 */
export interface CliLoginState {
  logged_in: boolean | null;
  detail: string;
  latency_ms: number | null;
  cached?: boolean;
}

export const getCliLoginState = (refresh = false) =>
  apiGet<CliLoginState>(
    `/settings/agent/cli-login${refresh ? "?refresh=true" : ""}`,
  );

export interface AgentModels {
  models: string[];
  /** "cli-probe" (live from the CLI) | "cache" | "fallback" */
  source: string;
  cli: string;
  cli_label?: string;
}

export const getAgentSettings = () =>
  apiGet<AgentSettings>("/settings/agent");

export const updateAgentSettings = (
  model?: string,
  extra?: { api_key?: string; api_base_url?: string },
) =>
  apiPut<{ ok: boolean; model: string }>("/settings/agent", {
    ...(model !== undefined ? { model } : {}),
    ...extra,
  });

export const getAgentModels = () =>
  apiGet<AgentModels>("/settings/agent/models");

// ----- System settings: agent-CLI API key accounts (tclaude 多账号) -----
export const saveApiKey = (label: string, apiKey: string, base_url?: string) =>
  apiPost<{ ok: boolean; saved_keys: ApiKeyEntry[] }>(
    "/settings/agent/apikeys",
    { label, api_key: apiKey, ...(base_url ? { base_url } : {}) },
  );

export const deleteApiKey = (label: string) =>
  apiPost<{ ok: boolean; saved_keys: ApiKeyEntry[] }>(
    "/settings/agent/apikeys/delete",
    { label },
  );

export const activateApiKey = (label: string) =>
  apiPost<{ ok: boolean; saved_keys: ApiKeyEntry[] }>(
    "/settings/agent/apikeys/activate",
    { label },
  );

/** Validate a raw key (not yet saved), a saved account by label, or the CLI login. */
export const validateApiKey = (params: { label?: string; api_key?: string; base_url?: string }) =>
  apiPost<ApiKeyValidateResult>("/settings/agent/apikeys/validate", params);

// ----- System settings: local machine environment detection (系统设置) -----
export interface GpuDevice {
  name: string;
  driver_version: string;
  memory_total_mb: number | null;
  memory_used_mb: number | null;
  utilization_pct: number | null;
  temperature_c: number | null;
}

export interface SystemEnvironment {
  docker: {
    available: boolean;
    daemon_running: boolean;
    path?: string;
    version?: string;
    server_version?: string;
    reason?: string;
  };
  gpu: {
    available: boolean;
    path?: string;
    cuda_version?: string;
    devices?: GpuDevice[];
    reason?: string;
  };
  agent_cli: {
    configured_cli: string;
    configured_label: string;
    configured_available: boolean;
    agent_command_set: boolean;
    clis: Array<{
      name: string;
      available: boolean;
      path: string;
      version: string;
      version_probe_ok?: boolean;
      configured?: boolean;
    }>;
  };
  host: {
    os: string;
    python: string;
    cpu_count: number | null;
    mem_total_gb: number | null;
  };
}

export const getSystemEnvironment = () =>
  apiGet<SystemEnvironment>("/settings/environment");

// ----- Research-skill knowledge base (研究 Skill · AREX-Skill) -----
export interface SkillEntry {
  name: string;
  description: string;
  /** repositories | task_oriented */
  category: string;
  /** 仓库名（repositories）或基准名（task_oriented，如 PaperBench） */
  group: string;
  /** 相对分组路径（sub-skills / skills/<source>/…） */
  sub: string;
  /** 研究大类（Computer Vision / Biomedical AI / …，来自官方 catalog） */
  domain: string;
  subdomain?: string;
  disco_role?: string;
  /** SKILL.md 的相对路径（详情查询键） */
  path: string;
}

export interface SkillSearchResult {
  total: number;
  items: SkillEntry[];
}

export interface SkillTree {
  total: number;
  categories: Record<string, number>;
  /** 大类 → 研究大类（domain）→ 仓库/基准 → 数量 */
  tree: Record<string, Record<string, Record<string, number>>>;
}

export interface SkillFileEntry {
  path: string;
  size: number;
}

export interface SkillDetail {
  found: boolean;
  path: string;
  meta: Record<string, string>;
  body: string;
  truncated: boolean;
  sub_skills: string[];
  files: SkillFileEntry[];
}

export interface SkillFileContent {
  found: boolean;
  binary?: boolean;
  path?: string;
  content?: string;
  truncated?: boolean;
  size?: number;
}

export const getSkillTree = () => apiGet<SkillTree>("/research-skills/tree");

export const searchSkills = (params: {
  q?: string;
  category?: string;
  group?: string;
  domain?: string;
  limit?: number;
  offset?: number;
}) => {
  const usp = new URLSearchParams();
  if (params.q) usp.set("q", params.q);
  if (params.category) usp.set("category", params.category);
  if (params.group) usp.set("group", params.group);
  if (params.domain) usp.set("domain", params.domain);
  usp.set("limit", String(params.limit ?? 50));
  usp.set("offset", String(params.offset ?? 0));
  return apiGet<SkillSearchResult>(`/research-skills?${usp.toString()}`);
};

export const getSkillFile = (path: string) =>
  apiGet<SkillFileContent>(
    `/research-skills/file?path=${encodeURIComponent(path)}`,
  );

export const getSkillDetail = (path: string) =>
  apiGet<SkillDetail>(
    `/research-skills/detail?path=${encodeURIComponent(path)}`,
  );

async function apiPut<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  return (await res.json()) as T;
}

// ----- Dataset management (数据集管理: register / inspect local datasets) -----
export interface DatasetRecord {
  dataset_id: string;
  name: string;
  modality: string; // text | image | audio
  task_kind: string; // classification | llm_generation
  data_path: string;
  label_file?: string;
  label_field?: string;
  content_field?: string;
  format: string;
  notes?: string;
  created_at: string;
  num_samples?: number | null;
  size_bytes?: number;
  columns?: string[] | null;
  samples?: any[][];
  label_stats?: Record<string, number> | null;
  media_file_count?: number | null;
  label_file_stats?: { rows: number; columns: string[] } | null;
  notes_list?: string[];
}

export interface DatasetRegisterInput {
  name: string;
  modality: string;
  task_kind: string;
  data_path: string;
  label_file?: string;
  label_field?: string;
  content_field?: string;
  notes?: string;
}

export const getDatasets = () => apiGet<DatasetRecord[]>("/datasets");

export const registerDataset = (input: DatasetRegisterInput) =>
  apiPost<DatasetRecord>("/datasets", input as unknown as Record<string, unknown>);

export const getDataset = (id: string) =>
  apiGet<DatasetRecord>(`/datasets/${encodeURIComponent(id)}`);

export const deleteDataset = (id: string) =>
  apiDelete<{ deleted: string }>(`/datasets/${encodeURIComponent(id)}`);

/** A single step in the agent's inner-loop execution trace ("Agent 执行流水"). */
export interface AgentTraceStep {
  seq: number;
  kind: "tool_call" | "final" | string;
  tool: string | null;
  args_summary: string;
  result_summary: string;
  detail?: string | null;
}

export const getAgentTrace = (runId: string) =>
  apiGet<AgentTraceStep[]>(`/workflow-runs/${encodeURIComponent(runId)}/agent-trace`);

// ----- Custom task registration (schema-driven) -----

/** One form field of a task-type registration spec (rendered dynamically). */
export interface FieldSpec {
  key: string;
  label: string;
  type: "text" | "number" | "select" | "multiselect" | "textarea" | "tags" | "bool" | "path" | "upload";
  group: "basic" | "data" | "model" | "train" | "eval";
  required?: boolean;
  default?: any;
  options?: Array<{ value: string; label: string }>;
  help?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  accept?: string;
}

/** A registrable research-task type (text/image/audio cls, SFT, RL, OPD, embedding...). */
export interface TaskTypeSpec {
  type_id: string;
  label: string;
  icon: string;
  modality: string;
  harness: string;
  executable: boolean;
  summary: string;
  default_metric: { eval_metric: string; direction: string };
  fields: FieldSpec[];
  common_fields: FieldSpec[];
}

export const getTaskTypes = () => apiGet<TaskTypeSpec[]>("/benchmark-tasks/task-types");

export const registerBenchmarkTask = (taskType: string, values: Record<string, any>) =>
  apiPost<BenchmarkTask>("/benchmark-tasks/register", { task_type: taskType, values });

export async function deleteBenchmarkTask(taskId: string): Promise<void> {
  const res = await apiFetch(`/benchmark-tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* keep default */
    }
    throw new Error(detail);
  }
}

export interface UploadResult {
  path: string;
  dir: string;
  filename: string;
  extracted: boolean;
  size_bytes: number;
}

// Large dataset uploads need a far longer timeout than the 30s default.
export async function uploadDataset(file: File, timeoutMs = 10 * 60_000): Promise<UploadResult> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await apiFetch(
    "/benchmark-tasks/upload-dataset",
    { method: "POST", body: fd },
    timeoutMs,
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* keep default */
    }
    throw new Error(detail);
  }
  return (await res.json()) as UploadResult;
}

/** Researcher-facing configuration of the inner research loop. */
export interface InnerLoopConfig {
  mode?: "scripted" | "agent";
  preset?: string | null;
  model?: string;
  fe?: "basic" | "rich";
  cv_folds?: number;
  threshold?: number | null;
  drop_cols?: string[];
  data_dir?: string | null;
  /** 优化指标覆盖（评估指标目录中的 metric_id）；null/undefined = 任务默认指标。 */
  eval_metric?: string | null;
  // agent-mode only (forwarded to a RemoteAgentHarness when configured)
  system_prompt?: string;
  skills?: string[];
  tools?: string[];
  step_plan?: string[];
  /** Which agent CLI drives the inner loop when mode == "agent": "codex" | "claude_code" | "auto". */
  agent_cli?: "codex" | "claude_code" | "auto" | null;
  /** Human-in-the-loop collaboration mode: "autonomous" | "step_confirm" | "outer_confirm". */
  collaboration_mode?: "autonomous" | "step_confirm" | "outer_confirm";
}

/** Researcher-facing configuration for launching a benchmark task as a research run. */
export interface LaunchConfig {
  audit_threshold?: number;
  max_outer_iters?: number;
  model?: string;
  fe?: boolean;
  inner_loop?: InnerLoopConfig;
  /** Agent CLI selection for agent mode: "codex" | "claude_code" | "auto" (env AGENT_COMMAND). */
  agent_cli?: "codex" | "claude_code" | "auto" | null;
}

export const launchBenchmarkTask = (taskId: string, config: LaunchConfig = {}, noAutoRun?: boolean) => {
  const path = `/benchmark-tasks/${encodeURIComponent(taskId)}/launch`;
  const url = noAutoRun ? `${path}?auto_run=false` : path;
  return apiPost<{
    run_id: string;
    task_id: string;
    supported_by_platform: boolean;
    status: string;
    message?: string;
  }>(url, config as Record<string, unknown>);
};

// ----- Research records (best-3 per task + global leaderboard) -----

/** One autonomous-research result record, persisted per run (auto-captured or reported). */
export interface ResearchRecord {
  record_id: string;
  task_id: string;
  run_id: string;
  metric_name: string;
  direction: string;
  score: number;
  config_snapshot: Record<string, any> | null;
  artifacts: Array<{artifact_id: string}> | null;
  is_top3: boolean;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Rubric stage (layer_12): task-specific executable scoring rubric + review of
// a declared evaluation standard along 准确性 / 完整性 / 科学性.
// ---------------------------------------------------------------------------

/** One defect found while auditing a declared evaluation standard. */
export interface RubricFinding {
  finding_id: string;
  /** "accuracy" | "completeness" | "scientificity" */
  dimension: string;
  /** "critical" | "important" | "minor" */
  severity: string;
  message: string;
  suggestion?: string;
  field?: string;
}

/** Three-dimensional audit of a task's declared evaluation standard. */
export interface RubricReview {
  review_id: string;
  task_id: string;
  /** false => nothing usable was declared, so a rubric is auto-generated instead. */
  standard_provided: boolean;
  accuracy: number;
  completeness: number;
  scientificity: number;
  overall: number;
  /** "sound" | "acceptable" | "needs_work" | "unusable" */
  verdict: string;
  findings: RubricFinding[];
  suggested_fixes: Record<string, any>;
  summary: string;
}

/** One executable criterion of a task-specific rubric. */
export interface RubricCriterion {
  criterion_id: string;
  goal_ids?: string[];
  requirement: string;
  /** "correctness" | "generalization" | "rigor" | "integrity" | "reporting" */
  dimension: string;
  /** "high" | "medium" | "low" */
  priority: string;
  satisfaction_condition: string;
  metrics?: string[];
  required_analysis?: string[];
  comparisons?: string[];
  weight?: number;
  check?: Record<string, any>;
  /** Non-empty => the requirement was retained but cannot be measured here. */
  blocked_reason?: string;
}

/** An atomic scientific goal derived from the task instruction. */
export interface RubricGoal {
  goal_id: string;
  title: string;
  requirement: string;
  instruction_evidence?: string[];
}

/** The frozen executable rubric a run is graded against. */
export interface ExecutableRubric {
  rubric_id: string;
  task_id: string;
  /** "synthesized" (auto-generated) | "reviewed" (declared standard, audited) */
  source: string;
  objective?: string;
  goals: RubricGoal[];
  criteria: RubricCriterion[];
  claims_to_avoid: string[];
  provided_standard?: Record<string, any>;
  review?: RubricReview | null;
  /** "rule_engine" | "llm" | "llm+rule_engine" */
  generator?: string;
  frozen?: boolean;
  integrity_hash?: string;
  /** Only on the run-level endpoint: does the rebuild match the recorded run hash? */
  hash_matches_run?: boolean;
}

/** Compact preview of the rubric a registration form would produce. */
export interface RubricPreview {
  rubric_id: string;
  source: string;
  criteria_count: number;
  machine_checkable_count: number;
  criteria: Array<
    Pick<RubricCriterion, "criterion_id" | "requirement" | "dimension" | "priority" | "satisfaction_condition"> & {
      check_kind: string;
      blocked_reason?: string;
    }
  >;
  claims_to_avoid: string[];
}

/** Registration-time rubric stage output (review + what would be generated). */
export interface RubricStageResult {
  review: RubricReview | null;
  rubric_preview: RubricPreview | null;
}

/** One criterion verdict recorded on an audit (extends the audit constraint shape). */
export interface RubricCriterionVerdict {
  id: string;
  criterion_id: string;
  description: string;
  /** "verified" | "partial" | "conflict" | "missing" */
  status: string;
  score: number;
  note?: string;
  dimension?: string;
  priority?: string;
  satisfaction_condition?: string;
  check_kind?: string;
  /** "programmatic:<kind>" | "judge" — how the verdict was reached. */
  evaluated_by?: string;
  blocked_reason?: string;
  weight?: number;
}

/** Per-iteration criterion roll-up for a run. */
export interface RubricIteration {
  iteration: number;
  audit_id: string;
  confidence: number;
  gate_passed: boolean;
  passed: number;
  failed: number;
  criteria: RubricCriterionVerdict[];
}

/** GET /workflow-runs/{id}/rubric */
export interface RunRubric {
  run_id: string;
  rubric: ExecutableRubric | null;
  event: Record<string, any> | null;
  iterations: RubricIteration[];
}

/** Audit a declared evaluation standard + preview the induced rubric (no persistence). */
export const reviewTaskStandard = (taskType: string, values: Record<string, any>) =>
  apiPost<RubricStageResult>("/benchmark-tasks/review-standard", {
    task_type: taskType,
    values,
  });

/** The executable rubric for a catalog task (identical to what a run is graded against). */
export const getTaskRubric = (taskId: string) =>
  apiGet<ExecutableRubric>(`/benchmark-tasks/${encodeURIComponent(taskId)}/rubric`);

/** The frozen rubric a run is graded against + per-iteration criterion verdicts. */
export const getRunRubric = (runId: string) =>
  apiGet<RunRubric>(`/workflow-runs/${encodeURIComponent(runId)}/rubric`);

export const RUBRIC_VERDICT_LABEL: Record<string, string> = {
  sound: "健全",
  acceptable: "可接受",
  needs_work: "需完善",
  unusable: "不可用",
};

export const RUBRIC_VERDICT_CLASS: Record<string, string> = {
  sound: "ok",
  acceptable: "ok",
  needs_work: "warn",
  unusable: "bad",
};

export const RUBRIC_SEVERITY_LABEL: Record<string, string> = {
  critical: "严重",
  important: "重要",
  minor: "轻微",
};

export const RUBRIC_SEVERITY_CLASS: Record<string, string> = {
  critical: "bad",
  important: "warn",
  minor: "accent",
};

export const RUBRIC_DIMENSION_LABEL: Record<string, string> = {
  accuracy: "准确性",
  completeness: "完整性",
  scientificity: "科学性",
  correctness: "正确性",
  generalization: "泛化性",
  rigor: "严谨性",
  integrity: "真实性",
  reporting: "汇报质量",
};

export const RUBRIC_STATUS_LABEL: Record<string, string> = {
  verified: "通过",
  partial: "部分满足",
  conflict: "未通过",
  missing: "缺证据",
};

export const RUBRIC_STATUS_CLASS: Record<string, string> = {
  verified: "ok",
  partial: "warn",
  conflict: "bad",
  missing: "bad",
};

/** Live validation result for a task-type registration form (does not persist). */
export interface ValidateResult {
  valid: boolean;
  errors: string[];
  /** Rubric stage: audit of the declared evaluation standard (advisory, never blocks). */
  review?: RubricReview | null;
  /** Rubric stage: the executable rubric this task definition would produce. */
  rubric_preview?: RubricPreview | null;
}

export const getResearchRecords = (taskId?: string) =>
  apiGet<ResearchRecord[]>(
    taskId
      ? `/research-records?task_id=${encodeURIComponent(taskId)}`
      : "/research-records",
  );

export const getLeaderboard = () =>
  apiGet<ResearchRecord[]>("/research-records/leaderboard");

export const reproduceRecord = (recordId: string, autostart = false) =>
  apiPost<{ run_id: string; task_id: string; from_record: string; autostarted?: boolean; note?: string }>(
    `/research-records/${encodeURIComponent(recordId)}/reproduce${autostart ? "?autostart=true" : ""}`,
  );

// ---------------------------------------------------------------------------
// Harness-engineering observability (Phase 1-3): playbook / strategies / evolution
// ---------------------------------------------------------------------------

/** One ACE playbook entry (itemized evolving context bullet). */
export interface PlaybookEntry {
  entry_id: string;
  scope: string;
  section: string;
  content: string;
  helpful: number;
  harmful: number;
  run_id?: string | null;
  iter_no: number;
}

/** One meta-loop strategy archive entry (propose-apply-verify-rollback lifecycle). */
export interface StrategyEntry {
  rollback_id: string;
  run_id?: string;
  status?: string;
  inner_param_patch?: Record<string, unknown>;
  prediction?: { baseline?: number; min_delta?: number; tolerance?: number; claim?: string };
  actual_accuracy?: number;
  actual_delta?: number;
}

/** One evolution candidate (Phase 3 population search). */
export interface EvolutionCandidate {
  candidate_id: string;
  run_id: string;
  params: Record<string, unknown>;
  generation: number;
  parent_id?: string | null;
  branch: string;
  fitness?: number | null;
  novelty: number;
  status: string;
  metrics?: Record<string, unknown>;
  offspring_count: number;
  // Phase A / OpenMLE-Evo: program-level grain (code candidates) -- optional on config nodes
  node_kind?: string;
  code?: string | null;
  operator?: string | null;
  parent_ids?: string[] | null;
}

export const getPlaybook = (scope?: string) =>
  apiGet<PlaybookEntry[]>(scope ? `/playbook?scope=${encodeURIComponent(scope)}` : "/playbook");

export const getStrategies = (runId?: string) =>
  apiGet<StrategyEntry[]>(runId ? `/strategies?run_id=${encodeURIComponent(runId)}` : "/strategies");

export const getEvolution = (runId: string) =>
  apiGet<EvolutionCandidate[]>(`/workflow-runs/${encodeURIComponent(runId)}/evolution`);

// ---------------------------------------------------------------------------
// Program-level evolutionary search (OpenRSI island model) — drives
// `run_program_evolutionary_loop` over the *code* space. The launch endpoint is
// backgrounded (mirrors the config-evolution / MEA endpoints); the dedicated GET
// returns only the `node_kind="program"` candidates for the island view.
// ---------------------------------------------------------------------------

/** Configuration for a program-level evolution launch. All fields optional. */
export interface ProgramEvolutionConfig {
  /** OpenMLETaskConfig fields (name/data_dir/target/id_col/direction/...). Empty -> bundled titanic preset. */
  task_config?: Record<string, any>;
  /** "template" (default, fully offline+deterministic) | "llm" (OpenAI-compatible chat API). */
  backend_type?: "template" | "llm";
  /** Backend credentials for backend_type="llm" (api_key/base_url/model/tme_open). */
  backend_config?: Record<string, any>;
  /** Number of islands in the OpenRSI model (1–16). */
  islands?: number;
  /** Candidates per island (1–32). */
  pop_per_island?: number;
  /** Generations per island (1–50). */
  generations?: number;
  /** Parallel workers (1–8). */
  max_workers?: number;
  /** Candidate novelty cutoff (0–1). */
  novelty_threshold?: number;
  /** Whether to run the outer audit at the end. */
  audit?: boolean;
  /** Extra audit kwargs. */
  audit_params?: Record<string, any>;
  /** Step/iteration budget. */
  budget?: Record<string, any>;
  /** RNG seed. */
  seed?: number;
}

export const launchProgramEvolution = (runId: string, config: ProgramEvolutionConfig = {}) =>
  apiPost<{ run_id: string; status: string; accepted: boolean }>(
    `/workflow-runs/${encodeURIComponent(runId)}/program-evolution`,
    config as Record<string, unknown>,
  );

/** Program-node candidates (code operators + island lineage) for a run. */
export const getProgramEvolution = (runId: string) =>
  apiGet<EvolutionCandidate[]>(`/workflow-runs/${encodeURIComponent(runId)}/program-evolution`);

/** Validate a task registration payload; returns errors without persisting. */
export const validateTask = (taskType: string, values: Record<string, any>) =>
  apiPost<ValidateResult>("/benchmark-tasks/validate", {
    task_type: taskType,
    values,
  });

/** A richer run object, including the launch-time configuration stored in objective_snapshot. */
export interface RunDetail extends WorkflowRunSummary {
  objective_snapshot?: {
    name?: string;
    benchmark_task_id?: string;
    eval_metric?: string;
    direction?: string;
    baseline?: number | null;
    reference?: number | null;
    gates?: Record<string, number | string>;
    dataset_desc?: string;
    harness?: string;
    execution_mode?: string;
    supported_by_platform?: boolean;
    config?: LaunchConfig;
    [key: string]: unknown;
  };
  started_at?: string;
  ended_at?: string;
  status_detail?: string | null;
}

// ---------------------------------------------------------------------------
// Debug: isolate one stage (inner/outer) of the dual loop
// ---------------------------------------------------------------------------

export interface DebugResult {
  stage: "inner" | "outer";
  ok: boolean;
  summary: string;
  metrics?: Record<string, number>;
  verdict?: {
    confidence: number;
    recoverable: boolean;
    gate_passed: boolean;
    unresolved: string[];
    rejected: string[];
    recommendation?: string;
  } | null;
  report_ref?: string | null;
  detail?: string | null;
  error?: string | null;
}

export const debugRun = (runId: string, stage: "inner" | "outer", auditInputOverride?: Record<string, any>) =>
  apiPost<{ run_id: string; stage: string; status: string }>(
    `/workflow-runs/${encodeURIComponent(runId)}/debug`,
    { stage, audit_input_override: auditInputOverride || null },
  );

// ---------------------------------------------------------------------------
// Run full experiment (for existing requested runs)
// ---------------------------------------------------------------------------

export const runExperiment = (runId: string) =>
  apiPost<{ run_id: string; status: string; collaboration_mode?: string }>(
    `/workflow-runs/${encodeURIComponent(runId)}/run-experiment`,
  );

/** Request cancellation of a running workflow (sets the cancel event checked by the loop). */
export const cancelRun = (runId: string) =>
  apiPost<{ run_id: string; status: string }>(
    `/workflow-runs/${encodeURIComponent(runId)}/cancel`,
  );

// ----- Run liveness: 运行中 run 是真在跑还是僵尸（状态未感知的异常终止） -----
export type LivenessVerdict =
  | "active" // 执行线程存活且事件在更新
  | "stale" // 线程在但长时间无事件（疑似卡死）
  | "zombie" // 无执行线程（异常终止遗留）
  | "waiting_approval"
  | "idle";

export interface RunLiveness {
  status: string;
  driver_alive: boolean;
  last_activity_at: string | null;
  stale_minutes: number | null;
  verdict: LivenessVerdict;
  reason: string;
}

/** Liveness snapshot for all non-terminal runs, keyed by run_id. */
export const getRunsLiveness = () =>
  apiGet<Record<string, RunLiveness>>("/workflow-runs/liveness");

/** Delete a TERMINAL run + its persisted objects and on-disk scratch files. */
export const deleteRun = (runId: string) =>
  apiDelete<{ run_id: string; deleted: Record<string, number> }>(
    `/workflow-runs/${encodeURIComponent(runId)}`,
  );

export interface ArtifactInfo {
  artifact_id: string;
  artifact_type: string;
  uri: string;
  schema_version: string;
  producer_ref: string;
  integrity_hash: string;
}

export const getRunArtifacts = (id: string) =>
  apiGet<ArtifactInfo[]>(`/workflow-runs/${encodeURIComponent(id)}/artifacts`);

/** 下载研究包（run 配置 + 指标 + 事件 + 产物 + 决策），用于分析 / 迁移。 */
export async function exportRunBundle(id: string): Promise<void> {
  const res = await fetch(`/api/workflow-runs/${encodeURIComponent(id)}/export`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${id}_research_bundle.json`;
  a.click();
  URL.revokeObjectURL(url);
}

async function apiDelete<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { method: "DELETE" });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep default */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Human-in-the-loop collaboration: resolve a paused step
// ---------------------------------------------------------------------------

export interface CollaborationAdjustments {
  model?: string | null;
  fe?: "basic" | "rich" | null;
  cv_folds?: number | null;
  threshold?: number | null;
  audit_threshold?: number | null;
  data_dir?: string | null;
  action?: "continue" | "abort" | "restart";
  note?: string | null;
}

export interface ResolveCollaborationPayload {
  resolution: "approved" | "rejected";
  reviewer?: string;
  adjustments?: CollaborationAdjustments | null;
}

export const resolveCollaboration = (runId: string, payload: ResolveCollaborationPayload) =>
  apiPost<{ status: string; run_id: string; resolution: string }>(
    `/workflow-runs/${encodeURIComponent(runId)}/resolve-collaboration`,
    payload as unknown as Record<string, unknown>,
  );

// ---------------------------------------------------------------------------
// Shared display constants — single source of truth for status/decision labels.
// Views import these instead of defining their own copies.
// ---------------------------------------------------------------------------

export const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  // requested = 仅创建了 run 记录（登记目标/配置），从未启动执行，后台无任何线程。
  requested: "已创建（未启动）",
  waiting_approval: "等待审批",
  succeeded: "成功",
  failed: "失败",
  exited_budget: "已完成 · 已达最大轮数",
  exited_converged: "已完成 · 审计通过",
  cancelled: "已取消",
};

export const STATUS_CLASS: Record<string, string> = {
  running: "warn",
  requested: "accent",
  waiting_approval: "accent",
  succeeded: "ok",
  failed: "bad",
  exited_budget: "ok",
  exited_converged: "ok",
  cancelled: "bad",
};

/** Run statuses from which nothing can change server-side — stop polling/streaming.
 *  Mirrors ``platform_contracts/transitions.py`` (states with no successors). */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "succeeded",
  "failed",
  "exited_budget",
  "exited_converged",
  "cancelled",
]);

export const DECISION_LABEL: Record<string, string> = {
  accept: "ACCEPT · 接受",
  revisit: "REFINE · 复核",
  restart: "RESTART · 重启",
  exit_success: "EXIT_SUCCESS · 通过",
  continue: "继续",
  audit_refine: "REFINE · 复核",
  audit_restart: "RESTART · 重启",
};

export const DECISION_CLASS: Record<string, string> = {
  accept: "ok",
  revisit: "warn",
  restart: "bad",
  exit_success: "ok",
  continue: "accent",
  audit_refine: "warn",
  audit_restart: "bad",
};

// ---------------------------------------------------------------------------
// Canonical task-category labels — single source of truth (superset of all
// views that previously defined their own copies of this map).
// v2 分类体系（2026-09）：主分类只描述「任务研究什么」；执行方式 / 来源等
// 正交属性不再混入 category。旧分类（LEGACY_CATEGORIES）不做映射，
// 历史研究记录在侧边栏统一归入「归档（旧分类）」组。
// ---------------------------------------------------------------------------
export const CATEGORY_LABELS: Record<string, string> = {
  ml_modeling: "机器学习建模",
  perf_opt: "性能与效率优化",
  safety_adversarial: "安全与对抗",
  agent_eval: "智能体能力评测",
};

export const SUBCATEGORY_LABELS: Record<string, string> = {
  tabular: "表格建模",
  sandbox: "沙箱建模",
  kernel: "算子与内核",
  algo: "算法加速",
  compression: "模型/编码压缩",
  llm_systems: "LLM 训练与服务",
  attack: "攻击与越狱",
  scientific_discovery: "科学发现",
  ml_engineering: "ML 工程",
  open_research: "开放式科研",
  meta_eval: "元评测",
};

// v1 分类（已退役）。历史 objective_snapshot 里可能仍存有这些值；
// 按约定不做新旧映射，统一折叠为「归档」分组展示。
export const LEGACY_CATEGORIES: ReadonlySet<string> = new Set([
  "model_dev",
  "system_opt",
  "puzzle",
  "cuda",
  "adversarial",
  "efficiency",
  "idea_eval",
  "tooling",
  "platform_native",
  "custom",
]);

export const ARCHIVE_CATEGORY_GROUP = "archive";
export const ARCHIVE_CATEGORY_LABEL = "归档（旧分类）";

/** Display label for a raw category id (legacy ids collapse to the archive label). */
export const categoryLabel = (c: string): string =>
  c === ARCHIVE_CATEGORY_GROUP || LEGACY_CATEGORIES.has(c)
    ? ARCHIVE_CATEGORY_LABEL
    : CATEGORY_LABELS[c] || c;

/** Grouping key for a raw category id (all legacy ids share one archive group). */
export const categoryGroup = (c: string): string =>
  LEGACY_CATEGORIES.has(c) ? ARCHIVE_CATEGORY_GROUP : c || "未分类";

// ---------------------------------------------------------------------------
// Dev guard: every canonical WorkflowStatus must have a display label. Surfaces
// contract drift immediately (a blank status pill) instead of failing silently.
// ---------------------------------------------------------------------------
for (const s of WorkflowStatus.options as readonly string[]) {
  if (!(s in STATUS_LABEL)) {
    console.warn(`[contracts] STATUS_LABEL is missing a label for WorkflowStatus "${s}"`);
  }
}

// ---------------------------------------------------------------------------
// Research-record comparison + HITL approval resolution, wrapped over the raw
// REST endpoints so every call funnels through the shared apiGet/apiPost path
// (instead of ad-hoc raw fetch calls scattered across views).
// ---------------------------------------------------------------------------
export const compareRuns = (ids: string[]) =>
  apiGet<Array<Record<string, any>>>(`/research-records/compare?ids=${encodeURIComponent(ids.join(","))}`);

export const resolveApproval = (runId: string, resolution: "approved" | "rejected") =>
  apiPost<Record<string, any>>(`/workflow-runs/${encodeURIComponent(runId)}/resolve-approval`, {
    resolution,
    resolved_by: "frontend_user",
  });

// ---------------------------------------------------------------------------
// B Flywheel (Phase 1.5): one-iteration badcase retrain + regression gate.
// POST runs a single iteration synchronously; GET replays the persisted history.
// ---------------------------------------------------------------------------

/** Configuration for one flywheel iteration (mirrors ``FlywheelRequest``). */
export interface FlywheelConfig {
  preset?: string;
  target?: string | null;
  model?: string;
  fe?: "basic" | "rich" | string;
  drop_cols?: string[];
  data_dir?: string | null;
  badcase_path?: string | null;
  badcase_ratio?: number;
  regression_tol?: number;
  eval_metric?: string;
  heldout_frac?: number;
  heldout_seed?: number;
}

/** Result of one flywheel iteration (POST /workflow-runs/{id}/flywheel). */
export interface FlywheelResult {
  run_id: string;
  stage_run_id: string;
  gate_result: string;
  badcase_collected: boolean;
  badcase_path?: string | null;
  regression_passed: boolean;
  badcase_improved: boolean;
  verdict: "ACCEPT" | "REJECT";
  detail: string;
  metrics: Record<string, number>;
}

/** One persisted flywheel iteration (GET /workflow-runs/{id}/flywheel). */
export interface FlywheelIteration {
  event_id: string;
  stage_run_id: string | null;
  eval_suite_id: string;
  passed: boolean;
  gate_passed: boolean;
  occurred_at?: string;
  metrics: Record<string, number>;
}

/** Create a workflow run (used to spin up a FLYWHEEL run before iterating). */
export const createWorkflowRun = (config: {
  program_id?: string;
  run_type: string;
  entry_stage?: string;
  target_id?: string;
  objective_snapshot?: Record<string, unknown>;
}) =>
  apiPost<{ run_id: string }>("/workflow-runs", config as Record<string, unknown>);

export const runFlywheel = (runId: string, config: FlywheelConfig = {}) =>
  apiPost<FlywheelResult>(
    `/workflow-runs/${encodeURIComponent(runId)}/flywheel`,
    config as Record<string, unknown>,
  );

export const getFlywheelIterations = (runId: string) =>
  apiGet<FlywheelIteration[]>(`/workflow-runs/${encodeURIComponent(runId)}/flywheel`);

// ---------------------------------------------------------------------------
// Flywheel scheduler (event-driven auto-trigger): polls the badcase CSV and
// fires ``run_capability(badcase_retrain)`` when the row count crosses a threshold.
// ---------------------------------------------------------------------------

export interface FlywheelScheduleConfig {
  badcase_path: string;
  threshold?: number;
  poll_interval_sec?: number;
  auto_clear_after_trigger?: boolean;
  preset?: string;
  target?: string | null;
  model?: string;
  fe?: string;
  drop_cols?: string[];
  data_dir?: string | null;
  badcase_ratio?: number;
  regression_tol?: number;
  eval_metric?: string;
  heldout_frac?: number;
  heldout_seed?: number;
}

export interface FlywheelSchedulerStatus {
  run_id: string;
  scheduled: boolean;
  started_at?: number;
  last_check_at?: number;
  last_badcase_count?: number;
  last_trigger_at?: number;
  trigger_count?: number;
  last_verdict?: string;
  last_detail?: string;
  error?: string;
  config?: FlywheelScheduleConfig;
}

export const startFlywheelScheduler = (runId: string, config: FlywheelScheduleConfig) =>
  apiPost<{ run_id: string; scheduled: boolean; threshold: number; poll_interval_sec: number; started_at: number }>(
    `/workflow-runs/${encodeURIComponent(runId)}/flywheel/schedule`,
    config as unknown as Record<string, unknown>,
  );

export const getFlywheelScheduler = (runId: string) =>
  apiGet<FlywheelSchedulerStatus>(`/workflow-runs/${encodeURIComponent(runId)}/flywheel/schedule`);

export const stopFlywheelScheduler = (runId: string) =>
  fetch(`/api/workflow-runs/${encodeURIComponent(runId)}/flywheel/schedule`, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  }).then(async (res) => {
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  });

// ---------------------------------------------------------------------------
// LLM provider config + auto-label debug surface (B-flywheel step 2).
// ---------------------------------------------------------------------------

export interface LlmModelInfo {
  id: string;
  label: string;
  family: string;
}

export interface LlmProviderInfo {
  base_url: string;
  api_key_masked: string;
  model: string;
  temperature: number;
  max_tokens: number;
  timeout_sec: number;
}

export interface LlmProviderRequest {
  base_url?: string | null;
  api_key?: string | null;
  model?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  timeout_sec?: number | null;
}

export const getLlmModels = () =>
  apiGet<{ models: LlmModelInfo[]; default: LlmProviderInfo }>("/agent/llm/models");

export const getLlmProvider = () => apiGet<LlmProviderInfo>("/agent/llm/provider");

export const chatLlm = (req: { system: string; user: string; provider?: LlmProviderRequest; model?: string | null }) =>
  apiPost<{ ok: boolean; text?: string; model?: string; error?: string; provider?: LlmProviderInfo }>(
    "/agent/llm/chat",
    req as Record<string, unknown>,
  );

export const testLlmLabel = (req: {
  rows: Array<Record<string, unknown>>;
  system_prompt?: string;
  user_template?: string;
  provider?: LlmProviderRequest;
  model?: string | null;
  batch_size?: number;
}) =>
  apiPost<{
    ok: boolean;
    error?: string;
    provider?: LlmProviderInfo;
    labels: Array<{ row: Record<string, unknown>; label: string; raw: string; error?: string | null }>;
  }>("/agent/llm/test-label", req as Record<string, unknown>);

