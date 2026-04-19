/** Shared TypeScript types for the documentary pipeline frontend. */

export interface Scene {
  scene_num: number;
  title: string;
  duration_sec: number;
  voices: VoiceBlock[];
  visual_notes: string;
  dopamine_hook: string;
}

export interface VoiceBlock {
  voice: "V1" | "V2" | "V3";
  text: string;
  tone: string;
}

export interface VisualConcept {
  scene_num: number;
  phrase_idx: number;
  start_time: number;
  end_time: number;
  duration: number;
  prompt: string;
  prompt_reasoning?: string;
  lora_id: string;
  lora_weight: number;
  camera_style: string;
  environment: string;
  mood: string;
  quality?: string;
  qa_reason?: string;
  attempts?: number;
  status?: string;
}

export interface AlignmentWord {
  word: string;
  start: number;
  end: number;
}

export interface AlignmentData {
  words: AlignmentWord[];
  total_duration: number;
  word_count: number;
}

export interface TimelineTrack {
  name: string;
  kind: string;
  clips: TimelineClip[];
  gaps: TimelineGap[];
  total_clips: number;
  total_gaps: number;
}

export interface TimelineClip {
  name: string;
  duration: number;
  metadata: Record<string, unknown>;
}

export interface TimelineGap {
  name: string;
  metadata: Record<string, unknown>;
}

export interface TimelineStatus {
  timeline_name: string;
  tracks: TimelineTrack[];
}

export interface PipelineSnapshot {
  run_id: string;
  topic: string;
  status: string;
  elapsed_sec: number;
  active_phase: string | null;
  phases_completed: number;
  total_tools: number;
  total_llm_calls: number;
  force_end: boolean;
  recent_events: PipelineEvent[];
}

export interface PipelineEvent {
  type: string;
  name?: string;
  tool?: string;
  agent?: string;
  status?: string;
  duration?: number;
  result_chars?: number;
  time: number;
}

export interface QAResult {
  valid: boolean;
  phase: string;
  errors?: string;
  message?: string;
  details?: Record<string, unknown>[];
}

export type PipelinePhase =
  | "idle"
  | "scenario"
  | "audio"
  | "visual_direction"
  | "production"
  | "assembly"
  | "completed";

export interface LoRAEntry {
  lora_id: string;
  description: string;
  tags: string[];
  default_weight: number;
  weight_range: [number, number];
  best_for: string;
  avoid_for: string;
  transition_affinity: string[];
}

export interface ToolCallInfo {
  tool_name: string;
  agent: string;
  args: Record<string, unknown>;
  result?: string;
  duration?: number;
  status: "running" | "completed" | "error";
}

// AG-UI types — artifact feedback, escalations, recovery

export interface Artifact {
  id: string;
  type: "video_clip" | "narration" | "scene_script" | "visual_concept" | "assembled_video";
  status: "generating" | "pending_review" | "approved" | "rejected" | "regenerating";
  scene_num: number;
  phrase_idx: number;
  language: string;
  preview_url: string;
  duration_sec: number;
  qa_scores: Record<string, string>;
  metadata: Record<string, unknown>;
  timestamp: number;
}

export interface Escalation {
  id: string;
  operation_name: string;
  error_chain: EscalationAttempt[];
  diagnosis: EscalationDiagnosis;
  proposed_actions: ProposedAction[];
  severity: "warning" | "critical";
  timestamp: number;
  resolved: boolean;
  response: Record<string, unknown> | null;
}

export interface EscalationAttempt {
  level: string;
  attempt: number;
  error: string;
  strategy: string;
  timestamp: number;
  success: boolean;
}

export interface EscalationDiagnosis {
  root_cause: string;
  confidence: "confirmed" | "likely" | "possible";
  checks: Record<string, unknown>[];
  proposed_fix: string;
  proposed_action: string;
}

export interface ProposedAction {
  action_id: string;
  description: string;
  risk_level: "low" | "medium" | "high";
}

