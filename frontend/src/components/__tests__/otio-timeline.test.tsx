/** @jest-environment jsdom */

/**
 * Render-smoke + DESIGN-04 (#256) colour-language regression for {@link OtioTimeline}.
 *
 * Originally a regression gate for the class of bug caught during the
 * UI-PIPE audit (PR #225): a `useState` declaration was lost during rebase
 * conflict resolution and the dashboard crashed at render with
 *
 *    ReferenceError: zoom is not defined
 *
 * The failure was invisible to CI because no test rendered the
 * component.  Extended for DESIGN-04: assert that every slot visual
 * carries a ``data-state`` attribute and picks up the
 * ``SLOT_STATE_CLASSES`` map, so drift from the design language is
 * caught at test time rather than when a reviewer eyeballs the dashboard.
 */

import { render, screen } from "@testing-library/react";
import type { OtioSlot, OtioTimelineStatus } from "@/lib/types";

function makeSlot(overrides: Partial<OtioSlot>): OtioSlot {
  return {
    slot_id: "slot",
    track: "V1_Video",
    scene_num: 1,
    phrase_idx: 0,
    start_sec: 0,
    duration_sec: 5,
    status: "pending",
    label: "scene 1",
    preview_url: "",
    thumbnail_url: "",
    waveform_url: "",
    failure_reason: "",
    rung: "",
    scripted_duration_sec: 5,
    measured_duration_sec: null,
    metadata: {},
    ...overrides,
  };
}

function makeTimeline(
  slots: OtioSlot[],
  overrides: Partial<OtioTimelineStatus> = {},
): OtioTimelineStatus {
  return {
    state: "draft",
    total_duration_sec: 30,
    source_file: "demo.otio",
    reconciliation: [],
    tracks: [
      {
        name: "V1_Video",
        kind: "video",
        total_slots: slots.length,
        slots,
      },
      { name: "A1_Narration", kind: "audio", total_slots: 0, slots: [] },
      { name: "A2_Music", kind: "audio", total_slots: 0, slots: [] },
    ],
    finished_film: null,
    ...overrides,
  };
}

let timelineForMock: OtioTimelineStatus = makeTimeline([
  makeSlot({ slot_id: "scene1_vid", status: "delivered" }),
]);

jest.mock("@/lib/otio-stream", () => ({
  useOtioStream: () => ({
    timeline: timelineForMock,
    error: null,
    connected: true,
    openGates: [],
    drift: {
      slotIds: new Set<string>(),
      sceneNums: new Set<number>(),
      slotStages: {} as Record<string, string>,
    },
  }),
}));

jest.mock("@/lib/preview-stream", () => ({
  usePreviewStream: () => ({ state: { entries: {} } }),
  boundaryLabel: () => "boundary",
  boundaryTimeSec: () => 0,
  isPreviewStale: () => false,
}));

jest.mock("@/components/slot-detail-panel", () => ({
  SlotDetailPanel: () => null,
}));
jest.mock("@/components/reconciliation-overlay", () => ({
  ReconciliationOverlay: () => null,
}));
jest.mock("@/components/approval-card", () => ({ ApprovalCard: () => null }));
jest.mock("@/components/preview-modal", () => ({ PreviewModal: () => null }));

import {
  OtioTimeline,
  SLOT_STATE_CLASSES,
  deriveSlotState,
} from "@/components/otio-timeline";

describe("OtioTimeline (render smoke)", () => {
  beforeEach(() => {
    timelineForMock = makeTimeline([
      makeSlot({ slot_id: "scene1_vid", status: "delivered" }),
    ]);
  });

  it("mounts without throwing and renders the plain-English header", () => {
    // UX-05 (#247): header was rewritten from "OTIO Timeline ·
    // Centrepiece" to "Your film so far" so first-time users do not
    // need to know what OTIO is.
    expect(() => render(<OtioTimeline />)).not.toThrow();
    expect(screen.getByText(/Your film so far/i)).toBeInTheDocument();
  });

  it("declares a numeric zoom state surfaced on the zoom controls", () => {
    render(<OtioTimeline />);
    // UX-05 (#247): the ``px/s`` readout moved to a tooltip, so we
    // assert on the data-zoom attribute instead.  If ``zoom`` regresses
    // to undefined the component throws before this matcher runs.
    const zoomEl = screen.getByTestId("otio-zoom-controls");
    expect(zoomEl).toHaveAttribute("data-zoom", expect.stringMatching(/^\d+$/));
    expect(zoomEl.getAttribute("title")).toMatch(/\d+ px\/s/);
  });
});

