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

/** Polling fallback for a run that the SSE stream cannot reach. */
export async function getRunState(runId: string): Promise<RunState> {
  return getJson<RunState>(`/runs/${runId}`);
}

/** Absolute URL of the SSE event stream for one run. */
export function runEventsUrl(runId: string): string {
  return `${PLAYGROUND_BASE}/runs/${runId}/events`;
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
