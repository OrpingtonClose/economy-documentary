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
