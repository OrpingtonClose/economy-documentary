/**
 * Fetch-helper smoke tests for the playground API module.
 *
 * Goal is narrow and deliberate — we stub ``fetch`` and assert:
 *  1. each helper hits the right URL under ``/playground`` with the
 *     right HTTP method, so any drift in the backend router path
 *     surfaces here immediately, and
 *  2. error responses surface as ``PlaygroundApiError`` instead of
 *     silently returning garbage.
 *
 * PR 7 will add component-level tests that compose these helpers;
 * keeping the unit layer small lets the PR diff stay focused on
 * wiring the UI, not re-asserting the helpers.
 */

import {
  PLAYGROUND_BASE,
  PlaygroundApiError,
  evaluateCase,
  getComponent,
  getComponentHealth,
  listComponents,
  runCase,
} from "@/lib/api";

type FetchMock = jest.Mock<Promise<Response>, [RequestInfo | URL, RequestInit?]>;

function installFetch(response: Response): FetchMock {
  const mock = jest.fn().mockResolvedValue(response) as FetchMock;
  (global as unknown as { fetch: FetchMock }).fetch = mock;
  return mock;
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

afterEach(() => {
  jest.resetAllMocks();
});

describe("listComponents", () => {
  it("GETs /playground/components and unwraps the envelope", async () => {
    const mock = installFetch(
      jsonResponse({ components: [{ id: "c01", title: "Scenario" }] }),
    );

    const result = await listComponents();

    expect(mock).toHaveBeenCalledTimes(1);
    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components`);
    expect(init?.method ?? "GET").toBe("GET");
    expect(result).toEqual([{ id: "c01", title: "Scenario" }]);
  });
});

describe("getComponent", () => {
  it("GETs /playground/components/{id}", async () => {
    const mock = installFetch(jsonResponse({ id: "c04" }));

    await getComponent("c04");

    const [url] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c04`);
  });
});

describe("getComponentHealth", () => {
  it("GETs /playground/components/{id}/health", async () => {
    const mock = installFetch(
      jsonResponse({
        component_id: "c01",
        models: [],
        total: 0,
        all_reachable: true,
        unreachable_sentinel: "MODEL_UNREACHABLE",
      }),
    );

    await getComponentHealth("c01");

    const [url] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c01/health`);
  });
});

describe("runCase", () => {
  it("POSTs JSON body to /playground/components/{id}/run", async () => {
    const mock = installFetch(
      jsonResponse({ status: "OK", component_id: "c01" }),
    );

    await runCase("c01", { case_name: "economics_basics" });

    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c01/run`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body ?? ""))).toEqual({
      case_name: "economics_basics",
    });
    expect(
      (init?.headers as Record<string, string> | undefined)?.["content-type"],
    ).toBe("application/json");
  });
});

describe("evaluateCase", () => {
  it("POSTs JSON body to /playground/components/{id}/evaluate", async () => {
    const mock = installFetch(
      jsonResponse({
        status: "OK",
        component_id: "c01",
        results: [],
        overall_passed: true,
      }),
    );

    await evaluateCase("c01", {
      case_name: "economics_basics",
      actual_output: { whatever: 1 },
    });

    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c01/evaluate`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body ?? "")).actual_output).toEqual({
      whatever: 1,
    });
  });
});

describe("error surface", () => {
  it("raises PlaygroundApiError with status on 4xx/5xx", async () => {
    installFetch(
      new Response("upstream unreachable", {
        status: 503,
        statusText: "Service Unavailable",
      }),
    );

    await expect(listComponents()).rejects.toBeInstanceOf(PlaygroundApiError);
  });

  it("preserves status on the error instance", async () => {
    installFetch(
      new Response("not found", { status: 404, statusText: "Not Found" }),
    );

    await expect(getComponent("c99")).rejects.toMatchObject({
      status: 404,
    });
  });
});