// ---------------------------------------------------------------------------
// ARCH-H1 / ARCH-H2 / ARCH-H3 — OTIO centrepiece timeline types
// ---------------------------------------------------------------------------

/** Lifecycle state of the OTIO timeline. ``authoritative`` locks the scale
 * and drops the reconciliation overlay. */
export type OtioState = "draft" | "authoritative";

/** Per-slot lifecycle used on the centrepiece dashboard.
 *
 * ``gap`` is a real OTIO gap segment (pacing silence), rendered as empty
 * space — not a failure state. */
export type SlotStatus =
  | "pending"
  | "in_progress"
  | "delivered"
  | "failed"
  | "gap";

/** Scale-accurate slot on one of the three canonical tracks. */
export interface OtioSlot {
  slot_id: string;
  track: "V1_Video" | "A1_Narration" | "A2_Music";
  scene_num: number;
  phrase_idx: number;
  start_sec: number;
  duration_sec: number;
  status: SlotStatus;
  label: string;
  preview_url: string;
  thumbnail_url: string;
  waveform_url: string;
  failure_reason: string;
  rung: string;
  scripted_duration_sec: number | null;
  measured_duration_sec: number | null;
  metadata: Record<string, unknown>;
}

export interface OtioTrack {
  name: "V1_Video" | "A1_Narration" | "A2_Music";
  kind: "video" | "audio";
  slots: OtioSlot[];
  total_slots: number;
}

export interface OtioReconciliationRow {
  slot_id: string;
  scene_num: number;
  phrase_idx: number;
  start_sec: number;
  scripted_duration_sec: number;
  measured_duration_sec: number | null;
  skew_sec: number | null;
}

export interface OtioTimelineStatus {
  state: OtioState;
  total_duration_sec: number;
  tracks: OtioTrack[];
  reconciliation: OtioReconciliationRow[];
  source_file: string;
}

/** UI-03a (#198): approval_gate_opened / _closed pipeline events.
 *
 * Emitted by ``callbacks.approval_gate.wait_for_approval`` on entry and
 * exit so the inline approval card on the OTIO timeline (UI-03b, #199)
 * and the narrator chat surface (UI-01) can drive off the unified AG-UI
 * event bus.  Paired: one ``approval_gate_opened`` per stage entry and
 * exactly one ``approval_gate_closed`` when the gate flips via
 * ``/agui/approve`` or via a stage-scoped directive (UI-03c, #200).
 */
export interface ApprovalGateEvent {
  stage: string;
  opened_at?: number;
  closed_at?: number;
  decision?: "approved" | "timeout" | "error" | string;
  reviewer?: string;
  boundary_slot_id?: string;
}

/** UI-05a: lightweight per-step progress event for the drift badge. */
export interface ReManifestationProgressEvent {
  plan_id: string;
  stage_name: string;
  action: string;
  artifact_key: string;
  scene_id: string | null;
  clip_id: string | null;
  scene_num: number | null;
  slot_ids: string[];
  reason: string;
  status: string;
  error: string | null;
  phase: "start" | "complete" | "failed" | string;
}

/** UI-05a: the "directive accepted, these slots are drifting" event. */
export interface DirectiveAppliedEvent {
  directive_text: string;
  l4_event_id: string;
  reviewer: string;
  ledger_record_ids: number[];
  records: Array<Record<string, unknown>>;
  drifted_slot_ids: string[];
  drifted_scene_nums: number[];
  scope: Record<string, unknown> | null;
  re_manifestation_plans: Array<Record<string, unknown>>;
}

/** Frontend-only drift state derived from SSE events; never persisted. */
export interface DriftState {
  /** Slot ids currently re-manifesting (amber outline + badge). */
  slotIds: Set<string>;
  /** Scene numbers whose exact phrase index was unknown (scene-wide drift). */
  sceneNums: Set<number>;
  /** Per-slot latest in-flight stage label, used by the badge copy. */
  slotStages: Record<string, string>;
}

