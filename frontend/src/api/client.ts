// Thin client over the control-plane REST API. All paths are relative to the /api
// proxy configured in vite.config.ts (which forwards to the FastAPI server).

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  try {
    return (await res.json()) as T;
  } catch (err) {
    console.error(`[API] Invalid JSON response from ${path}:`, err);
    throw new Error(`Invalid response from ${path}`);
  }
}

export async function apiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  try {
    return (await res.json()) as T;
  } catch (err) {
    console.error(`[API] Invalid JSON response from ${path}:`, err);
    throw new Error(`Invalid response from ${path}`);
  }
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

export const getRuns = () => apiGet<WorkflowRunSummary[]>("/workflow-runs");
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
export const getAudit = (runId: string) => apiGet<AuditEvent[]>(`/workflow-runs/${runId}/audit`);
export const getImprovements = (runId: string) =>
  apiGet<ImprovementEvent[]>(`/workflow-runs/${runId}/improvements`);
export const getHypoTree = (runId: string) =>
  apiGet<{ nodes: HypoNode[] }>(`/workflow-runs/${runId}/hypo-tree`);
export const getExperiences = () => apiGet<ExperienceEntry[]>("/experiences");
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
  /** Concise task objective, composed from core fields. */
  goal?: string;
  /** custom-registered tasks only */
  task_type?: string;
  type_config?: Record<string, any> | null;
}

export const getBenchmarkTasks = () => apiGet<BenchmarkTask[]>("/benchmark-tasks");

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
  const res = await fetch(`/api/benchmark-tasks/${encodeURIComponent(taskId)}`, {
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

export async function uploadDataset(file: File): Promise<UploadResult> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/benchmark-tasks/upload-dataset", { method: "POST", body: fd });
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

export const launchBenchmarkTask = (taskId: string, config: LaunchConfig = {}) =>
  apiPost<{
    run_id: string;
    task_id: string;
    supported_by_platform: boolean;
    status: string;
    message?: string;
  }>(
    `/benchmark-tasks/${encodeURIComponent(taskId)}/launch`,
    config as Record<string, unknown>,
  );

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
  artifacts: Record<string, any> | null;
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

export const reproduceRecord = (recordId: string) =>
  apiPost<{ run_id: string; task_id: string; from_record: string }>(
    `/research-records/${encodeURIComponent(recordId)}/reproduce`,
  );

/** Validate a task registration payload; returns errors without persisting. */
export const validateTask = (taskType: string, values: Record<string, any>) =>
  apiPost<ValidateResult>("/benchmark-tasks/validate", {
    task_type: taskType,
    values,
  });

/** Compute a metric for a tracked (non-executable) task from held-out eval/predictions. */
export const evaluateRun = (runId: string, params: Record<string, any>) =>
  apiPost<ResearchRecord>(
    `/workflow-runs/${encodeURIComponent(runId)}/evaluate`,
    params,
  );

/** Self-report a final metric for a tracked task (enters the leaderboard). */
export const reportRunMetric = (
  runId: string,
  params: { metric_name: string; direction: string; score: number; config_snapshot?: Record<string, any> },
) =>
  apiPost<ResearchRecord>(
    `/workflow-runs/${encodeURIComponent(runId)}/report-metric`,
    params,
  );

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
