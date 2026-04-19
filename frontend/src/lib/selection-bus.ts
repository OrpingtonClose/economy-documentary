/**
 * Cross-panel selection bus.
 *
 * UI-01c (#195) chips in the chat panel dispatch a selection action; the
 * OTIO timeline and slot-detail panel listen for it and scroll / focus
 * the matching slot.  UI-02a will eventually reshape this into a global
 * store -- until then, a lightweight ``window`` event channel keeps the
 * chat and timeline panels loosely coupled without forcing a React
 * context refactor for this feature alone.
 *
 * The channel is intentionally not typed into a Redux-style store so
 * non-React callers (e.g. ad-hoc devtools, keyboard shortcuts from the
 * dashboard shell) can participate without instantiating providers.
 */

export const SLOT_SELECTION_EVENT = "docpipe:slot-select";
export const PREVIEW_OPEN_EVENT = "docpipe:preview-open";

export type SlotSelectionDetail = {
  slotId: string;
  /** Where the selection originated -- useful for telemetry. */
  source: "chat-chip" | "timeline" | "slot-detail" | "other";
};

export type PreviewOpenDetail = {
  boundary: string;
  source: "chat-chip" | "timeline" | "other";
};

/**
 * Emit a slot-selection action.
 *
 * No-ops outside a browser environment so the helper is safe to call
 * from code paths exercised under Next.js SSR or unit tests.
 */
export function dispatchSlotSelection(detail: SlotSelectionDetail): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<SlotSelectionDetail>(SLOT_SELECTION_EVENT, { detail })
  );
}

export function subscribeSlotSelection(
  handler: (detail: SlotSelectionDetail) => void
): () => void {
  if (typeof window === "undefined") return () => undefined;
  const listener = (event: Event) => {
    const ce = event as CustomEvent<SlotSelectionDetail>;
    if (ce.detail && typeof ce.detail.slotId === "string") handler(ce.detail);
  };
  window.addEventListener(SLOT_SELECTION_EVENT, listener);
  return () => window.removeEventListener(SLOT_SELECTION_EVENT, listener);
}

export function dispatchPreviewOpen(detail: PreviewOpenDetail): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<PreviewOpenDetail>(PREVIEW_OPEN_EVENT, { detail })
  );
}

export function subscribePreviewOpen(
  handler: (detail: PreviewOpenDetail) => void
): () => void {
  if (typeof window === "undefined") return () => undefined;
  const listener = (event: Event) => {
    const ce = event as CustomEvent<PreviewOpenDetail>;
    if (ce.detail && typeof ce.detail.boundary === "string") {
      handler(ce.detail);
    }
  };
  window.addEventListener(PREVIEW_OPEN_EVENT, listener);
  return () => window.removeEventListener(PREVIEW_OPEN_EVENT, listener);
}
