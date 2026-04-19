/** @jest-environment jsdom */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SlotChip } from "@/components/slot-chip";
import {
  _resetSelectionForTests,
  selectionStore,
} from "@/lib/stores/selection";
import type { OtioTimelineStatus } from "@/lib/types";

function makeTimeline(): OtioTimelineStatus {
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
      {
        name: "V1_Video",
        kind: "video",
        total_slots: 0,
        slots: [],
      },
      {
        name: "A2_Music",
        kind: "audio",
        total_slots: 0,
        slots: [],
      },
    ],
  };
}

describe("UI-02b SlotChip", () => {
  beforeEach(() => {
    _resetSelectionForTests();
  });

  test("renders the slot label resolved from the OTIO snapshot", () => {
    render(<SlotChip slotId="scene3_narr" timeline={makeTimeline()} />);
    const btn = screen.getByRole("button", { name: /slot scene3_narr/i });
    expect(btn).toHaveTextContent("scene 3 narration");
    expect(btn).toHaveAttribute("data-state", "default");
    expect(btn).toHaveAttribute("aria-pressed", "false");
  });

  test("falls back to a humanised id when the OTIO snapshot is absent", () => {
    render(<SlotChip slotId="scene3_narr" />);
    expect(
      screen.getByRole("button", { name: /slot scene3_narr/i }),
    ).toHaveTextContent("scene3 narr");
  });

  test("renders a missing state when the slot id is not in OTIO", () => {
    render(<SlotChip slotId="ghost_slot" timeline={makeTimeline()} />);
    const btn = screen.getByRole("button", { name: /ghost_slot/i });
    expect(btn).toHaveAttribute("data-state", "missing");
    expect(btn.getAttribute("aria-label")).toMatch(/missing/i);
  });

  test("click selects the slot (origin=chip) in the shared store", async () => {
    const user = userEvent.setup();
    render(<SlotChip slotId="scene3_narr" timeline={makeTimeline()} />);
    await user.click(screen.getByRole("button"));
    expect(selectionStore.getState().selectedSlotId).toBe("scene3_narr");
    expect(selectionStore.getState().selectionOrigin).toBe("chip");
  });

  test("keyboard Enter activates selection", async () => {
    const user = userEvent.setup();
    render(<SlotChip slotId="scene3_narr" timeline={makeTimeline()} />);
    const btn = screen.getByRole("button");
    btn.focus();
    await user.keyboard("{Enter}");
    expect(selectionStore.getState().selectedSlotId).toBe("scene3_narr");
  });

  test("selected state reflects the store (aria-pressed=true)", async () => {
    const user = userEvent.setup();
    render(<SlotChip slotId="scene3_narr" timeline={makeTimeline()} />);
    await user.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "selected");
  });

  test("fires onSelect callback after selecting", async () => {
    const user = userEvent.setup();
    const onSelect = jest.fn();
    render(
      <SlotChip
        slotId="scene3_narr"
        timeline={makeTimeline()}
        onSelect={onSelect}
      />,
    );
    await user.click(screen.getByRole("button"));
    expect(onSelect).toHaveBeenCalledWith("scene3_narr");
  });

  test("aria-label includes track, duration, and status for sighted+screen-reader parity", () => {
    render(<SlotChip slotId="scene3_narr" timeline={makeTimeline()} />);
    const label = screen.getByRole("button").getAttribute("aria-label") ?? "";
    expect(label).toMatch(/A1_Narration/);
    expect(label).toMatch(/4\.2 seconds/);
    expect(label).toMatch(/delivered/);
  });

  test("snapshot: default / selected / missing", async () => {
    const user = userEvent.setup();
    const { container, rerender } = render(
      <SlotChip slotId="scene3_narr" timeline={makeTimeline()} />,
    );
    expect(container.firstChild).toMatchSnapshot("default");
    await user.click(screen.getByRole("button"));
    expect(container.firstChild).toMatchSnapshot("selected");
    rerender(<SlotChip slotId="ghost_slot" timeline={makeTimeline()} />);
    expect(container.firstChild).toMatchSnapshot("missing");
  });
});
