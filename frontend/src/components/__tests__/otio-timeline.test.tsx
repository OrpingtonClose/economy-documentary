/** @jest-environment jsdom */

/**
 * Render-smoke for {@link OtioTimeline}.
 *
 * Regression gate for the class of bug caught during the UI-PIPE
 * audit (PR #225): a `useState` declaration was lost during rebase
 * conflict resolution and the dashboard crashed at render with
 *
 *    ReferenceError: zoom is not defined
 *
 * The failure was invisible to CI because no test rendered the
 * component.  This test exists purely to import, render, and assert
 * that the canvas mounts without throwing — if the hook wiring drops
 * state again, Jest will fail here rather than a user seeing the red
 * Next.js overlay on first page load.
 */

import { render, screen } from "@testing-library/react";
import type { OtioTimelineStatus } from "@/lib/types";

const populatedTimeline: OtioTimelineStatus = {
  state: "draft",
  total_duration_sec: 30,
  source_file: "demo.otio",
  reconciliation: [],
  tracks: [
    {
      name: "V1_Video",
      kind: "video",
      total_slots: 1,
      slots: [
        {
          slot_id: "scene1_vid",
          track: "V1_Video",
          scene_num: 1,
          phrase_idx: 0,
          start_sec: 0,
          duration_sec: 5,
          status: "delivered",
          label: "scene 1",
          preview_url: "",
          thumbnail_url: "",
          waveform_url: "",
          failure_reason: "",
          rung: "",
          scripted_duration_sec: 5,
          measured_duration_sec: 5,
          metadata: {},
        },
      ],
    },
    { name: "A1_Narration", kind: "audio", total_slots: 0, slots: [] },
    { name: "A2_Music", kind: "audio", total_slots: 0, slots: [] },
  ],
  finished_film: null,
};

jest.mock("@/lib/otio-stream", () => ({
  useOtioStream: () => ({
    timeline: populatedTimeline,
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

import { OtioTimeline } from "@/components/otio-timeline";

describe("OtioTimeline (render smoke)", () => {
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
