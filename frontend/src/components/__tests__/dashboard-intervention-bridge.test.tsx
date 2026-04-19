/** @jest-environment jsdom */

/**
 * ARCH-H4 bridge tests for the halt surface.
 *
 * After DESIGN-06 (#258) moved the directive input into `<IntentBar />`,
 * `<DashboardIntervention />` is responsible only for the halt surface.
 * These tests lock down that contract:
 *
 *   1. The component no longer renders a directive input / scope chip /
 *      submit button — those responsibilities live in `<IntentBar />`.
 *   2. The pause button still submits a halt request to
 *      `/api/halt` when the pipeline is running.
 *   3. `deriveSlotContext` remains exported for legacy callers.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  DashboardIntervention,
  deriveSlotContext,
} from "@/components/dashboard-intervention";

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
    async (url: string, init?: RequestInit) => handler(url, init),
  ) as unknown as jest.Mock;
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe("DashboardIntervention halt surface", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("does not render the directive input, scope chip, or submit button", async () => {
    mockFetch((url: string) => {
      if (url.endsWith("/api/halt_state")) {
        return jsonResponse({
          halt_requested: false,
          halted_at_stage: null,
          halt_reviewer: null,
          halt_reason: null,
          halt_timestamp: null,
        });
      }
      if (url.endsWith("/dashboard/latest")) return jsonResponse({ status: "idle" });
      return jsonResponse({}, 404);
    });
    render(<DashboardIntervention />);
    // DESIGN-06 moved these affordances to <IntentBar />.
    expect(screen.queryByTestId("directive-input")).toBeNull();
    expect(screen.queryByTestId("directive-submit")).toBeNull();
    expect(screen.queryByTestId("directive-scope-chip")).toBeNull();
  });

  test("pause button posts to /api/halt only after cost-preview confirmation", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    mockFetch((url: string, init) => {
      calls.push([url, init]);
      if (url.endsWith("/api/halt_state")) {
        return jsonResponse({
          halt_requested: false,
          halted_at_stage: null,
          halt_reviewer: null,
          halt_reason: null,
          halt_timestamp: null,
        });
      }
      if (url.endsWith("/dashboard/latest")) {
        return jsonResponse({
          run_id: "run-1",
          status: "running",
          active_phase: "scenario",
        });
      }
      if (url.endsWith("/api/halt")) {
        return jsonResponse({
          halt_requested: true,
          halted_at_stage: null,
          halt_reviewer: "dashboard-user",
          halt_reason: null,
          halt_timestamp: null,
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<DashboardIntervention />);
    // DESIGN-09 (#261): the pause button now opens a cost-preview
    // dialog instead of firing immediately. The actual /api/halt POST
    // only happens after the reviewer confirms in the dialog.
    const btn = await screen.findByTestId("halt-pause-button");
    await user.click(btn);
    // Dialog is now open; no halt POST yet.
    expect(calls.some(([u]) => u.endsWith("/api/halt"))).toBe(false);
    const confirm = await screen.findByTestId("halt-pause-dialog-confirm");
    await user.click(confirm);
    await waitFor(() => {
      expect(calls.some(([u]) => u.endsWith("/api/halt"))).toBe(true);
    });
  });

  test("deriveSlotContext returns element-scoped context with scene_num", () => {
    const ctx = deriveSlotContext("scene3_narr", { scene_num: 3 });
    expect(ctx).toMatchObject({
      scope: "element",
      scope_ref: "scene3_narr",
      clip_id: "scene3_narr",
      scene_num: 3,
    });
  });
});
