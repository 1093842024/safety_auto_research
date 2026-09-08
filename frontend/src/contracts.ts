import { z } from "zod";

// =============================================================================
// Auto-generated unified platform contracts (objects + events + enums).
// Source of truth: safety_auto_research/platform_contracts.
// Do NOT edit by hand; regenerate with export_typescript.py.
// =============================================================================

// ----- Enums -----
export const ArtifactType = z.enum(['paper_set', 'idea_card', 'design_spec', 'dataset_release', 'code_patch', 'model_checkpoint', 'eval_report', 'attack_report', 'lesson_card', 'audit_report', 'improvement_report', 'hypothesis_tree', 'experience_bank'] as const);
export type ArtifactType = z.infer<typeof ArtifactType>;


export const DecisionType = z.enum(['continue', 'revisit', 'exit_success', 'exit_budget', 'exit_converged', 'hitl_required'] as const);
export type DecisionType = z.infer<typeof DecisionType>;


export const EventType = z.enum(['program_created', 'workflow_requested', 'workflow_started', 'stage_queued', 'stage_started', 'artifact_published', 'gate_passed', 'gate_failed', 'eval_completed', 'attack_completed', 'decision_issued', 'lesson_promoted', 'approval_required', 'approval_resolved', 'workflow_finished', 'stage_cancelled', 'audit_completed', 'audit_followup', 'improvement_applied', 'agent_step', 'debug_result'] as const);
export type EventType = z.infer<typeof EventType>;


export const GateResult = z.enum(['pending', 'passed', 'failed', 'waived'] as const);
export type GateResult = z.infer<typeof GateResult>;


export const Modality = z.enum(['text', 'image', 'audio', 'video', 'code', 'multimodal'] as const);
export type Modality = z.infer<typeof Modality>;


export const PiiStatus = z.enum(['unknown', 'redacted', 'blocked', 'approved_sensitive'] as const);
export type PiiStatus = z.infer<typeof PiiStatus>;


export const ProgramStatus = z.enum(['draft', 'active', 'paused', 'archived'] as const);
export type ProgramStatus = z.infer<typeof ProgramStatus>;


export const RegistryStatus = z.enum(['candidate', 'approved', 'production', 'archived'] as const);
export type RegistryStatus = z.infer<typeof RegistryStatus>;


export const RiskTier = z.enum(['low', 'medium', 'high', 'critical'] as const);
export type RiskTier = z.infer<typeof RiskTier>;


export const RunType = z.enum(['standard_research', 'badcase_retrain', 'adversarial_hardening', 'flywheel'] as const);
export type RunType = z.infer<typeof RunType>;


export const StageStatus = z.enum(['queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled'] as const);
export type StageStatus = z.infer<typeof StageStatus>;


export const TargetType = z.enum(['classifier', 'judge', 'router', 'rag_guard', 'agent_guard', 'multimodal_detector'] as const);
export type TargetType = z.infer<typeof TargetType>;


export const Visibility = z.enum(['private', 'restricted', 'internal', 'public'] as const);
export type Visibility = z.infer<typeof Visibility>;


export const WorkflowStatus = z.enum(['requested', 'running', 'waiting_approval', 'succeeded', 'failed', 'exited_budget', 'exited_converged', 'cancelled'] as const);
export type WorkflowStatus = z.infer<typeof WorkflowStatus>;


// ----- Core objects -----
export const ResearchProgramSchema = z.object({
  program_id: z.string(),
  name: z.string(),
  domain: z.string(),
  owner: z.string(),
  goal_statement: z.string(),
  risk_tier: RiskTier,
  budget_policy_ref: z.string(),
  default_policy_pack_ref: z.string(),
  status: ProgramStatus,
});
export type ResearchProgram = z.infer<typeof ResearchProgramSchema>;

export const SafetyTargetSchema = z.object({
  target_id: z.string(),
  target_type: TargetType,
  modality: Modality,
  task_family: z.string(),
  capabilities: z.array(z.string()).optional(),
  threat_model_refs: z.array(z.string()).optional(),
  acceptance_policy_ref: z.string(),
});
export type SafetyTarget = z.infer<typeof SafetyTargetSchema>;

