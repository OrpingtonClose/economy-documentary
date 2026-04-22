/**
 * TypeScript contracts mirroring the server/playground.py HTTP
 * surface. Kept intentionally hand-written (rather than generated
 * from OpenAPI) so PR review is grounded in the exact keys the UI
 * reads — a drift here shows up as a TS compile error the first
 * time the server envelope changes, which is the whole point of the
 * playground being a separate app.
 *
 * Every field in this file is pinned to a line in
 * ``server/playground.py``. When changing a field here, update the
 * citation too.
 */

export type ComponentId = string; // "c01".."c15"

export type ComponentKind = "leaf" | "loop" | "gate" | "graph";

/**
 * Mirror of ``_serialise_model`` in ``server/playground.py``:
 *
 *     {"id": model.id, "provider": model.provider, "role": model.role}
 */
export interface DeclaredModel {
  readonly id: string;
  readonly provider: string;
  readonly role: string;
}

/**
 * Mirror of ``_serialise_evaluator`` in ``server/playground.py``.
 */
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

/**
 * Mirror of ``_component_summary`` in ``server/playground.py`` — the
 * list-endpoint serialisation. Note the server emits ``evaluators``
 * and ``case_count``; it does NOT emit ``has_task_adapter`` /
 * ``has_evaluator_stack`` booleans. The UI can derive both from
 * ``evaluators.length`` / ``case_count`` if needed.
 */
export interface ComponentSummary {
  readonly id: ComponentId;
  readonly title: string;
  readonly kind: ComponentKind;
  readonly row: 1 | 2 | 3;
  readonly summary: string;
  readonly declared_models: readonly DeclaredModel[];
  readonly evaluators: readonly EvaluatorDeclaration[];
  readonly case_count: number;
}

/**
 * Mirror of ``get_component_detail`` in ``server/playground.py``.
 * ``detail = _component_summary(...); detail["cases"] = [...]``, so
 * the detail shape is the summary plus ``cases``. ``evaluators`` is
 * already on the summary — not re-declared here.
 */
export interface ComponentDetail extends ComponentSummary {
  readonly cases: readonly CaseSummary[];
}

/**
 * Mirror of ``_serialise_reachability`` in ``server/playground.py``:
 *
 *     {"model_id": status.model_id, "provider": status.provider,
 *      "reachable": status.reachable, "reason": status.reason,
 *      "checked_at": status.checked_at, "latency_ms": status.latency_ms}
 *
 * The server does not attach ``role`` to reachability rows — callers
 * that need role must join against the component's ``declared_models``
 * using ``model_id``.
 */
export interface ReachabilityEntry {
  readonly model_id: string;
  readonly provider: string;
  readonly reachable: boolean;
  readonly reason: string | null;
  readonly checked_at: string | null;
  readonly latency_ms: number | null;
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

/**
 * Mirror of the evaluator-result row written by ``evaluate_component``
 * in ``server/playground.py``. The OK branch writes ``mean_score`` as
 * a ``float``; the exception branch writes ``mean_score: None`` —
 * hence ``number | null``. ``status`` is one of the
 * ``EVAL_STATUS_*`` sentinels. ``outputs`` carries the per-case
 * ``EvaluationOutput`` projections; absent on the error branch.
 */
export interface EvaluatorResultRow {
  readonly name: string;
  readonly threshold: number;
  readonly hard_gate: boolean;
  readonly passed: boolean;
  readonly status: EvaluateStatus;
  readonly mean_score: number | null;
  readonly error?: string;
  readonly outputs?: ReadonlyArray<{
    readonly score: number | null;
    readonly test_pass: boolean | null;
    readonly reason: string | null;
    readonly label: string | null;
  }>;
}

export interface EvaluateResponse {
  readonly status: EvaluateStatus;
  readonly component_id: ComponentId;
  readonly case_name?: string | null;
  readonly results: readonly EvaluatorResultRow[];
  readonly overall_passed: boolean;
}