describe("DESIGN-04 (#256) slot state colour language", () => {
  it("maps each backend status to the expected design-language state", () => {
    const base = makeTimeline([]);
    expect(
      deriveSlotState(makeSlot({ status: "pending" }), base),
    ).toBe("pending");
    expect(
      deriveSlotState(makeSlot({ status: "in_progress" }), base),
    ).toBe("generating");
    expect(
      deriveSlotState(makeSlot({ status: "delivered" }), base),
    ).toBe("drafted");
    expect(
      deriveSlotState(makeSlot({ status: "failed" }), base),
    ).toBe("flagged");
    expect(
      deriveSlotState(makeSlot({ status: "gap" }), base),
    ).toBe("gap");

    // ``approved`` kicks in once the OTIO timeline is authoritative.
    expect(
      deriveSlotState(
        makeSlot({ status: "delivered" }),
        makeTimeline([], { state: "authoritative" }),
      ),
    ).toBe("approved");

    // ``locked`` wins over ``approved`` once the film is stitched.
    expect(
      deriveSlotState(
        makeSlot({ status: "delivered" }),
        makeTimeline([], {
          state: "authoritative",
          finished_film: {
            url: "/agui/final_film/final_documentary.mp4",
            duration_sec: 60,
            language: "",
            alternates: [],
          },
        }),
      ),
    ).toBe("locked");
  });

  it("writes the class-map entry and a data-state attribute for each rendered slot", () => {
    timelineForMock = makeTimeline([
      makeSlot({ slot_id: "p", status: "pending" }),
      makeSlot({
        slot_id: "g",
        status: "in_progress",
        start_sec: 5,
        duration_sec: 4,
      }),
      makeSlot({
        slot_id: "d",
        status: "delivered",
        start_sec: 10,
        duration_sec: 4,
      }),
      makeSlot({
        slot_id: "f",
        status: "failed",
        start_sec: 15,
        duration_sec: 4,
        failure_reason: "needs another take",
      }),
    ]);
    render(<OtioTimeline />);

    const pending = document.querySelector('[data-state="pending"]');
    const generating = document.querySelector('[data-state="generating"]');
    const drafted = document.querySelector('[data-state="drafted"]');
    const flagged = document.querySelector('[data-state="flagged"]');

    expect(pending).not.toBeNull();
    expect(generating).not.toBeNull();
    expect(drafted).not.toBeNull();
    expect(flagged).not.toBeNull();

    // Each rendered slot should pick up *every* token from the state's
    // entry in the class map — catches accidental hex literals being
    // reintroduced outside the map.
    const assertHasClasses = (
      el: Element | null,
      classes: string,
    ) => {
      if (!el) throw new Error("expected element for state");
      for (const token of classes.split(/\s+/).filter(Boolean)) {
        expect(el.className).toEqual(expect.stringContaining(token));
      }
    };
    assertHasClasses(pending, SLOT_STATE_CLASSES.pending);
    assertHasClasses(generating, SLOT_STATE_CLASSES.generating);
    assertHasClasses(drafted, SLOT_STATE_CLASSES.drafted);
    assertHasClasses(flagged, SLOT_STATE_CLASSES.flagged);
  });

  it("renders 'flagged' slots in amber rather than red (nothing red unless action needed)", () => {
    // DESIGN-04 hard constraint: no raw red utilities on slot visuals.
    expect(SLOT_STATE_CLASSES.flagged).toEqual(
      expect.stringContaining("amber"),
    );
    expect(SLOT_STATE_CLASSES.flagged).not.toEqual(
      expect.stringContaining("red-"),
    );
    for (const cls of Object.values(SLOT_STATE_CLASSES)) {
      expect(cls).not.toMatch(/#[0-9a-fA-F]{3,8}/);
    }
  });
});