export const WorkflowRunSchema = z.object({
  run_id: z.string(),
  program_id: z.string(),
  run_type: RunType,
  entry_stage: z.string(),
  target_id: z.string(),
  objective_snapshot: z.record(z.string(), z.any()).optional(),
  requested_outcomes: z.array(z.string()).optional(),
  status: WorkflowStatus,
  started_at: z.string(),
  ended_at: z.string().optional(),
  status_detail: z.string().optional(),
});
export type WorkflowRun = z.infer<typeof WorkflowRunSchema>;

export const StageRunSchema = z.object({
  stage_run_id: z.string(),
  run_id: z.string(),
  stage_code: z.string(),
  input_refs: z.array(z.string()).optional(),
  output_refs: z.array(z.string()).optional(),
  executor_family: z.string(),
  reviewer_family: z.string().optional(),
  gate_result: GateResult,
  status: StageStatus,
  retry_count: z.number().int().min(0).optional(),
});
export type StageRun = z.infer<typeof StageRunSchema>;

export const ArtifactSchema = z.object({
  artifact_id: z.string(),
  artifact_type: ArtifactType,
  uri: z.string(),
  schema_version: z.string(),
  producer_ref: z.string(),
  lineage_parent_ids: z.array(z.string()).optional(),
  integrity_hash: z.string(),
  visibility: Visibility,
  compliance_tags: z.array(z.string()).optional(),
});
export type Artifact = z.infer<typeof ArtifactSchema>;

export const DatasetReleaseSchema = z.object({
  dataset_id: z.string(),
  artifact_ref: z.string(),
  source_mix: z.record(z.string(), z.number()).optional(),
  label_schema_version: z.string(),
  pii_status: PiiStatus,
  split_policy: z.string(),
  retention_policy: z.string(),
  quality_report_ref: z.string(),
  leakage_report_ref: z.string(),
});
export type DatasetRelease = z.infer<typeof DatasetReleaseSchema>;

export const ModelVersionSchema = z.object({
  model_id: z.string(),
  base_model: z.string(),
  adapter_stack: z.array(z.string()).optional(),
  training_recipe_ref: z.string(),
  safety_capabilities: z.array(z.string()).optional(),
  artifact_ref: z.string(),
  registry_status: RegistryStatus,
});
export type ModelVersion = z.infer<typeof ModelVersionSchema>;

export const EvalSuiteSchema = z.object({
  eval_suite_id: z.string(),
  suite_type: z.string(),
  task_refs: z.array(z.string()).optional(),
  metric_defs: z.record(z.string(), z.string()).optional(),
  pass_thresholds: z.record(z.string(), z.number()).optional(),
  sandbox_policy_ref: z.string(),
  canary_policy_ref: z.string(),
});
export type EvalSuite = z.infer<typeof EvalSuiteSchema>;

export const AttackCampaignSchema = z.object({
  campaign_id: z.string(),
  target_model_id: z.string(),
  attack_taxonomy_refs: z.array(z.string()).optional(),
  generation_policy_ref: z.string(),
  risk_controls: z.array(z.string()).optional(),
  success_metrics: z.record(z.string(), z.any()).optional(),
  replay_buffer_ref: z.string().optional(),
});
export type AttackCampaign = z.infer<typeof AttackCampaignSchema>;

export const DecisionRecordSchema = z.object({
  decision_id: z.string(),
  run_id: z.string(),
  decision_type: DecisionType,
  target_stage: z.string().optional(),
  reason_codes: z.array(z.string()).optional(),
  evidence_refs: z.array(z.string()).optional(),
  policy_hits: z.array(z.string()).optional(),
  approved_by: z.array(z.string()).optional(),
});
export type DecisionRecord = z.infer<typeof DecisionRecordSchema>;

export const LessonCardSchema = z.object({
  lesson_id: z.string(),
  scope: z.string(),
  source_run_id: z.string(),
  pattern_type: z.string(),
  applicable_stages: z.array(z.string()).optional(),
  confidence: z.number().min(0.0).max(1.0),
  payload_ref: z.string(),
  prm_score: z.number().min(0.0).max(1.0),
});
export type LessonCard = z.infer<typeof LessonCardSchema>;

