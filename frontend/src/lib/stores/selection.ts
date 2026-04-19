"use client";

/**
 * UI-02a (#196) — shared selection state.
 *
 * Lightweight zustand store that owns which OTIO slot the reviewer
 * currently has selected and how that selection originated. Consumers:
 *
 *   - OTIO timeline (visual highlight, scroll-into-view when selection
 *     originates outside the timeline, e.g. from a chat chip).
 *   - Slot detail panel (opens / refreshes on selection).
 *   - Directive input in the intervention bar (scopes `slot_context`).
 *   - `<SlotChip />` (UI-02b) — both producer and consumer.
 *
 * Invariants:
 *
 *   1. Selection is ephemeral; it never round-trips to the backend.
 *      Directives carry it through `slot_context` as a point-in-time
 *      copy, but the store itself is not persisted.
 *   2. `selectedSlotId` must match the OTIO `slot_id` used by the
 *      backend (same ids that appear in `[[slot:ID]]` chips in chat).
 *   3. `selectionOrigin` is informational — it lets consumers decide
 *      whether they need to react (e.g. the timeline scrolls the slot
 *      into view only when the click came from chat, not from itself).
 */

import { useMemo } from "react";
import { createStore, useStore } from "zustand";

export type SelectionOrigin =
  | "chip"
  | "timeline"
  | "panel"
  | "directive"
  | "other";

export interface SelectionState {
  selectedSlotId: string | null;
  selectionOrigin: SelectionOrigin | null;
  /**
   * Monotonic counter incremented on every `selectSlot` call, even
   * when the id is unchanged. Consumers (e.g. the timeline scroll
   * controller) subscribe to this so a second click on the same chip
   * still triggers a scroll-into-view.
   */
  selectionTick: number;
  selectSlot: (slotId: string, origin?: SelectionOrigin) => void;
  clearSelection: () => void;
}

export const selectionStore = createStore<SelectionState>((set, get) => ({
  selectedSlotId: null,
  selectionOrigin: null,
  selectionTick: 0,
  selectSlot: (slotId, origin = "other") => {
    set({
      selectedSlotId: slotId,
      selectionOrigin: origin,
      selectionTick: get().selectionTick + 1,
    });
  },
  clearSelection: () => {
    const s = get();
    if (s.selectedSlotId === null && s.selectionOrigin === null) return;
    set({
      selectedSlotId: null,
      selectionOrigin: null,
      selectionTick: s.selectionTick + 1,
    });
  },
}));

/** Reset helper — test-only. Avoid calling from application code. */
export function _resetSelectionForTests(): void {
  selectionStore.setState(
    {
      selectedSlotId: null,
      selectionOrigin: null,
      selectionTick: 0,
    },
    false,
  );
}

/** Imperative API — safe to call from non-React code (e.g. the chat
 * parser in UI-01c, DOM event handlers, etc.). */
export function selectSlot(
  slotId: string,
  origin: SelectionOrigin = "other",
): void {
  selectionStore.getState().selectSlot(slotId, origin);
}

export function clearSelection(): void {
  selectionStore.getState().clearSelection();
}

/**
 * React hook — returns the whole selection state object (without
 * the mutator functions). Used by components that care about the
 * origin and tick, e.g. the timeline scroll controller and the
 * intervention bar's scope chip.
 */
export function useSelection(): {
  selectedSlotId: string | null;
  selectionOrigin: SelectionOrigin | null;
  selectionTick: number;
} {
  const selectedSlotId = useStore(selectionStore, (s) => s.selectedSlotId);
  const selectionOrigin = useStore(selectionStore, (s) => s.selectionOrigin);
  const selectionTick = useStore(selectionStore, (s) => s.selectionTick);
  return useMemo(
    () => ({ selectedSlotId, selectionOrigin, selectionTick }),
    [selectedSlotId, selectionOrigin, selectionTick],
  );
}

/**
 * React hook — returns just `selectedSlotId`. Used by consumers that
 * only need the id (e.g. `<SlotChip />` for its "selected" visual).
 */
export function useSelectedSlot(): string | null {
  return useStore(selectionStore, (s) => s.selectedSlotId);
}
