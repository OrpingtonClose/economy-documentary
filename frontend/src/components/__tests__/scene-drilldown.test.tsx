/** @jest-environment jsdom */

/**
 * DESIGN-05 (#257) — tests for the scene drilldown Sheet.
 *
 * Covers:
 *
 *   1. The Sheet is closed when no slot is selected.
 *   2. Selecting a slot opens the Sheet with the scene's label in its
 *      title and renders the four tabs (Narration / Visual / QA / Why).
 *   3. QA statuses like ``pass`` / ``fail`` render as plain-English copy
 *      ("Looks good", "Needs a redo") rather than the internal labels.
 *   4. "Redo this scene" POSTs ``scope: "scene"`` to /api/directive with
 *      the scene_num derived from the selected slot.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SceneDrilldown } from "@/components/scene-drilldown";
import {
  _resetSelectionForTests,
  selectionStore,
} from "@/lib/stores/selection";
import type { OtioTimelineStatus, SlotFullView } from "@/lib/types";

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
            slot_id: "scene3_narr",
            track: "A1_Narration",
            scene_num: 3,
            phrase_idx: 0,
            start_sec: 12,
            duration_sec: 4.2,
            status: "delivered",
            label: "Scene 3 narration",
            preview_url: "",
            thumbnail_url: "",
            waveform_url: "",
            failure_reason: "",
            rung: "",
            scripted_duration_sec: 4.2,
            measured_duration_sec: 4.2,
            metadata: { text: "A quiet voice sets the scene." },
          },
        ],
      },
      {
        name: "V1_Video",
        kind: "video",
        total_slots: 1,
        slots: [
          {
            slot_id: "scene3_vid",
            track: "V1_Video",
            scene_num: 3,
            phrase_idx: 0,
            start_sec: 12,
            duration_sec: 4.2,
            status: "delivered",
            label: "Scene 3 clip",
            preview_url: "",
            thumbnail_url: "",
            waveform_url: "",
            failure_reason: "",
            rung: "",
            scripted_duration_sec: 4.2,
            measured_duration_sec: 4.2,
            metadata: { prompt: "Wide shot of a rainy street at dusk." },
          },
        ],
      },
      { name: "A2_Music", kind: "audio", slots: [], total_slots: 0 },
    ],
  };
}

function makeFull(overrides: Partial<SlotFullView> = {}): SlotFullView {
  return {
    slot: {
      slot_id: "scene3_narr",
      track: "A1_Narration",
      scene_num: 3,
      phrase_idx: 0,
      start_sec: 12,
      duration_sec: 4.2,
      status: "delivered",
      label: "Scene 3 narration",
      preview_url: "",
      thumbnail_url: "",
      waveform_url: "",
      failure_reason: "",
      rung: "",
      scripted_duration_sec: 4.2,
      measured_duration_sec: 4.2,
      metadata: {},
    },
    takes: [],
    critiques: [],
    qa_results: [],
    artifacts: [],
    ledger_records: [],
    reasoning_trace_preview: [],
    current_rung: {},
    latest_preview: {},
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch(
  perSlot: Record<string, SlotFullView>,
  directiveCalls: Array<[string, RequestInit | undefined]>,
) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
    async (url: string, init?: RequestInit) => {
      if (url.endsWith("/agui/otio/state")) return jsonResponse(timeline());
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
      const match = url.match(/\/api\/slots\/([^/]+)\/full$/);
      if (match) {
        const slotId = decodeURIComponent(match[1]);
        return jsonResponse(perSlot[slotId] ?? makeFull({ slot: { ...makeFull().slot, slot_id: slotId } }));
      }
      if (url.endsWith("/api/directive")) {
        directiveCalls.push([url, init]);
        return jsonResponse({
          status: "accepted",
          l4_event_id: "evt-scene",
          record_ids: [1],
          records: [],
          re_manifestation_plans: [],
        });
      }
      return jsonResponse({}, 404);
    },
  ) as unknown as jest.Mock;
}

describe("SceneDrilldown", () => {
  beforeEach(() => {
    _resetSelectionForTests();
    jest.clearAllMocks();
  });

  test("is closed when no slot is selected", () => {
    installFetch({}, []);
    render(<SceneDrilldown />);
    expect(screen.queryByTestId("scene-drilldown")).toBeNull();
  });

  test("opens with scene label and four tabs when a slot is selected", async () => {
    installFetch(
      {
        scene3_narr: makeFull(),
        scene3_vid: makeFull({
          slot: { ...makeFull().slot, slot_id: "scene3_vid", track: "V1_Video" },
        }),
      },
      [],
    );
    render(<SceneDrilldown />);
    act(() => {
      selectionStore.getState().selectSlot("scene3_narr", "timeline");
    });
    const title = await screen.findByTestId("scene-drilldown-title");
    expect(title).toHaveTextContent(/Scene 3/);
    expect(screen.getByTestId("scene-tab-narration")).toBeInTheDocument();
    expect(screen.getByTestId("scene-tab-visual")).toBeInTheDocument();
    expect(screen.getByTestId("scene-tab-qa")).toBeInTheDocument();
    expect(screen.getByTestId("scene-tab-why")).toBeInTheDocument();
    expect(screen.getByText("A quiet voice sets the scene.")).toBeInTheDocument();
  });

  test("QA tab translates reviewer labels to plain English", async () => {
    installFetch(
      {
        scene3_narr: makeFull({
          qa_results: [
            { source: "loudness_qa", status: "pass", score: 1, summary: "LUFS within target" },
            { source: "semantic", status: "fail", score: 0, summary: "off-brief" },
          ],
        }),
        scene3_vid: makeFull({
          slot: { ...makeFull().slot, slot_id: "scene3_vid", track: "V1_Video" },
        }),
      },
      [],
    );
    const user = userEvent.setup();
    render(<SceneDrilldown />);
    act(() => {
      selectionStore.getState().selectSlot("scene3_narr", "timeline");
    });
    await user.click(await screen.findByTestId("scene-tab-qa"));
    await waitFor(() => {
      expect(screen.getByText("Looks good")).toBeInTheDocument();
      expect(screen.getByText("Needs a redo")).toBeInTheDocument();
      expect(screen.getByText("Loudness")).toBeInTheDocument();
    });
    // No raw reviewer taxonomy should leak into the QA tab copy.
    expect(screen.queryByText(/\bloudness_qa\b/)).toBeNull();
  });

  test("'Redo this scene' POSTs scope=scene with scene_num", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    installFetch(
      {
        scene3_narr: makeFull(),
        scene3_vid: makeFull({
          slot: { ...makeFull().slot, slot_id: "scene3_vid", track: "V1_Video" },
        }),
      },
      calls,
    );
    const user = userEvent.setup();
    render(<SceneDrilldown />);
    act(() => {
      selectionStore.getState().selectSlot("scene3_narr", "timeline");
    });
    const redo = await screen.findByTestId("scene-redo-button");
    await user.click(redo);
    await waitFor(() => {
      expect(calls.length).toBeGreaterThan(0);
    });
    const body = JSON.parse(String(calls[0][1]!.body));
    expect(body.slot_context).toMatchObject({
      scope: "scene",
      scope_ref: "3",
      scene_num: 3,
    });
    expect(body.directive).toMatch(/Scene 3/);
  });
});