export interface SlotStateEvent {
  slot_id: string;
  track: OtioTrack["name"];
  scene_num: number;
  phrase_idx: number;
  status: SlotStatus;
  artifact_id: string;
  artifact_status: string;
  preview_url: string;
  duration_sec: number;
  qa_scores: Record<string, number>;
}

export interface SlotDetailView {
  slot_id: string;
  track: OtioTrack["name"];
  scene_num: number;
  phrase_idx: number;
  artifact_history: Array<Record<string, unknown>>;
  qa_verdicts: Array<Record<string, unknown>>;
  reasoning_digests: Array<Record<string, unknown>>;
  ledger_records: Array<Record<string, unknown>>;
  current_rung: Record<string, unknown>;
  latest_preview: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// UI-04 — full slot drilldown (issue #189 / #201 / #204)
// ---------------------------------------------------------------------------

/** A single artifact revision recorded against a slot.
 *
 * ``outcome`` is the normalised lifecycle: ``accepted`` / ``rejected`` /
 * ``pending`` / ``regenerating`` / ``generating`` / ``failed``. ``b2_url``
 * is the canonical download URL for the take's media when B2 uploads are
 * present; otherwise it falls back to ``preview_url``.
 */
export interface SlotTake {
  revision: number;
  artifact_id: string;
  status: string;
  outcome: string;
  timestamp: number | null;
  preview_url: string;
  b2_url: string;
  qa_scores: Record<string, string | number>;
  ledger_revision_at_derivation: { revision: number } | null;
}

/** One LLM critic's structured perspective on the current artifact. */
export interface SlotCritique {
  source: string;
  voter_model: string;
  rating: "EXCELLENT" | "GOOD" | "FAIR" | "POOR" | "UNKNOWN";
  score: number | null;
  summary: string;
  issues: string[];
  suggestions: string[];
  timestamp?: number;
  iteration?: number;
}

/** One deterministic QA evaluator verdict. */
export interface SlotQaResult {
  source: string;
  status: "pass" | "warn" | "escalate" | "fail";
  score: number | null;
  summary: string;
  measurements: Record<string, number | string>;
  timestamp?: number;
  // Loose shape — evaluators emit heterogeneous fields.
  [key: string]: unknown;
}

/** A media artifact reference (preview / waveform / thumbnail / take). */
export interface SlotArtifactRef {
  kind: "preview" | "thumbnail" | "waveform" | "take";
  url: string;
  label: string;
  revision?: number;
  outcome?: string;
}

/** Full drilldown payload returned by ``GET /api/slots/{slot_id}/full``. */
export interface SlotFullView {
  slot: OtioSlot;
  takes: SlotTake[];
  critiques: SlotCritique[];
  qa_results: SlotQaResult[];
  artifacts: SlotArtifactRef[];
  ledger_records: Array<Record<string, unknown>>;
  reasoning_trace_preview: Array<Record<string, unknown>>;
  current_rung: Record<string, unknown>;
  latest_preview: Record<string, unknown>;
}

// Reasoning trace types — live agent thinking surfaced to the observer

export interface ReasoningDigest {
  id: number;
  timestamp: number;
  agent: string;
  phase: string;
  importance: "low" | "medium" | "high";
  summary: string;
  details: {
    tokens?: { in: number; out: number };
    rating?: string;
    feedback?: string;
    focus_areas?: string[];
    plan?: {
      batches?: number;
      strategy?: string;
      estimated_gpu_minutes?: number;
    };
    tools_used?: string[];
    errors?: string[];
  };
  raw_trace_ids: number[];
}

export interface ReasoningTrace {
  id: number;
  timestamp: number;
  event_type: "agent_started" | "agent_completed" | "llm_request" | "llm_response" | "llm_error" | "tool_started" | "tool_completed" | "tool_error" | "agent_event" | "invocation_started" | "invocation_completed";
  agent_name: string;
  model: string;
  content: string;
  tokens_in: number | null;
  tokens_out: number | null;
  metadata: Record<string, unknown>;
}
