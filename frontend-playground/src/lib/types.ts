/**
 * TypeScript contracts mirroring the server/playground.py HTTP
 * surface. Kept intentionally hand-written (rather than generated
 * from OpenAPI) so PR review is grounded in the exact keys the UI
 * reads — a drift here shows up as a TS compile error the first
 * time the server envelope changes, which is the whole point of the
 * playground being a separate app.
 */

export type ComponentId = string; // "c01".."c15"

export type ComponentKind = "leaf" | "loop" | "gate" | "graph";

export interface DeclaredModel {
  readonly provider: string;
  readonly model: string;
  readonly role: string;
}

export interface EvaluatorDeclaration {
  readonly name: string;
  readonly threshold: number;
  readonly hard_gate: boolean;
}

export interface CaseSummary {
  readonly name: string | null;
  readonly role: "pass" | "neg" | "edge" | string;
  readonly session_id?: string;
  readonly input?: unknown;
  readonly expected_output?: unknown;
  readonly metadata?: Record<string, unknown>;
}

export interface ComponentSummary {
  readonly id: ComponentId;
  readonly title: string;
  readonly kind: ComponentKind;
  readonly row: 1 | 2 | 3;
  readonly summary: string;
  readonly declared_models: readonly DeclaredModel[];
  readonly has_task_adapter: boolean;
  readonly has_evaluator_stack: boolean;
}

export interface ComponentDetail extends ComponentSummary {
  readonly cases: readonly CaseSummary[];
  readonly evaluators: readonly EvaluatorDeclaration[];
}

export interface ReachabilityEntry {
  readonly provider: string;
  readonly model: string;
  readonly role: string;
  readonly reachable: boolean;
  readonly reason: string | null;
}

export interface HealthResponse {
  readonly component_id: ComponentId;
  readonly models: readonly ReachabilityEntry[];
  readonly total: number;
  readonly all_reachable: boolean;
  readonly unreachable_sentinel: string;
}

/** Run envelope — mirrors the four statuses in playground.py. */
export type RunStatus =
  | "OK"
  | "MODEL_UNREACHABLE"
  | "NO_TASK_ADAPTER"
  | "TASK_ERROR";

export interface RunResponse {
  readonly status: RunStatus;
  readonly component_id: ComponentId;
  readonly case_name?: string | null;
  readonly output?: unknown;
  readonly trajectory?: unknown;
  readonly error?: string;
  readonly unreachable_models?: readonly ReachabilityEntry[];
}

export type EvaluateStatus = "OK" | "NO_EVALUATORS" | "EVALUATOR_ERROR";

export interface EvaluatorResultRow {
  readonly evaluator_name: string;
  readonly threshold: number;
  readonly hard_gate: boolean;
  readonly passed: boolean;
  readonly mean_score: number;
  readonly error?: string;
}

export interface EvaluateResponse {
  readonly status: EvaluateStatus;
  readonly component_id: ComponentId;
  readonly case_name?: string | null;
  readonly results: readonly EvaluatorResultRow[];
  readonly overall_passed: boolean;
}
