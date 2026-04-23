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

export type ComponentKind = "leaf" | "tool" | "loop" | "gate" | "graph";

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
  /**
   * ``"user"`` for cases persisted via
   * ``POST /components/{id}/user-cases``; absent on canonical
   * cases emitted by ``_serialise_case``. The workbench uses this
   * to render a "saved by you" tag and to key off the user corpus
   * when listing / filtering.
   */
  readonly source?: "user";
  readonly notes?: string | null;
  readonly created_at?: string | null;
  readonly created_by?: string | null;
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
  /**
   * User-authored cases persisted under
   * ``server/strands_agents/playground/user_cases/``. Separate from
   * ``cases`` (the canonical CI corpus) so the atlas chip counts
   * stay stable — user additions are additive, never replace.
   */
  readonly user_cases?: readonly CaseSummary[];
}

/**
 * Mirror of the ``preview`` envelope returned by
 * ``POST /components/{id}/user-cases`` in ``server/playground.py``.
 * Both the preview-only path (``confirm=False``) and the commit
 * path (``confirm=True``) carry this bundle so the UI can render
 * the same diff view in both flows.
 */
export interface SaveUserCasePreview {
  readonly file_path: string;
  readonly existed: boolean;
  readonly diff: string;
  readonly before: string;
  readonly after: string;
  readonly case_count_before: number;
  readonly case_count_after: number;
}

export interface SaveUserCaseResponse {
  readonly component_id: ComponentId;
  readonly committed: boolean;
  readonly preview: SaveUserCasePreview;
  readonly case?: CaseSummary;
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
  readonly checked_at: number | null;
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

// --------------------------------------------------------------------------
// Event stream (for the live status line + interpretation card)
//
// Mirrors ``Event.to_dict`` in ``server/strands_agents/playground/events.py``
// and the start-run / run-state envelopes in ``server/playground.py``.
// --------------------------------------------------------------------------

/**
 * AG-UI event types emitted alongside the legacy ``kind``. Mirror of
 * ``AGUI_TYPES`` in ``server/strands_agents/playground/agui.py``.
 *
 * The server emits **both** ``kind`` (legacy, stable internal
 * vocabulary) and ``type`` (AG-UI wire protocol) on every envelope.
 * Existing consumers branch on ``kind``; AG-UI-aware tooling
 * (Langfuse dashboards, the AG-UI SDK, third-party frontends)
 * branches on ``type``. Both fields derive from the same source of
 * truth so they can never disagree.
 *
 * See https://docs.ag-ui.com for the protocol spec.
 */
export type AgUiEventType =
  | "RUN_STARTED"
  | "RUN_FINISHED"
  | "RUN_ERROR"
  | "STEP_STARTED"
  | "STEP_FINISHED"
  | "TOOL_CALL_START"
  | "TOOL_CALL_END"
  | "TEXT_MESSAGE_CONTENT"
  | "CUSTOM";

/**
 * One structured step in a run's timeline. The ``kind`` vocabulary is
 * shared with the narrator prompt — keep it in sync with the list
 * in ``events.py``'s docstring.
 *
 * ``type`` is the AG-UI wire-protocol discriminator; it is always
 * present in live server output (every envelope carries it via
 * ``Event.to_dict``) but is typed optional here so fixtures and
 * older snapshots that pre-date the AG-UI migration still compile.
 * ``step_name`` / ``source`` / ``name`` / ``cancelled`` are AG-UI
 * sub-discriminators — present only on the events where they apply.
 */
export interface RunEvent {
  readonly seq: number;
  readonly ts: number;
  readonly kind: string;
  readonly summary: string;
  readonly detail: Record<string, unknown>;
  readonly type?: AgUiEventType;
  /** AG-UI step name for ``STEP_STARTED`` / ``STEP_FINISHED``. */
  readonly step_name?: string;
  /** AG-UI attribution for ``TEXT_MESSAGE_CONTENT`` (``narrator`` / ``interpreter``). */
  readonly source?: string;
  /** AG-UI sub-kind for ``CUSTOM`` events (carries the legacy ``kind``). */
  readonly name?: string;
  /** ``true`` when a run ends via cancellation rather than success. */
  readonly cancelled?: boolean;
}

/** Mirror of ``_serialise_run_state`` in ``server/playground.py``. */
export interface RunState {
  readonly run_id: string;
  readonly component_id: ComponentId;
  readonly case_name: string | null;
  readonly created_at: number;
  readonly closed: boolean;
  readonly events: readonly RunEvent[];
  readonly terminal: RunTerminal | null;
  /**
   * 32-char hex OTel trace id for the dispatch root span, or ``null``
   * when OTel is not configured. Used together with the Langfuse
   * config to render a "View Trace" link next to the live rail —
   * ``null`` means the button is hidden, not that the run failed.
   */
  readonly trace_id?: string | null;
  /**
   * Full ``LANGFUSE_HOST/trace/<trace_id>`` URL, or ``null`` when
   * Langfuse is not wired. Precomputed server-side so the frontend
   * doesn't have to concatenate and re-validate the host.
   */
  readonly trace_url?: string | null;
}

/**
 * Mirror of ``/playground/config/langfuse``. The frontend polls this
 * once on mount so the "View Trace" button can decide whether to
 * render. ``host`` is returned for diagnostics (tooltip, dev tools)
 * but the authoritative link is always ``RunState.trace_url`` —
 * having the server hand back the full URL avoids double-encoding
 * bugs when ``host`` contains a path prefix.
 */
export interface LangfuseConfig {
  readonly enabled: boolean;
  readonly host: string | null;
}

/** Terminal payload shape written into ``RunStream.terminal``. */
export interface RunTerminal {
  readonly status: RunStatus | "CANCELLED";
  readonly component_id: ComponentId;
  readonly case_name?: string | null;
  readonly output?: unknown;
  readonly trajectory?: unknown;
  readonly error?: string;
  readonly error_class?: string;
  readonly unreachable_models?: readonly ReachabilityEntry[];
  readonly interpretation?: string;
}

/** Mirror of the ``start_run`` response envelope. */
export interface StartRunResponse {
  readonly run_id: string;
  readonly component_id: ComponentId;
  readonly case_name: string;
  readonly events_url: string;
  readonly state_url: string;
}
