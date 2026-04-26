/**
 * Thin fetch helpers for the playground HTTP surface.
 *
 * Why hand-written:
 * - The API is small (five endpoints), so a generated client would
 *   be heavier than the code it replaces.
 * - Every call goes through the Next.js rewrite in next.config.js so
 *   the browser only ever talks to the same-origin dev server. The
 *   rewrite target is controlled by ``PLAYGROUND_API_URL``; tests
 *   stub ``global.fetch`` directly.
 */

import type {
  CaseSummary,
  ComponentDetail,
  ComponentSummary,
  EvaluateResponse,
  HealthResponse,
  LangfuseConfig,
  RunResponse,
  RunState,
  SaveUserCaseResponse,
  StartPipelineRunBody,
  StartPipelineRunResponse,
  StartRunResponse,
} from "./types";

/** Playground catalog root — every request starts here. */
export const PLAYGROUND_BASE = "/playground";

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${PLAYGROUND_BASE}${path}`, {
    ...init,
    headers: {
      accept: "application/json",
      ...(init?.headers || {}),
    },
    // Playground data is always session-scoped — never cached.
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await safeText(response);
    throw new PlaygroundApiError(
      `GET ${path} -> ${response.status} ${response.statusText}: ${body}`,
      response.status
    );
  }
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${PLAYGROUND_BASE}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body ?? {}),
    cache: "no-store",
  });
  if (!response.ok) {
    const text = await safeText(response);
    throw new PlaygroundApiError(
      `POST ${path} -> ${response.status} ${response.statusText}: ${text}`,
      response.status
    );
  }
  return (await response.json()) as T;
}

async function safeText(response: Response): Promise<string> {
  try {
    return await response.text();
  } catch {
    return "";
  }
}

export class PlaygroundApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "PlaygroundApiError";
    this.status = status;
  }
}

export async function listComponents(): Promise<readonly ComponentSummary[]> {
  const payload = await getJson<{ components: ComponentSummary[] }>(
    "/components"
  );
  return payload.components;
}

export async function getComponent(
  componentId: string
): Promise<ComponentDetail> {
  return getJson<ComponentDetail>(`/components/${componentId}`);
}

export async function getComponentHealth(
  componentId: string
): Promise<HealthResponse> {
  // Matches the FastAPI route registered in server/playground.py:
  // @router.get("/components/{component_id}/models/health"). The
  // shorter "/components/{id}/health" path is not registered and
  // would 404.
  return getJson<HealthResponse>(
    `/components/${componentId}/models/health`,
  );
}

export async function runCase(
  componentId: string,
  body: { case_name?: string; custom_input?: unknown }
): Promise<RunResponse> {
  return postJson<RunResponse>(`/components/${componentId}/run`, body);
}

export async function evaluateCase(
  componentId: string,
  body: {
    case_name?: string;
    custom_input?: unknown;
    custom_expected?: unknown;
    actual_output: unknown;
    actual_trajectory?: unknown;
  }
): Promise<EvaluateResponse> {
  return postJson<EvaluateResponse>(`/components/${componentId}/evaluate`, body);
}

/**
 * Body for the save-as-case endpoint. ``confirm=false`` (the default)
 * returns a preview only; ``confirm=true`` writes to the on-disk
 * sidecar under ``server/strands_agents/playground/user_cases/``.
 */
export interface SaveUserCaseBody {
  readonly name: string;
  readonly role?: "pass" | "neg" | "edge";
  readonly input: unknown;
  readonly metadata?: Record<string, unknown>;
  readonly notes?: string;
  readonly created_by?: string;
  readonly confirm?: boolean;
}

export async function listUserCases(
  componentId: string
): Promise<readonly CaseSummary[]> {
  const payload = await getJson<{ user_cases: CaseSummary[] }>(
    `/components/${componentId}/user-cases`
  );
  return payload.user_cases;
}

export async function saveUserCase(
  componentId: string,
  body: SaveUserCaseBody
): Promise<SaveUserCaseResponse> {
  return postJson<SaveUserCaseResponse>(
    `/components/${componentId}/user-cases`,
    body
  );
}

/**
 * Allocate a run_id and kick off a dispatch on the server. The
 * response is returned immediately; progress is observed via the
 * SSE stream at ``events_url`` and terminal state is polled from
 * ``state_url`` as a fallback.
 */
export async function startRun(
  componentId: string,
  body: { case_name?: string; custom_input?: unknown }
): Promise<StartRunResponse> {
  return postJson<StartRunResponse>(
    `/components/${componentId}/runs`,
    body
  );
}

/**
 * Allocate a pipeline run on the server. The response carries the
 * run_id used to subscribe to the same SSE surface as a single-
 * component run, plus the topic / duration / language echoed back
 * (so the page can render the run header without re-reading state).
 */
export async function startPipelineRun(
  body: StartPipelineRunBody,
): Promise<StartPipelineRunResponse> {
  return postJson<StartPipelineRunResponse>("/pipeline/runs", body);
}

/** Polling fallback for a run that the SSE stream cannot reach. */
export async function getRunState(runId: string): Promise<RunState> {
  return getJson<RunState>(`/runs/${runId}`);
}

/** Absolute URL of the SSE event stream for one run. */
export function runEventsUrl(runId: string): string {
  return `${PLAYGROUND_BASE}/runs/${runId}/events`;
}

/**
 * Operator decision payload accepted by ``POST /playground/approval/
 * resume/{run_id}/{interrupt_id}``.
 *
 * Mirrors :class:`server.strands_agents.approval.ApprovalDecision`.
 * The ``type`` field is the operator's verdict; ``edits`` carries
 * mutated tool args for ``"edit"`` (orchestrator re-plans against
 * them); ``feedback`` is a human-readable explanation surfaced on
 * the audit record for ``"reject"`` and ``"respond"``.
 */
export interface ApprovalDecisionBody {
  readonly type: "approve" | "edit" | "reject" | "respond";
  readonly edits?: Record<string, unknown>;
  readonly feedback?: string;
}

/** Response shape of ``/playground/approval/resume/...``. */
export interface ResolveApprovalResponse {
  readonly status: string;
  readonly decision_type: string;
}

/**
 * Submit an operator decision for a pending pipeline gate.
 *
 * The frontend extracts ``run_id`` + ``interrupt_id`` from the
 * ``pipeline.approval.waiting`` SSE event surfaced on the run
 * stream. The pipeline orchestrator awaits the same future this
 * call resolves; once it returns the run resumes with the
 * decision applied.
 *
 * @throws PlaygroundApiError when the gate is not pending (404),
 *   the decision is malformed (400), or any non-2xx response.
 */
export async function resolvePipelineApproval(
  runId: string,
  interruptId: string,
  decision: ApprovalDecisionBody,
): Promise<ResolveApprovalResponse> {
  return postJson<ResolveApprovalResponse>(
    `/approval/resume/${encodeURIComponent(runId)}/${encodeURIComponent(
      interruptId,
    )}`,
    decision,
  );
}

/**
 * Fetch the Langfuse observability config.
 *
 * Called once per workbench mount so the "View Trace" button can
 * conditionally render. Never throws on a missing backend — returns
 * the disabled default so the UI degrades cleanly.
 */
export async function getLangfuseConfig(): Promise<LangfuseConfig> {
  try {
    return await getJson<LangfuseConfig>("/config/langfuse");
  } catch {
    return { enabled: false, host: null };
  }
}