export const PolicyPackSchema = z.object({
  policy_pack_id: z.string(),
  domain: z.string(),
  rules: z.record(z.string(), z.any()).optional(),
  required_approvals: z.array(z.string()).optional(),
  model_separation_policy: z.string(),
  data_compliance_policy: z.string(),
  redteam_constraints: z.string(),
});
export type PolicyPack = z.infer<typeof PolicyPackSchema>;

export const AuditReportSchema = z.object({
  audit_id: z.string(),
  audited_target: z.string(),
  constraints: z.array(z.record(z.string(), z.any())).optional(),
  unresolved_claims: z.array(z.string()).optional(),
  rejected_candidates: z.array(z.string()).optional(),
  confidence: z.number().min(0.0).max(1.0),
  recoverable: z.boolean().optional(),
  audit_confidence: z.number().min(0.0).max(1.0),
  recommendation: z.string(),
  report_ref: z.string(),
  followups: z.array(z.record(z.string(), z.any())).optional(),
});
export type AuditReport = z.infer<typeof AuditReportSchema>;

export const ImprovementProposalSchema = z.object({
  improvement_id: z.string(),
  target_mechanism: z.string(),
  title: z.string(),
  description: z.string(),
  patch: z.record(z.string(), z.any()).optional(),
  rationale: z.string(),
  expected_effect: z.string(),
  rollback_id: z.string(),
  proposed_by: z.string().optional(),
});
export type ImprovementProposal = z.infer<typeof ImprovementProposalSchema>;

export const HypothesisNodeSchema = z.object({
  node_id: z.string(),
  parent_id: z.string().optional(),
  hypothesis: z.string(),
  evidence_refs: z.array(z.string()).optional(),
  artifact_ref: z.string().optional(),
  insight: z.string().optional(),
  score: z.number().min(0.0).max(1.0).optional(),
  status: z.string().optional(),
  branch: z.string().optional(),
  node_kind: z.string().optional(),
  run_id: z.string().optional(),
});
export type HypothesisNode = z.infer<typeof HypothesisNodeSchema>;

export const ExperienceEntrySchema = z.object({
  entry_id: z.string(),
  kind: z.string(),
  context: z.string(),
  lesson: z.string(),
  applicable_stages: z.array(z.string()).optional(),
  confidence: z.number().min(0.0).max(1.0).optional(),
  uses: z.number().int().optional(),
  seq: z.number().int().optional(),
  source_run_id: z.string().optional(),
});
export type ExperienceEntry = z.infer<typeof ExperienceEntrySchema>;

// ----- Event models -----
export const BasePlatformEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType,
  run_id: z.string(),
  occurred_at: z.string().optional(),
});
export type BasePlatformEvent = z.infer<typeof BasePlatformEventSchema>;

export const WorkflowStatusChangedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType,
  run_id: z.string(),
  occurred_at: z.string().optional(),
  from_status: WorkflowStatus,
  to_status: WorkflowStatus,
});
export type WorkflowStatusChangedEvent = z.infer<typeof WorkflowStatusChangedEventSchema>;

export const StageStatusChangedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType,
  run_id: z.string(),
  occurred_at: z.string().optional(),
  stage_run_id: z.string(),
  from_status: StageStatus,
  to_status: StageStatus,
});
export type StageStatusChangedEvent = z.infer<typeof StageStatusChangedEventSchema>;

export const DecisionRecordedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  decision_id: z.string(),
  decision_type: DecisionType,
});
export type DecisionRecordedEvent = z.infer<typeof DecisionRecordedEventSchema>;

export const ArtifactPublishedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  artifact_id: z.string(),
  artifact_type: ArtifactType,
});
export type ArtifactPublishedEvent = z.infer<typeof ArtifactPublishedEventSchema>;

export const ApprovalRequiredEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  approval_id: z.string(),
  subject_type: z.string(),
  subject_ref: z.string(),
  reason: z.string(),
  policy_ref: z.string(),
  required_roles: z.array(z.string()).optional(),
  risk_tier: z.string().optional(),
});
export type ApprovalRequiredEvent = z.infer<typeof ApprovalRequiredEventSchema>;

