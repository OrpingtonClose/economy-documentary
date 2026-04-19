/** @jest-environment jsdom */

/**
 * UI-02 bridge tests for the intervention bar.
 *
 * Covers:
 *   - directive input auto-scopes to the selected slot (shows a
 *     "scoped to <slot>" chip above the textarea),
 *   - clicking the chip's × clears selection and drops the scope,
 *   - without any selection the input posts globally (no slot_context).
 *
 * The network and SSE surface of DashboardIntervention is stubbed via
 * `global.fetch` + `global.EventSource` so the tests stay narrowly
 * focused on the bridge behaviour.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  DashboardIntervention,
  deriveSlotContext,
} from "@/components/dashboard-intervention";
import {
  _resetSelectionForTests,
  selectionStore,
} from "@/lib/stores/selection";
import type { OtioTimelineStatus } from "@/lib/types";

// ---- stubs ---------------------------------------------------------------

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

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
    async (url: string, init?: RequestInit) => {
      const result = await handler(url, init);
      return result;
    },
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

function otioTimeline(): OtioTimelineStatus {
  return {
    state: "draft",
    total_duration_sec: 30,
    source_file: "demo.otio",
    reconciliation: [],
    tracks: [
      {
        name: "A1_Narration",
        kind: "audio",
        total_slots: 1,
        slots: [
          {
            slot_id: "scene3_narr",
            track: "A1_Narration",
            scene_num: 3,
            phrase_idx: 0,
            start_sec: 12,
            duration_sec: 4.2,
            status: "delivered",
            label: "scene 3 narration",
            preview_url: "",
            thumbnail_url: "",
            waveform_url: "",
            failure_reason: "",
            rung: "",
            scripted_duration_sec: 4.2,
            measured_duration_sec: 4.2,
            metadata: {},
          },
        ],
      },
      { name: "V1_Video", kind: "video", slots: [], total_slots: 0 },
      { name: "A2_Music", kind: "audio", slots: [], total_slots: 0 },
    ],
  };
}

describe("UI-02 intervention bar bridge", () => {
  beforeEach(() => {
    _resetSelectionForTests();
    jest.clearAllMocks();
    mockFetch((url: string) => {
      if (url.endsWith("/agui/otio/state")) return jsonResponse(otioTimeline());
      if (url.endsWith("/api/halt_state")) {
        return jsonResponse({
          halt_requested: false,
          halted_at_stage: null,
          halt_reviewer: null,
          halt_reason: null,
          halt_timestamp: null,
        });
      }
      if (url.endsWith("/api/directive")) {
        return jsonResponse({
          status: "accepted",
          l4_event_id: "evt-1",
          record_ids: [1],
          records: [
            {
              revision: 1,
              scope: "element",
              scope_ref: "scene3_narr",
              polarity: "prefer",
              subject: "louder",
              content: "prefer louder @ element:scene3_narr",
            },
          ],
          re_manifestation_plans: [],
          scope_hint: { scope: "element", scope_ref: "scene3_narr" },
        });
      }
      return jsonResponse({}, 404);
    });
  });

  test("no selection -> directive posts without a slot_context", async () => {
    const user = userEvent.setup();
    render(<DashboardIntervention />);
    await waitFor(() => {
      expect(screen.queryByTestId("directive-scope-chip")).toBeNull();
    });
    await user.type(screen.getByTestId("directive-input"), "prefer shorter");
    await user.click(screen.getByTestId("directive-submit"));
    await waitFor(() => {
      const calls = (global.fetch as unknown as jest.Mock).mock.calls as Array<
        [string, RequestInit | undefined]
      >;
      const directiveCall = calls.find(([u]) => u.endsWith("/api/directive"));
      expect(directiveCall).toBeDefined();
      const body = JSON.parse(String(directiveCall![1]!.body));
      expect(body.slot_context).toBeNull();
    });
  });

  test("selecting a slot scopes the directive to that slot", async () => {
    const user = userEvent.setup();
    render(<DashboardIntervention />);
    // Wait for the timeline stream to hydrate so slot metadata is
    // resolvable from the selection store.
    await waitFor(() => {
      const calls = (global.fetch as unknown as jest.Mock).mock.calls as Array<
        [string]
      >;
      expect(calls.some(([u]) => u.endsWith("/agui/otio/state"))).toBe(true);
    });

    act(() => {
      selectionStore.getState().selectSlot("scene3_narr", "chip");
    });

    await waitFor(() => {
      const chip = screen.getByTestId("directive-scope-chip");
      expect(chip).toHaveTextContent(/scene 3 narration/i);
    });

    await user.type(screen.getByTestId("directive-input"), "louder");
    await user.click(screen.getByTestId("directive-submit"));

    await waitFor(() => {
      const calls = (global.fetch as unknown as jest.Mock).mock.calls as Array<
        [string, RequestInit | undefined]
      >;
      const directiveCall = calls.find(([u]) => u.endsWith("/api/directive"));
      expect(directiveCall).toBeDefined();
      const body = JSON.parse(String(directiveCall![1]!.body));
      expect(body.slot_context).toMatchObject({
        scope: "element",
        scope_ref: "scene3_narr",
        clip_id: "scene3_narr",
        scene_num: 3,
      });
    });
  });

  test("clicking × on the scope chip clears selection (directive becomes global)", async () => {
    const user = userEvent.setup();
    render(<DashboardIntervention />);

    act(() => {
      selectionStore.getState().selectSlot("scene3_narr", "timeline");
    });
    const clearBtn = await screen.findByTestId("directive-scope-clear");
    await user.click(clearBtn);
    expect(selectionStore.getState().selectedSlotId).toBeNull();
    expect(screen.queryByTestId("directive-scope-chip")).toBeNull();
  });

  test("scope chip × is hidden when selectedSlot prop override is in use", async () => {
    // Parent-owned selection (legacy API): the chip describes the scope
    // but we must NOT render a × that calls the store's clearSelection —
    // it would be a no-op because the prop is the source of truth.
    render(
      <DashboardIntervention
        selectedSlot={{ scope: "element", scope_ref: "override_slot" }}
      />,
    );
    const chip = await screen.findByTestId("directive-scope-chip");
    expect(chip).toHaveTextContent(/override_slot/i);
    expect(screen.queryByTestId("directive-scope-clear")).toBeNull();
  });

  test("deriveSlotContext produces the expected backend payload", () => {
    expect(deriveSlotContext("scene3_narr", null)).toEqual({
      scope: "element",
      scope_ref: "scene3_narr",
      clip_id: "scene3_narr",
    });
    const slot = otioTimeline().tracks[0].slots[0];
    expect(deriveSlotContext(slot.slot_id, slot)).toEqual({
      scope: "element",
      scope_ref: "scene3_narr",
      clip_id: "scene3_narr",
      scene_num: 3,
    });
  });
});
