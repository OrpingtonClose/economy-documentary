"use client";

/**
 * UI-02b (#197) — accessible slot chip.
 *
 * Clickable pill used wherever chat (and anywhere else) references an
 * OTIO slot. Produced speculatively for UI-01c (#186) so the chat
 * parser can drop it in once it lands: `[[slot:scene3_narr]]` tokens
 * in assistant messages are parsed via {@link parseSlotTokens} and
 * each slot segment is rendered as `<SlotChip slotId="scene3_narr" />`.
 *
 * Visual states:
 *
 *   - default    — slot exists in the OTIO, not currently selected.
 *   - selected   — slot exists and matches `useSelectedSlot()`.
 *   - missing    — slot id not found in the current OTIO snapshot
 *                  (e.g. a stale chat message that references a slot
 *                  from before a rescenariod re-synth). Rendered faded
 *                  with "missing" tooltip; still clickable so the
 *                  reviewer can try — `selectSlot` will fire and the
 *                  detail panel's own missing-slot handling kicks in.
 *
 * Accessibility:
 *
 *   - `role="button"` (the chip is rendered as a `<button>`).
 *   - `aria-label` includes the resolved label, track, duration, and
 *     status so screen readers announce meaningful context.
 *   - `aria-pressed` reflects selected state.
 *   - Keyboard: native `<button>` handles Enter/Space activation.
 */

import { useCallback, useMemo } from "react";
import { useStore } from "zustand/react";
import type { OtioSlot, OtioTimelineStatus } from "@/lib/types";
import {
  selectionStore,
  selectSlot as selectSlotImperative,
  type SelectionOrigin,
} from "@/lib/stores/selection";

const SELECTED_CLASSES =
  "border-pipeline-accent bg-pipeline-accent/30 text-pipeline-text ring-2 ring-pipeline-accent";
const DEFAULT_CLASSES =
  "border-pipeline-blue/70 bg-pipeline-blue/20 text-pipeline-accent hover:bg-pipeline-blue/40 hover:text-pipeline-text";
const MISSING_CLASSES =
  "border-dashed border-pipeline-blue/40 bg-transparent text-pipeline-muted/70 hover:text-pipeline-muted";

export interface SlotChipProps {
  slotId: string;
  /** Optional override label. Defaults to the OTIO slot's `label`. */
  label?: string;
  /**
   * Optional OTIO snapshot. Provided when the caller already has the
   * timeline in scope (avoids subscribing to the SSE store from each
   * chip). When omitted, the chip resolves the label/metadata from
   * its own snapshot via the parent (the chat message component
   * usually passes the timeline prop down once).
   */
  timeline?: OtioTimelineStatus | null;
  /** Attribution for the resulting selection event. Defaults to `chip`. */
  origin?: SelectionOrigin;
  /**
   * Optional hook for side-effects (e.g. timeline scroll). Called
   * *after* the store has been updated. The OTIO timeline scroll
   * controller usually drives this via its own subscription to
   * `selectionTick`, so callers can leave this undefined.
   */
  onSelect?: (slotId: string) => void;
  className?: string;
}

export function SlotChip({
  slotId,
  label,
  timeline,
  origin = "chip",
  onSelect,
  className,
}: SlotChipProps) {
  const selectedSlotId = useStore(selectionStore, (s) => s.selectedSlotId);
  const selected = selectedSlotId === slotId;

  const slotMeta = useMemo(
    () => (timeline ? findSlot(timeline, slotId) : null),
    [timeline, slotId],
  );
  const missing = timeline != null && slotMeta == null;

  const resolvedLabel =
    label ?? slotMeta?.label ?? humaniseSlotId(slotId);
  const ariaLabel = buildAriaLabel(slotId, slotMeta, resolvedLabel, {
    missing,
    selected,
  });

  const handleClick = useCallback(() => {
    selectSlotImperative(slotId, origin);
    onSelect?.(slotId);
  }, [slotId, origin, onSelect]);

  const stateClasses = selected
    ? SELECTED_CLASSES
    : missing
    ? MISSING_CLASSES
    : DEFAULT_CLASSES;

  return (
    <button
      type="button"
      role="button"
      onClick={handleClick}
      aria-pressed={selected}
      aria-label={ariaLabel}
      data-testid={`slot-chip-${slotId}`}
      data-slot-id={slotId}
      data-state={missing ? "missing" : selected ? "selected" : "default"}
      className={[
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[11px] leading-tight transition",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-pipeline-accent",
        stateClasses,
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span aria-hidden="true" className="opacity-70">
        ◉
      </span>
      <span className="truncate max-w-[18ch]">{resolvedLabel}</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function findSlot(
  timeline: OtioTimelineStatus,
  slotId: string,
): OtioSlot | null {
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      if (slot.slot_id === slotId) return slot;
    }
  }
  return null;
}

function buildAriaLabel(
  slotId: string,
  slot: OtioSlot | null,
  label: string,
  flags: { missing: boolean; selected: boolean },
): string {
  const parts: string[] = [`slot ${slotId}`];
  if (label && label !== slotId) parts.push(label);
  if (slot) {
    parts.push(`track ${slot.track}`);
    parts.push(`${slot.duration_sec.toFixed(1)} seconds`);
    parts.push(`status ${slot.status}`);
  }
  if (flags.missing) parts.push("missing from current timeline");
  if (flags.selected) parts.push("selected");
  return parts.join(", ");
}

function humaniseSlotId(slotId: string): string {
  // `scene3_narr` → `scene3 narr` — just a readable fallback for chips
  // rendered before the OTIO snapshot has arrived.
  return slotId.replace(/_/g, " ");
}
