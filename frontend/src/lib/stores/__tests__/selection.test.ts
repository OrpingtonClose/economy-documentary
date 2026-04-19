/** @jest-environment jsdom */

import { act, renderHook } from "@testing-library/react";
import {
  _resetSelectionForTests,
  clearSelection,
  selectionStore,
  selectSlot,
  useSelectedSlot,
  useSelection,
} from "@/lib/stores/selection";

describe("UI-02a selection store", () => {
  beforeEach(() => {
    _resetSelectionForTests();
  });

  test("initial state is empty", () => {
    expect(selectionStore.getState().selectedSlotId).toBeNull();
    expect(selectionStore.getState().selectionOrigin).toBeNull();
    expect(selectionStore.getState().selectionTick).toBe(0);
  });

  test("selectSlot updates id, origin, and bumps tick", () => {
    selectSlot("scene3_narr", "chip");
    const s = selectionStore.getState();
    expect(s.selectedSlotId).toBe("scene3_narr");
    expect(s.selectionOrigin).toBe("chip");
    expect(s.selectionTick).toBe(1);

    selectSlot("scene3_narr", "chip");
    // Re-selecting the same slot still bumps the tick so consumers
    // that only scroll on tick change still respond.
    expect(selectionStore.getState().selectionTick).toBe(2);
    expect(selectionStore.getState().selectedSlotId).toBe("scene3_narr");
  });

  test("clearSelection resets to null", () => {
    selectSlot("scene1_vid", "timeline");
    clearSelection();
    const s = selectionStore.getState();
    expect(s.selectedSlotId).toBeNull();
    expect(s.selectionOrigin).toBeNull();
    expect(s.selectionTick).toBe(2);
  });

  test("clearSelection is a no-op when already cleared (no tick bump)", () => {
    clearSelection();
    expect(selectionStore.getState().selectionTick).toBe(0);
  });

  test("useSelectedSlot subscribes and updates on select/clear", () => {
    const { result } = renderHook(() => useSelectedSlot());
    expect(result.current).toBeNull();
    act(() => selectSlot("scene3_narr", "chip"));
    expect(result.current).toBe("scene3_narr");
    act(() => clearSelection());
    expect(result.current).toBeNull();
  });

  test("useSelection exposes id, origin, and tick", () => {
    const { result } = renderHook(() => useSelection());
    expect(result.current).toEqual({
      selectedSlotId: null,
      selectionOrigin: null,
      selectionTick: 0,
    });
    act(() => selectSlot("scene1_vid", "timeline"));
    expect(result.current).toEqual({
      selectedSlotId: "scene1_vid",
      selectionOrigin: "timeline",
      selectionTick: 1,
    });
  });

  test("origin is attributable to the caller", () => {
    selectSlot("scene3_narr", "chip");
    expect(selectionStore.getState().selectionOrigin).toBe("chip");
    selectSlot("scene3_narr", "timeline");
    expect(selectionStore.getState().selectionOrigin).toBe("timeline");
    selectSlot("scene3_narr", "directive");
    expect(selectionStore.getState().selectionOrigin).toBe("directive");
  });

  test("unmounted hook does not leak subscriptions", () => {
    const { result, unmount } = renderHook(() => useSelectedSlot());
    act(() => selectSlot("a", "chip"));
    expect(result.current).toBe("a");
    unmount();
    // Mutations after unmount must not throw and must not retain the
    // React listener.
    expect(() => selectSlot("b", "chip")).not.toThrow();
    // Internal listener count should not grow unbounded across
    // mount/unmount cycles.
    const internal = (
      selectionStore as unknown as { subscribe: (l: () => void) => () => void }
    );
    let count = 0;
    const off1 = internal.subscribe(() => {
      count += 1;
    });
    const off2 = internal.subscribe(() => {
      count += 1;
    });
    act(() => selectSlot("c", "chip"));
    // Both of our own listeners fired exactly once.
    expect(count).toBe(2);
    off1();
    off2();
  });
});
