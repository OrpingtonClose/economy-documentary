/**
 * Server-side fetchers for the playground HTTP surface.
 *
 * Why a separate module from ``api.ts``: Server Components in the
 * Next.js app router run on the server, where the ``/playground/*``
 * rewrite in ``next.config.js`` does NOT apply — the rewrite only
 * proxies browser requests. Server Components must therefore call
 * the FastAPI origin directly.
 *
 * The origin is resolved from ``PLAYGROUND_API_URL`` with the same
 * default as ``next.config.js`` (``http://127.0.0.1:8000``), so
 * server-side and client-side requests end up at the same backend.
 */

import type {
  ComponentDetail,
  ComponentSummary,
} from "./types";

function origin(): string {
  const configured = process.env.PLAYGROUND_API_URL;
  return configured && configured.length > 0
    ? configured
    : "http://127.0.0.1:8000";
}

async function getJsonFromOrigin<T>(path: string): Promise<T> {
  const response = await fetch(`${origin()}/playground${path}`, {
    headers: { accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(
      `GET ${path} -> ${response.status} ${response.statusText}: ${body}`,
    );
  }
  return (await response.json()) as T;
}

export async function fetchComponents(): Promise<
  readonly ComponentSummary[]
> {
  const payload = await getJsonFromOrigin<{ components: ComponentSummary[] }>(
    "/components",
  );
  return payload.components;
}

export async function fetchComponent(
  componentId: string,
): Promise<ComponentDetail> {
  return getJsonFromOrigin<ComponentDetail>(`/components/${componentId}`);
}
