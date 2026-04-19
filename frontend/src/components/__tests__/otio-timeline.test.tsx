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
  it("mounts without throwing and renders the centrepiece header", () => {
    expect(() => render(<OtioTimeline />)).not.toThrow();
    expect(
      screen.getByText(/OTIO Timeline · Centrepiece/i),
    ).toBeInTheDocument();
  });

  it("declares a numeric zoom state visible in the ZoomControls", () => {
    render(<OtioTimeline />);
    // ZoomControls renders `${zoom.toFixed(0)} px/s`.  If `zoom`
    // regresses to undefined the component throws before this
    // matcher runs.
    expect(screen.getByText(/\d+ px\/s/)).toBeInTheDocument();
  });
});
