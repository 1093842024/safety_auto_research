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
  constraints: Array<{ id: string; description: string; status: string; score?: number; note?: string }>;
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

const runSummarySchema = z.object({ run_id: z.string(), status: z.string() });

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

export const getBenchmarkTasks = async () => {
  const data = await apiGet<any[]>("/benchmark-tasks");
  for (const t of data) {
    if (typeof t?.task_id !== "string" || typeof t?.name !== "string") {
      console.error("[Schema Violation] getBenchmarkTasks: item missing task_id/name", t);
    }
  }
  return data as BenchmarkTask[];
};

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

/** Live validation result for a task-type registration form (does not persist). */
export interface ValidateResult {
  valid: boolean;
  errors: string[];
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
  requested: "已请求",
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
// ---------------------------------------------------------------------------
export const CATEGORY_LABELS: Record<string, string> = {
  model_dev: "模型开发",
  system_opt: "系统优化",
  puzzle: "谜题/挑战",
  cuda: "CUDA 内核",
  adversarial: "对抗/越狱",
  efficiency: "效率基准",
  agent_eval: "科研 Agent 评测",
  idea_eval: "想法质量评测",
  tooling: "工具型元评测",
  platform_native: "平台原生(可实跑)",
  custom: "自定义注册任务",
};

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