export const ApprovalResolvedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  approval_id: z.string(),
  resolution: z.string(),
  resolved_by: z.string(),
  decision_ref: z.string().optional(),
  note: z.string().optional(),
});
export type ApprovalResolvedEvent = z.infer<typeof ApprovalResolvedEventSchema>;

export const EvalCompletedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  eval_suite_id: z.string(),
  stage_run_id: z.string().optional(),
  passed: z.boolean(),
  metrics: z.record(z.string(), z.number()).optional(),
  gate_passed: z.boolean().optional(),
  report_ref: z.string(),
});
export type EvalCompletedEvent = z.infer<typeof EvalCompletedEventSchema>;

export const AttackCompletedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  campaign_id: z.string(),
  target_model_id: z.string(),
  stage_run_id: z.string().optional(),
  success_rate: z.number().min(0.0).max(1.0),
  total_attempts: z.number().int().min(0),
  successful_attempts: z.number().int().min(0),
  retention_rate: z.number().min(0.0).max(1.0).optional(),
  forgetting_rate: z.number().min(0.0).max(1.0).optional(),
  vulnerability_patterns: z.array(z.string()).optional(),
  report_ref: z.string(),
});
export type AttackCompletedEvent = z.infer<typeof AttackCompletedEventSchema>;

export const LessonPromotedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  lesson_id: z.string(),
  source_run_id: z.string(),
  scope: z.string(),
  pattern_type: z.string(),
  prm_score: z.number().min(0.0).max(1.0),
  confidence: z.number().min(0.0).max(1.0),
  applicable_stages: z.array(z.string()).optional(),
  payload_ref: z.string(),
});
export type LessonPromotedEvent = z.infer<typeof LessonPromotedEventSchema>;

export const AuditCompletedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  audit_id: z.string(),
  audited_target: z.string(),
  constraints: z.array(z.record(z.string(), z.any())).optional(),
  unresolved_claims: z.array(z.string()).optional(),
  rejected_candidates: z.array(z.string()).optional(),
  confidence: z.number().min(0.0).max(1.0),
  recoverable: z.boolean().optional(),
  audit_confidence: z.number().min(0.0).max(1.0),
  gate_passed: z.boolean().optional(),
  report_ref: z.string(),
});
export type AuditCompletedEvent = z.infer<typeof AuditCompletedEventSchema>;

export const AuditFollowupEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  audit_id: z.string(),
  constraint_id: z.string(),
  question: z.string().optional(),
  clarification: z.string().optional(),
  prior_status: z.string(),
  new_status: z.string(),
  new_score: z.number().min(0.0).max(1.0),
  response: z.string().optional(),
  confidence: z.number().min(0.0).max(1.0),
  recommendation: z.string(),
  resolved: z.boolean().optional(),
});
export type AuditFollowupEvent = z.infer<typeof AuditFollowupEventSchema>;

export const AgentStepEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  seq: z.number().int().min(0).optional(),
  kind: z.string(),
  tool: z.string().optional(),
  args_summary: z.string().optional(),
  result_summary: z.string().optional(),
  detail: z.string().optional(),
});
export type AgentStepEvent = z.infer<typeof AgentStepEventSchema>;

export const DebugEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  stage: z.string(),
  ok: z.boolean().optional(),
  summary: z.string().optional(),
  metrics: z.record(z.string(), z.number()).optional(),
  verdict: z.record(z.string(), z.any()).optional(),
  report_ref: z.string().optional(),
  detail: z.string().optional(),
  error: z.string().optional(),
});
export type DebugEvent = z.infer<typeof DebugEventSchema>;

export const ImprovementAppliedEventSchema = z.object({
  event_id: z.string().optional(),
  event_type: EventType.optional(),
  run_id: z.string(),
  occurred_at: z.string().optional(),
  improvement_id: z.string(),
  target_mechanism: z.string(),
  proposal_ref: z.string(),
  rollback_id: z.string(),
  validated_heldout: z.boolean().optional(),
  metrics_before: z.record(z.string(), z.number()).optional(),
  metrics_after: z.record(z.string(), z.number()).optional(),
  reverted: z.boolean().optional(),
});
export type ImprovementAppliedEvent = z.infer<typeof ImprovementAppliedEventSchema>;
