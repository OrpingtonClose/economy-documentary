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
  listUserCases,
  resolvePipelineApproval,
  runCase,
  saveUserCase,
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
  it("GETs /playground/components/{id}/models/health", async () => {
    // Pinned to the FastAPI route in server/playground.py; the
    // short "/{id}/health" form is not registered and would 404.
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
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c01/models/health`);
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

describe("listUserCases", () => {
  it("GETs /playground/components/{id}/user-cases and unwraps", async () => {
    const mock = installFetch(
      jsonResponse({
        component_id: "c02",
        user_cases: [
          { name: "saved_a", role: "pass", input: {}, source: "user" },
        ],
        total: 1,
      }),
    );

    const result = await listUserCases("c02");

    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c02/user-cases`);
    expect(init?.method ?? "GET").toBe("GET");
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("saved_a");
  });
});

describe("saveUserCase", () => {
  it("POSTs preview body with confirm=false by default", async () => {
    const mock = installFetch(
      jsonResponse({
        component_id: "c02",
        committed: false,
        preview: {
          file_path: "/tmp/x.json",
          existed: false,
          diff: "",
          before: "",
          after: "[]",
          case_count_before: 0,
          case_count_after: 1,
        },
      }),
    );

    const result = await saveUserCase("c02", {
      name: "my_case",
      role: "edge",
      input: { a: 1 },
    });

    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(`${PLAYGROUND_BASE}/components/c02/user-cases`);
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body ?? ""));
    expect(body.name).toBe("my_case");
    expect(body.role).toBe("edge");
    expect(result.committed).toBe(false);
  });

  it("commits when confirm=true", async () => {
    const mock = installFetch(
      jsonResponse({
        component_id: "c02",
        committed: true,
        preview: {
          file_path: "/tmp/x.json",
          existed: false,
          diff: "",
          before: "",
          after: "[]",
          case_count_before: 0,
          case_count_after: 1,
        },
        case: { name: "my_case", role: "pass", input: {}, source: "user" },
      }),
    );

    const result = await saveUserCase("c02", {
      name: "my_case",
      input: {},
      confirm: true,
    });

    expect(result.committed).toBe(true);
    expect(result.case?.name).toBe("my_case");
    const [, init] = mock.mock.calls[0];
    const body = JSON.parse(String(init?.body ?? ""));
    expect(body.confirm).toBe(true);
  });

  it("surfaces 409 collisions as PlaygroundApiError", async () => {
    installFetch(
      new Response("name collides with canonical case", {
        status: 409,
        statusText: "Conflict",
      }),
    );

    await expect(
      saveUserCase("c02", { name: "intent_exact", input: {}, confirm: true }),
    ).rejects.toMatchObject({ status: 409 });
  });
});

describe("resolvePipelineApproval", () => {
  it("POSTs to /playground/approval/resume/{run}/{interrupt} with decision body", async () => {
    const mock = installFetch(
      jsonResponse({ status: "ok", decision_type: "accept" }),
    );

    const result = await resolvePipelineApproval("run-abc", "int-789", {
      type: "accept",
    });

    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(
      `${PLAYGROUND_BASE}/approval/resume/run-abc/int-789`,
    );
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body ?? ""))).toEqual({ type: "accept" });
    expect(result).toEqual({ status: "ok", decision_type: "accept" });
  });

  it("URL-encodes ids that contain reserved characters", async () => {
    const mock = installFetch(
      jsonResponse({ status: "ok", decision_type: "edit" }),
    );

    await resolvePipelineApproval("run/abc", "int 1", {
      type: "edit",
      args: { scene_id: "s1" },
    });

    const [url] = mock.mock.calls[0];
    expect(url).toBe(
      `${PLAYGROUND_BASE}/approval/resume/run%2Fabc/int%201`,
    );
  });

  it("propagates the full decision body including args and reason", async () => {
    const mock = installFetch(
      jsonResponse({ status: "ok", decision_type: "edit" }),
    );

    await resolvePipelineApproval("run-1", "int-1", {
      type: "edit",
      args: { scene_id: "s1", prompt: "wide shot" },
      reason: "tighten the framing",
    });

    const [, init] = mock.mock.calls[0];
    expect(JSON.parse(String(init?.body ?? ""))).toEqual({
      type: "edit",
      args: { scene_id: "s1", prompt: "wide shot" },
      reason: "tighten the framing",
    });
  });

  it("raises PlaygroundApiError on 404 (gate not pending)", async () => {
    installFetch(
      new Response("not pending", { status: 404, statusText: "Not Found" }),
    );

    await expect(
      resolvePipelineApproval("run-x", "int-y", { type: "accept" }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("raises PlaygroundApiError on 400 (invalid decision)", async () => {
    installFetch(
      new Response("edit decision requires args", {
        status: 400,
        statusText: "Bad Request",
      }),
    );

    await expect(
      resolvePipelineApproval("run-x", "int-y", { type: "edit" }),
    ).rejects.toBeInstanceOf(PlaygroundApiError);
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
