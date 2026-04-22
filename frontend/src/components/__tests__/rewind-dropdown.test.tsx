/** @jest-environment jsdom */

/**
 * DESIGN-08 (#260): Rewind dropdown + Abandon alert-dialog tests.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RewindDropdown, REWIND_STAGES } from "@/components/rewind-dropdown";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
    async (url: string, init?: RequestInit) => handler(url, init),
  ) as unknown as jest.Mock;
}

describe("RewindDropdown", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("exposes the five plain-English rewind items plus Abandon run", async () => {
    const user = userEvent.setup();
    render(<RewindDropdown />);
    await user.click(screen.getByTestId("rewind-dropdown-trigger"));
    await waitFor(() => {
      expect(screen.getByTestId("rewind-item-scenario")).toBeInTheDocument();
    });
    expect(screen.getByTestId("rewind-item-scenario")).toHaveTextContent(
      /Rewind to scenario/,
    );
    expect(screen.getByTestId("rewind-item-audio")).toHaveTextContent(
      /Rewind to narration/,
    );
    expect(screen.getByTestId("rewind-item-visual_director")).toHaveTextContent(
      /Rewind to visuals/,
    );
    expect(screen.getByTestId("rewind-item-video")).toHaveTextContent(
      /Rewind to production/,
    );
    expect(screen.getByTestId("rewind-item-assembly")).toHaveTextContent(
      /Rewind to final touches/,
    );
    const abandon = screen.getByTestId("rewind-item-abandon");
    expect(abandon).toHaveTextContent(/Abandon run/);
    // Abandon is the ONLY red affordance here.
    expect(abandon.className).toMatch(/destructive/);
  });

  test("the five plain-English labels map to the canonical backend stage names", () => {
    const ids = REWIND_STAGES.map((s) => s.stage);
    expect(ids).toEqual([
      "scenario",
      "audio",
      "visual_director",
      "video",
      "assembly",
    ]);
  });

  test("selecting a stage opens the cost preview and POSTs on confirm", async () => {
    mockFetch((url) => {
      if (url.endsWith("/agui/estimate_directive")) {
        return jsonResponse({
          stages: 1,
          stage_label: "scene",
          eta_minutes: 5,
          dollars: 0.4,
          summary:
            "This will rerun 1 scene, add about 5 minutes, and cost about $0.40.",
        });
      }
      if (url.endsWith("/agui/rewind_to_stage")) {
        return jsonResponse({ status: "accepted", stage: "audio" });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    const onRewindAccepted = jest.fn();
    render(<RewindDropdown onRewindAccepted={onRewindAccepted} />);

    await user.click(screen.getByTestId("rewind-dropdown-trigger"));
    await user.click(screen.getByTestId("rewind-item-audio"));

    await waitFor(() => {
      expect(screen.getByTestId("rewind-cost-dialog")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(
        screen.getByTestId("rewind-cost-dialog-summary"),
      ).toHaveTextContent(/rerun 1 scene/i);
    });

    await user.click(screen.getByTestId("rewind-cost-dialog-confirm"));

    await waitFor(() => {
      const calls = (global.fetch as unknown as jest.Mock).mock.calls as Array<
        [string, RequestInit | undefined]
      >;
      const rewindCall = calls.find(([u]) =>
        u.endsWith("/agui/rewind_to_stage"),
      );
      expect(rewindCall).toBeDefined();
      const body = JSON.parse(String(rewindCall![1]!.body));
      expect(body.stage).toBe("audio");
    });
    expect(onRewindAccepted).toHaveBeenCalledWith("audio", "Rewind to narration");
  });

  test("cancelling the cost preview does NOT POST the rewind", async () => {
    mockFetch((url) => {
      if (url.endsWith("/agui/estimate_directive")) {
        return jsonResponse({
          stages: 1,
          stage_label: "scene",
          eta_minutes: 5,
          dollars: 0.4,
          summary: "test summary",
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<RewindDropdown />);
    await user.click(screen.getByTestId("rewind-dropdown-trigger"));
    await user.click(screen.getByTestId("rewind-item-scenario"));
    await waitFor(() => {
      expect(screen.getByTestId("rewind-cost-dialog")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("rewind-cost-dialog-cancel"));

    const calls = (global.fetch as unknown as jest.Mock).mock.calls as Array<
      [string, RequestInit | undefined]
    >;
    const rewindCall = calls.find(([u]) =>
      u.endsWith("/agui/rewind_to_stage"),
    );
    expect(rewindCall).toBeUndefined();
  });

  test("abandon dialog requires typing 'abandon' before the confirm enables", async () => {
    mockFetch((url) => {
      if (url.endsWith("/api/halt")) {
        return jsonResponse({ halt_requested: true });
      }
      if (url.endsWith("/api/halt/release")) {
        return jsonResponse({ status: "released" });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    const onAbandonAccepted = jest.fn();
    render(<RewindDropdown onAbandonAccepted={onAbandonAccepted} />);

    await user.click(screen.getByTestId("rewind-dropdown-trigger"));
    await user.click(screen.getByTestId("rewind-item-abandon"));

    await waitFor(() => {
      expect(screen.getByTestId("abandon-dialog")).toBeInTheDocument();
    });
    const confirm = screen.getByTestId("abandon-confirm");
    expect(confirm).toBeDisabled();

    const input = screen.getByTestId("abandon-confirm-input");
    await user.type(input, "wrong");
    expect(screen.getByTestId("abandon-confirm")).toBeDisabled();

    await user.clear(input);
    await user.type(input, "abandon");
    await waitFor(() => {
      expect(screen.getByTestId("abandon-confirm")).not.toBeDisabled();
    });

    await user.click(screen.getByTestId("abandon-confirm"));
    await waitFor(() => {
      expect(onAbandonAccepted).toHaveBeenCalled();
    });

    // Abandon posts halt + halt/release exit -- verify both fired.
    const calls = (global.fetch as unknown as jest.Mock).mock.calls as Array<
      [string, RequestInit | undefined]
    >;
    expect(calls.find(([u]) => u.endsWith("/api/halt"))).toBeDefined();
    const releaseCall = calls.find(([u]) => u.endsWith("/api/halt/release"));
    expect(releaseCall).toBeDefined();
    const releaseBody = JSON.parse(String(releaseCall![1]!.body));
    expect(releaseBody.mode).toBe("exit");
  });
});
