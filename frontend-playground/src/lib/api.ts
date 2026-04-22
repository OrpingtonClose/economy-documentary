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
  ComponentDetail,
  ComponentSummary,
  EvaluateResponse,
  HealthResponse,
  RunResponse,
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
