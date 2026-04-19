/** @jest-environment jsdom */

/**
 * DESIGN-06 (#258) — tests for the stage-aware intent bar.
 *
 * Covers:
 *
 *   1. Placeholder copy reflects the current selection ("Say something
 *      about the whole film" vs "Say something about Scene N").
 *   2. `chipsForStage` returns the expected chip set for each stage —
 *      no jargon, plain English, and different chips per stage.
 *   3. Clicking a chip POSTs the chip text as the directive; no slot
 *      context when nothing is selected.
 *   4. When a slot is selected, submitting the typed input posts
 *      ``scope: "scene"`` with the scene_num derived from the store.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IntentBar, chipsForStage } from "@/components/intent-bar";
import {
  _resetSelectionForTests,
  selectionStore,
} from "@/lib/stores/selection";
import type { OtioTimelineStatus } from "@/lib/types";

class StubEventSource {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 1;
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {}
}
(global as unknown as { EventSource: typeof StubEventSource }).EventSource =
  StubEventSource;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function timeline(): OtioTimelineStatus {
  return {
    state: "draft",
    total_duration_sec: 30,
    source_file: "demo.otio",
    reconciliation: [],
    finished_film: null,
    tracks: [
      {
        name: "A1_Narration",
        kind: "audio",
        total_slots: 1,
        slots: [
          {
            slot_id: "scene5_narr",
            track: "A1_Narration",
            scene_num: 5,
            phrase_idx: 0,
            start_sec: 20,
            duration_sec: 3,
            status: "delivered",
            label: "Scene 5 narration",
            preview_url: "",
            thumbnail_url: "",
            waveform_url: "",
            failure_reason: "",
            rung: "",
            scripted_duration_sec: 3,
            measured_duration_sec: 3,
            metadata: {},
          },
        ],
      },
      { name: "V1_Video", kind: "video", slots: [], total_slots: 0 },
      { name: "A2_Music", kind: "audio", slots: [], total_slots: 0 },
    ],
  };
}

function installFetch(
  directiveCalls: Array<[string, RequestInit | undefined]>,
) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
    async (url: string, init?: RequestInit) => {
      if (url.endsWith("/agui/otio/state")) return jsonResponse(timeline());
      if (url.endsWith("/dashboard/latest")) {
        return jsonResponse({ status: "running", active_phase: "scenario" });
      }
      if (url.endsWith("/api/directive")) {
        directiveCalls.push([url, init]);
        return jsonResponse({
          status: "accepted",
          l4_event_id: "evt-1",
          record_ids: [1],
          records: [],
          re_manifestation_plans: [],
        });
      }
      return jsonResponse({}, 404);
    },
  ) as unknown as jest.Mock;
}

describe("chipsForStage", () => {
  test("scenario stage surfaces tightening / tone / grounding chips", () => {
    const labels = chipsForStage("scenario").map((c) => c.label);
    expect(labels).toEqual(
      expect.arrayContaining([
        "Make it tighter",
        "Friendlier tone",
        "More grounded",
      ]),
    );
  });

  test("visual_direction stage swaps in aesthetic chips", () => {
    const labels = chipsForStage("visual_direction").map((c) => c.label);
    expect(labels).toEqual(
      expect.arrayContaining(["Darker aesthetic", "Slower shots"]),
    );
  });

  test("audio stage surfaces delivery-focused chips", () => {
    const labels = chipsForStage("audio").map((c) => c.label);
    expect(labels).toEqual(expect.arrayContaining(["Calmer delivery", "More energy"]));
  });

  test("every chip label is plain English — no jargon tokens", () => {
    const stages: Array<Parameters<typeof chipsForStage>[0]> = [
      "brief",
      "scenario",
      "audio",
      "visual_direction",
      "production",
      "assembly",
      "completed",
      "idle",
    ];
    for (const stage of stages) {
      for (const chip of chipsForStage(stage)) {
        expect(chip.label).not.toMatch(/\b(scope|slot|directive|L4|scene_num)\b/i);
      }
    }
  });
});

describe("IntentBar", () => {
  beforeEach(() => {
    _resetSelectionForTests();
    jest.clearAllMocks();
  });

  test("placeholder says 'whole film' when nothing is selected", async () => {
    installFetch([]);
    render(<IntentBar stageOverride="scenario" />);
    const input = await screen.findByTestId("intent-input");
    expect(input).toHaveAttribute(
      "placeholder",
      "Say something about the whole film",
    );
  });

  test("placeholder reflects the selected scene number", async () => {
    installFetch([]);
    render(<IntentBar stageOverride="scenario" />);
    await waitFor(() => {
      expect(
        (global.fetch as unknown as jest.Mock).mock.calls.some(([u]) =>
          String(u).endsWith("/agui/otio/state"),
        ),
      ).toBe(true);
    });
    act(() => {
      selectionStore.getState().selectSlot("scene5_narr", "timeline");
    });
    await waitFor(() => {
      const input = screen.getByTestId("intent-input");
      expect(input).toHaveAttribute("placeholder", "Say something about Scene 5");
    });
  });

  test("clicking a chip opens the cost preview before posting", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    installFetch(calls);
    const user = userEvent.setup();
    render(<IntentBar stageOverride="scenario" />);
    const chip = await screen.findByTestId("intent-chip-make-it-tighter");
    await user.click(chip);
    // DESIGN-07 (#259): the dialog must open first; no /api/directive
    // POST fires until the reviewer confirms.
    expect(calls.length).toBe(0);
    const confirm = await screen.findByTestId("intent-cost-preview-confirm");
    await user.click(confirm);
    await waitFor(() => expect(calls.length).toBe(1));
    const body = JSON.parse(String(calls[0][1]!.body));
    expect(body.directive).toBe("Make it tighter");
    expect(body.slot_context).toBeNull();
  });

  test("cancelling the cost preview skips the /api/directive POST", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    installFetch(calls);
    const user = userEvent.setup();
    render(<IntentBar stageOverride="scenario" />);
    const chip = await screen.findByTestId("intent-chip-friendlier-tone");
    await user.click(chip);
    const cancel = await screen.findByTestId("intent-cost-preview-cancel");
    await user.click(cancel);
    // Give any pending work a chance to settle, then assert no POST.
    await waitFor(() => {
      expect(screen.queryByTestId("intent-cost-preview")).toBeNull();
    });
    expect(calls.length).toBe(0);
  });

  test("closes the cost-preview dialog on /api/directive error so the error banner is visible", async () => {
    // Regression for the Devin Review finding on PR #279: before this
    // fix, an HTTP error from /api/directive left the Radix portal
    // dialog open, hiding the intent-bar error banner behind it.
    (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
      async (url: string) => {
        if (url.endsWith("/agui/otio/state")) return jsonResponse(timeline());
        if (url.endsWith("/dashboard/latest")) {
          return jsonResponse({ status: "running", active_phase: "scenario" });
        }
        if (url.endsWith("/api/directive")) {
          return jsonResponse({ detail: "nope" }, 500);
        }
        return jsonResponse({}, 404);
      },
    ) as unknown as jest.Mock;
    const user = userEvent.setup();
    render(<IntentBar stageOverride="scenario" />);
    const chip = await screen.findByTestId("intent-chip-make-it-tighter");
    await user.click(chip);
    const confirm = await screen.findByTestId("intent-cost-preview-confirm");
    await user.click(confirm);
    await screen.findByTestId("intent-status-error");
    await waitFor(() => {
      expect(screen.queryByTestId("intent-cost-preview")).toBeNull();
    });
  });

  test("typed submit on a selected scene posts scope=scene with scene_num", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    installFetch(calls);
    const user = userEvent.setup();
    render(<IntentBar stageOverride="scenario" />);
    await waitFor(() => {
      expect(
        (global.fetch as unknown as jest.Mock).mock.calls.some(([u]) =>
          String(u).endsWith("/agui/otio/state"),
        ),
      ).toBe(true);
    });
    act(() => {
      selectionStore.getState().selectSlot("scene5_narr", "timeline");
    });
    await user.type(screen.getByTestId("intent-input"), "warmer palette");
    await user.click(screen.getByTestId("intent-submit"));
    // DESIGN-07: confirm the cost preview to fire the POST.
    const confirm = await screen.findByTestId("intent-cost-preview-confirm");
    await user.click(confirm);
    await waitFor(() => expect(calls.length).toBe(1));
    const body = JSON.parse(String(calls[0][1]!.body));
    expect(body.directive).toBe("warmer palette");
    expect(body.slot_context).toMatchObject({
      scope: "scene",
      scope_ref: "5",
      scene_num: 5,
    });
  });
});
