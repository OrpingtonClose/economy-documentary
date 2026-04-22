/**
 * Tests for the server-side fetchers (``src/lib/server-fetch.ts``).
 *
 * Server Components can't rely on the Next.js ``/playground/*``
 * rewrite, so this helper hits the FastAPI origin directly. We
 * assert the origin is resolved from ``PLAYGROUND_API_URL`` (with a
 * loopback default) and that transport failures surface as thrown
 * Errors rather than silent undefined returns.
 */

import { fetchComponent, fetchComponents } from "@/lib/server-fetch";

type FetchMock = jest.Mock<
  Promise<Response>,
  [RequestInfo | URL, RequestInit?]
>;

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

const originalEnv = process.env.PLAYGROUND_API_URL;

afterEach(() => {
  jest.resetAllMocks();
  if (originalEnv === undefined) {
    delete process.env.PLAYGROUND_API_URL;
  } else {
    process.env.PLAYGROUND_API_URL = originalEnv;
  }
});

describe("fetchComponents", () => {
  it("defaults to http://127.0.0.1:8000 when PLAYGROUND_API_URL is unset", async () => {
    delete process.env.PLAYGROUND_API_URL;
    const mock = installFetch(jsonResponse({ components: [], total: 0 }));

    await fetchComponents();

    const [url] = mock.mock.calls[0];
    expect(String(url)).toBe("http://127.0.0.1:8000/playground/components");
  });

  it("uses PLAYGROUND_API_URL when set", async () => {
    process.env.PLAYGROUND_API_URL = "https://pg-preview.example";
    const mock = installFetch(jsonResponse({ components: [], total: 0 }));

    await fetchComponents();

    const [url] = mock.mock.calls[0];
    expect(String(url)).toBe(
      "https://pg-preview.example/playground/components",
    );
  });

  it("unwraps the {components} envelope", async () => {
    installFetch(
      jsonResponse({
        components: [{ id: "c01", title: "Scenario" }],
        total: 1,
      }),
    );

    const result = await fetchComponents();

    expect(result).toEqual([{ id: "c01", title: "Scenario" }]);
  });
});

describe("fetchComponent", () => {
  it("GETs /playground/components/{id}", async () => {
    delete process.env.PLAYGROUND_API_URL;
    const mock = installFetch(jsonResponse({ id: "c04", title: "Audio" }));

    await fetchComponent("c04");

    const [url] = mock.mock.calls[0];
    expect(String(url)).toBe("http://127.0.0.1:8000/playground/components/c04");
  });

  it("throws on non-2xx responses", async () => {
    installFetch(
      new Response("not found", { status: 404, statusText: "Not Found" }),
    );

    await expect(fetchComponent("c99")).rejects.toThrow(/404/);
  });
});
