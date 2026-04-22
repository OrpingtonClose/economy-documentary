"use client";

/**
 * ARCH-H1 / ARCH-H2 / ARCH-H3 — OTIO centrepiece timeline.
 *
 * Renders the authoritative (or draft-with-reconciliation-overlay) OTIO
 * timeline as three horizontal tracks drawn to scale against real time:
 *
 *   V1_Video       — thumbnails for delivered clips; placeholder / amber /
 *                    red bars for pending / in-progress / failed.
 *   A1_Narration   — waveform strips for delivered narration.
 *   A2_Music       — waveform strips for delivered beds.
 *
 * Invariants (also tested on the backend):
 *   1. Slots are rendered at `duration_sec * pixels_per_second`.  If scene 3
 *      is 12.4 s in OTIO, it is 12.4 s wide at 1× zoom — no flex magic.
 *   2. Event-driven, not poll-driven.  All updates come through
 *      {@link useOtioStream}'s EventSource.
 *   3. Read-only.  Clicking a slot opens the H3 side panel; no mutation
 *      flows from the timeline (preference interpreter is H4).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOtioStream } from "@/lib/otio-stream";
import {
  usePreviewStream,
  boundaryLabel,
  boundaryTimeSec,
  isPreviewStale,
} from "@/lib/preview-stream";
import type { PreviewEntry } from "@/lib/preview-stream";
import type {
  DriftState,
  OtioSlot,
  OtioTimelineStatus,
  OtioTrack,
} from "@/lib/types";
import { SlotDetailPanel } from "@/components/slot-detail-panel";
import { ReconciliationOverlay } from "@/components/reconciliation-overlay";
import { ApprovalCard } from "@/components/approval-card";
import { FinishedFilmCard } from "@/components/finished-film-card";
import { PreviewModal } from "@/components/preview-modal";
import { subscribeSlotSelection } from "@/lib/selection-bus";
import {
  selectionStore,
  useSelection,
} from "@/lib/stores/selection";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

const TRACK_HEIGHT_PX = 72;
const RULER_HEIGHT_PX = 28;
const TRACK_LABEL_WIDTH_PX = 120;
const PREVIEW_RIBBON_HEIGHT_PX = 26;

const TRACK_DEFS: Array<{
  name: OtioTrack["name"];
  label: string;
  accent: string;
}> = [
  { name: "V1_Video", label: "V1 · Video", accent: "bg-blue-500/60" },
  { name: "A1_Narration", label: "A1 · Narration", accent: "bg-emerald-500/60" },
  { name: "A2_Music", label: "A2 · Music", accent: "bg-purple-500/60" },
];

// ---------------------------------------------------------------------------
// DESIGN-04 (#256) — state-coded colour language
// ---------------------------------------------------------------------------

/** Six UI states keyed off each slot. ``gap`` is the seventh rendering
 * branch (not a slot state; an OTIO gap segment).  Every slot visual is
 * driven by one of these values — no ad-hoc colour decisions.
 */
export type SlotVisualState =
  | "pending"
  | "generating"
  | "drafted"
  | "approved"
  | "flagged"
  | "locked";

/** Class map the timeline commits to.  All colours flow through either
 * Tailwind tokens or shadcn CSS variables — no raw hex literals live
 * inside slot rendering, which makes theme swaps a one-file change and
 * keeps the ribbon in sync with the rest of the dashboard.
 *
 * Design invariants (issue #256):
 *   - ``pending``    — outline only (not started yet).
 *   - ``generating`` — soft green pulse (working now).
 *   - ``drafted``    — muted fill (have a draft, not yet signed off).
 *   - ``approved``   — solid fill (committed / authoritative).
 *   - ``flagged``    — amber border (needs attention; never red).
 *   - ``locked``     — gold border (final cut, do-not-touch).
 */
export const SLOT_STATE_CLASSES: Record<SlotVisualState, string> = {
  pending:
    "border border-dashed border-muted-foreground/50 bg-transparent text-muted-foreground",
  generating:
    "border border-emerald-400 bg-emerald-500/20 text-emerald-50 animate-pulse",
  drafted:
    "border border-muted-foreground/40 bg-muted/40 text-foreground",
  approved:
    "border border-emerald-500 bg-emerald-600/70 text-emerald-50",
  flagged:
    "border-2 border-amber-500 bg-amber-500/20 text-amber-50",
  locked:
    "border-2 border-yellow-500 bg-yellow-500/20 text-yellow-50",
};

/** Derive the UI state from the backend ``slot.status`` plus timeline
 * context. The backend never emits the six design-language states
 * directly — we project them here so the DESIGN-04 swap is a
 * frontend-only change (no backend migration, per issue scope).
 *
 *   pending     → pending
 *   in_progress → generating
 *   delivered   → locked   (if the finished film has been stitched)
 *                 approved (if the OTIO timeline is authoritative)
 *                 drafted  (timeline still in draft)
 *   failed      → flagged  (amber — nothing is red unless action needed)
 *   gap         → not a slot state; rendered as a dashed pacing silence.
 */
export function deriveSlotState(
  slot: OtioSlot,
  timeline: OtioTimelineStatus,
): SlotVisualState | "gap" {
  switch (slot.status) {
    case "pending":
      return "pending";
    case "in_progress":
      return "generating";
    case "delivered":
      if (timeline.finished_film) return "locked";
      if (timeline.state === "authoritative") return "approved";
      return "drafted";
    case "failed":
      return "flagged";
    case "gap":
      return "gap";
    default:
      return "pending";
  }
}

export function OtioTimeline() {
  const { timeline, connected, error, openGates, drift } = useOtioStream();
  const { state: previewState } = usePreviewStream();
  const { selectedSlotId, selectionOrigin, selectionTick } = useSelection();
  const [zoom, setZoom] = useState<number>(40); // pixels per second
  const [openBoundary, setOpenBoundary] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const handleSelect = useCallback((slotId: string) => {
    selectionStore.getState().selectSlot(slotId, "timeline");
  }, []);

  const handleClearSelection = useCallback(() => {
    selectionStore.getState().clearSelection();
  }, []);

  // UI-02 bridge: when selection originates *outside* the timeline
  // (chat chip, detail panel, directive input) scroll the selected
  // slot into view. Scrolling on an in-timeline click would fight the
  // user's intent, so we gate on `selectionOrigin !== "timeline"`.
  useEffect(() => {
    if (!selectedSlotId) return;
    if (selectionOrigin === "timeline") return;
    if (!timeline) return;
    const container = scrollRef.current;
    if (!container) return;
    const slot = findSlotInTimeline(timeline, selectedSlotId);
    if (!slot) return;
    const slotLeftPx = TRACK_LABEL_WIDTH_PX + slot.start_sec * zoom;
    const slotRightPx =
      slotLeftPx + Math.max(slot.duration_sec * zoom, 2);
    const viewLeft = container.scrollLeft;
    const viewRight = viewLeft + container.clientWidth;
    if (slotLeftPx < viewLeft || slotRightPx > viewRight) {
      // centre the slot when it's out of view
      const target =
        slotLeftPx - container.clientWidth / 2 + (slot.duration_sec * zoom) / 2;
      container.scrollTo({
        left: Math.max(0, target),
        behavior: "smooth",
      });
    }
  }, [selectedSlotId, selectionOrigin, selectionTick, timeline, zoom]);

  // UI-01c (#195): react to chat-chip clicks by selecting the referenced
  // slot.  The chip dispatches via the selection bus so the coupling
  // stays one-way and the chat panel doesn't need a ref into the timeline.
  useEffect(() => {
    return subscribeSlotSelection((detail) => {
      if (detail.source !== "timeline") {
        selectionStore.getState().selectSlot(detail.slotId, "chip");
      }
    });
  }, []);

  if (!timeline) {
    return (
      <div className="flex h-64 items-center justify-center text-pipeline-muted">
        {error ? `Failed to load OTIO: ${error}` : "Loading OTIO timeline…"}
      </div>
    );
  }

  const totalDuration =
    timeline.total_duration_sec || derivedTotalDuration(timeline);

  const sceneEndSecByNum = computeSceneEnds(timeline);
  const actEndSecByNum = computeActEnds(timeline);
  const previews = Object.values(previewState.entries);
  const openPreview = openBoundary ? previewState.entries[openBoundary] : null;

  return (
    <div className="flex h-full flex-col gap-3">
      <header className="flex items-center justify-between px-1">
        <div>
          {/* UX-05 (#247): plain-English header. The older
            * "OTIO Timeline · Centrepiece" phrasing only makes sense to
            * engineers who know what OTIO is. */}
          <h2 className="text-lg font-semibold text-pipeline-accent">
            Your film so far
          </h2>
          <p className="text-xs text-pipeline-muted">
            Scenes, narration, and music drawn to scale against real seconds.
            {/* UX-05 (#247): filesystem path removed from the header.
              * It leaked a CI-only detail (``demo.otio``) and made the
              * dashboard look like a debug console. The path is still
              * available to power users via the slot detail drilldown. */}
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs">
          {/* UX-05 (#247): consolidate the three internal status badges
            * (draft/authoritative, total duration, SSE live/connecting)
            * into small info icons with title tooltips. Primary users
            * no longer see jargon in the header; advanced users still
            * get the details on hover. */}
          <InfoDot
            label={
              timeline.state === "authoritative"
                ? "Locked"
                : "Still drafting"
            }
            tooltip={
              timeline.state === "authoritative"
                ? "Timeline finalised — pacing locked."
                : "Timeline still being assembled. A reconciliation overlay highlights segments that are still moving."
            }
            tone={timeline.state === "authoritative" ? "ok" : "warn"}
            testId="otio-state-info"
          />
          <InfoDot
            label="Length"
            tooltip={`Total runtime: ${totalDuration.toFixed(1)} seconds.`}
            tone="muted"
            testId="otio-duration-info"
          />
          <InfoDot
            label={connected ? "Live" : "Reconnecting"}
            tooltip={
              connected
                ? "Live stream connected — updates arrive as the pipeline runs."
                : "Reconnecting to the live event stream…"
            }
            tone={connected ? "ok" : "warn"}
            testId="otio-sse-info"
          />
          <ZoomControls zoom={zoom} onChange={setZoom} />
        </div>
      </header>

      {timeline.finished_film && (
        // UX-01: definitive "your movie is ready" surface.  Renders
        // only when the assembly callback has written a
        // ``final_documentary*.mp4`` -- see
        // ``otio_timeline_model._detect_finished_film``.
        <FinishedFilmCard film={timeline.finished_film} />
      )}

      <div className="relative flex-1 overflow-hidden rounded-lg border border-pipeline-blue/60 bg-pipeline-card">
        {openGates.length > 0 && (
          // UI-03b (#199): inline approval cards.  Absolutely positioned
          // overlay so mount/unmount does not shift the track layout --
          // one of the DoD criteria on the issue.
          <div
            className="pointer-events-none absolute right-3 top-3 z-20 flex w-80 flex-col gap-2"
            data-testid="approval-card-stack"
          >
            {openGates.map((gate) => (
              <ApprovalCard key={gate.stage} gate={gate} />
            ))}
          </div>
        )}
        <div
          ref={scrollRef}
          className="relative h-full overflow-x-auto overflow-y-hidden"
          onClick={(e) => {
            // Clicking background (but not a slot button) clears selection.
            if (e.target === e.currentTarget) handleClearSelection();
          }}
          data-testid="otio-scroll-container"
        >
          <div
            style={{
              width:
                TRACK_LABEL_WIDTH_PX +
                Math.max(totalDuration, 10) * zoom +
                32,
              minHeight:
                RULER_HEIGHT_PX +
                (previews.length > 0 ? PREVIEW_RIBBON_HEIGHT_PX : 0) +
                TRACK_DEFS.length * TRACK_HEIGHT_PX +
                16,
            }}
            className="relative"
          >
            <TimelineRuler
              totalDuration={totalDuration}
              zoom={zoom}
              leftPx={TRACK_LABEL_WIDTH_PX}
            />

            <PreviewMarkerRibbon
              previews={previews}
              totalDuration={totalDuration}
              zoom={zoom}
              leftPx={TRACK_LABEL_WIDTH_PX}
              sceneEndSecByNum={sceneEndSecByNum}
              actEndSecByNum={actEndSecByNum}
              isStale={(p) => isPreviewStale(p, previewState)}
              onOpen={setOpenBoundary}
            />

            {TRACK_DEFS.map((def, idx) => {
              const track = timeline.tracks.find((t) => t.name === def.name);
              const ribbonOffset =
                previews.length > 0 ? PREVIEW_RIBBON_HEIGHT_PX : 0;
              const top =
                RULER_HEIGHT_PX + ribbonOffset + idx * TRACK_HEIGHT_PX + 4;
              return (
                <TrackRow
                  key={def.name}
                  top={top}
                  track={
                    track || {
                      name: def.name,
                      kind: def.name === "V1_Video" ? "video" : "audio",
                      slots: [],
                      total_slots: 0,
                    }
                  }
                  label={def.label}
                  accent={def.accent}
                  zoom={zoom}
                  totalDuration={totalDuration}
                  onSelect={handleSelect}
                  selectedSlotId={selectedSlotId}
                  drift={drift}
                  timeline={timeline}
                />
              );
            })}

            {timeline.state === "draft" && (
              <ReconciliationOverlay
                reconciliation={timeline.reconciliation}
                zoom={zoom}
                leftPx={TRACK_LABEL_WIDTH_PX}
                narrationTop={
                  RULER_HEIGHT_PX +
                  (previews.length > 0 ? PREVIEW_RIBBON_HEIGHT_PX : 0) +
                  1 * TRACK_HEIGHT_PX +
                  4
                }
              />
            )}
          </div>
        </div>
      </div>

      {selectedSlotId && (
        <SlotDetailPanel
          slotId={selectedSlotId}
          onClose={handleClearSelection}
        />
      )}

      {openPreview && (
        <PreviewModal
          preview={openPreview}
          stale={isPreviewStale(openPreview, previewState)}
          onClose={() => setOpenBoundary(null)}
        />
      )}
    </div>
  );
}

function findSlotInTimeline(
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

// ---------------------------------------------------------------------------
// Preview markers (UI-06b #209)
// ---------------------------------------------------------------------------

function PreviewMarkerRibbon({
  previews,
  totalDuration,
  zoom,
  leftPx,
  sceneEndSecByNum,
  actEndSecByNum,
  isStale,
  onOpen,
}: {
  previews: PreviewEntry[];
  totalDuration: number;
  zoom: number;
  leftPx: number;
  sceneEndSecByNum: Record<number, number>;
  actEndSecByNum: Record<number, number>;
  isStale: (p: PreviewEntry) => boolean;
  onOpen: (boundary: string) => void;
}) {
  if (previews.length === 0) return null;
  return (
    <div
      className="absolute left-0 right-0 border-b border-pipeline-blue/40 bg-pipeline-card/40"
      style={{
        top: RULER_HEIGHT_PX,
        height: 26,
        paddingLeft: leftPx,
      }}
      data-testid="preview-marker-ribbon"
    >
      <div className="relative h-full">
        {previews.map((p) => {
          const t = boundaryTimeSec(p.boundary, {
            totalDuration,
            sceneEndSecByNum,
            actEndSecByNum,
          });
          if (t === null) return null;
          const stale = isStale(p);
          const failed = p.status === "failed";
          return (
            <button
              key={p.boundary}
              type="button"
              onClick={() => onOpen(p.boundary)}
              title={`${boundaryLabel(p.boundary)} — ${
                failed ? "failed" : stale ? "stale" : "ready"
              }`}
              aria-label={`Open preview: ${boundaryLabel(p.boundary)}${
                failed ? " (failed)" : stale ? " (stale)" : ""
              }`}
              data-testid={`preview-marker-${p.boundary}`}
              data-stale={stale ? "true" : "false"}
              data-status={p.status}
              className={
                "absolute top-1 flex h-5 items-center gap-1 rounded-full border px-2 text-[11px] font-medium shadow " +
                (failed
                  ? "border-red-500/70 bg-red-900/60 text-red-100 hover:bg-red-900/80"
                  : stale
                    ? "border-amber-500/70 bg-amber-900/50 text-amber-100 hover:bg-amber-900/70"
                    : "border-emerald-500/70 bg-emerald-900/60 text-emerald-100 hover:bg-emerald-900/80")
              }
              style={{
                left: Math.max(0, t * zoom - 10),
              }}
            >
              <span aria-hidden="true">{failed ? "⚠" : "▶"}</span>
              <span className="whitespace-nowrap">
                {boundaryLabel(p.boundary)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function computeSceneEnds(
  timeline: OtioTimelineStatus,
): Record<number, number> {
  const out: Record<number, number> = {};
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      if (typeof slot.scene_num !== "number" || slot.scene_num < 1) continue;
      const end = slot.start_sec + slot.duration_sec;
      const prior = out[slot.scene_num] ?? 0;
      if (end > prior) out[slot.scene_num] = end;
    }
  }
  return out;
}

function computeActEnds(
  timeline: OtioTimelineStatus,
): Record<number, number> {
  const out: Record<number, number> = {};
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      const act =
        typeof slot.metadata?.act_num === "number"
          ? (slot.metadata.act_num as number)
          : typeof slot.metadata?.act === "number"
            ? (slot.metadata.act as number)
            : null;
      if (act === null || act < 1) continue;
      const end = slot.start_sec + slot.duration_sec;
      const prior = out[act] ?? 0;
      if (end > prior) out[act] = end;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Ruler
// ---------------------------------------------------------------------------

function TimelineRuler({
  totalDuration,
  zoom,
  leftPx,
}: {
  totalDuration: number;
  zoom: number;
  leftPx: number;
}) {
  const ticks = useMemo(() => {
    const stepSec = zoom >= 60 ? 1 : zoom >= 30 ? 2 : zoom >= 15 ? 5 : 10;
    const out: number[] = [];
    for (let t = 0; t <= totalDuration + stepSec; t += stepSec) {
      out.push(t);
    }
    return out;
  }, [totalDuration, zoom]);

  return (
    <div
      className="absolute left-0 right-0 top-0 border-b border-pipeline-blue/40 bg-pipeline-bg/80 text-[10px] uppercase tracking-wider text-pipeline-muted"
      style={{ height: RULER_HEIGHT_PX, paddingLeft: leftPx }}
    >
      <div className="relative h-full">
        {ticks.map((t) => (
          <div
            key={t}
            className="absolute top-0 bottom-0"
            style={{ left: t * zoom }}
          >
            <div className="h-2 w-px bg-pipeline-blue/70" />
            <div className="px-1 text-pipeline-muted">{t.toFixed(0)}s</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Track row
// ---------------------------------------------------------------------------

function TrackRow({
  top,
  track,
  label,
  accent,
  zoom,
  totalDuration,
  onSelect,
  selectedSlotId,
  drift,
  timeline,
}: {
  top: number;
  track: OtioTrack;
  label: string;
  accent: string;
  zoom: number;
  totalDuration: number;
  onSelect: (slotId: string) => void;
  selectedSlotId: string | null;
  drift: DriftState;
  timeline: OtioTimelineStatus;
}) {
  return (
    <div
      className="absolute left-0"
      style={{ top, height: TRACK_HEIGHT_PX - 8 }}
    >
      <div
        className={
          "absolute left-0 flex h-full items-center gap-2 border-r border-pipeline-blue/60 bg-pipeline-card px-3 text-xs font-semibold text-pipeline-text"
        }
        style={{ width: TRACK_LABEL_WIDTH_PX }}
      >
        <span className={"inline-block h-3 w-3 rounded-full " + accent} />
        {label}
      </div>
      <div
        className="absolute top-0"
        style={{
          left: TRACK_LABEL_WIDTH_PX,
          width: Math.max(totalDuration, 10) * zoom,
          height: TRACK_HEIGHT_PX - 8,
        }}
      >
        {track.slots.length === 0 ? (
          <div className="flex h-full items-center px-2 text-xs text-pipeline-muted">
            empty track
          </div>
        ) : (
          track.slots.map((slot) => {
            const drifting =
              drift.slotIds.has(slot.slot_id) ||
              drift.sceneNums.has(slot.scene_num);
            const driftStage = drift.slotStages[slot.slot_id] || null;
            return (
              <SlotBlock
                key={slot.slot_id}
                slot={slot}
                state={deriveSlotState(slot, timeline)}
                zoom={zoom}
                selected={selectedSlotId === slot.slot_id}
                onSelect={onSelect}
                drifting={drifting}
                driftStage={driftStage}
              />
            );
          })
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Slot rendering
// ---------------------------------------------------------------------------

function SlotBlock({
  slot,
  state,
  zoom,
  selected,
  onSelect,
  drifting,
  driftStage,
}: {
  slot: OtioSlot;
  state: SlotVisualState | "gap";
  zoom: number;
  selected: boolean;
  onSelect: (slotId: string) => void;
  drifting: boolean;
  driftStage: string | null;
}) {
  const left = slot.start_sec * zoom;
  const width = Math.max(slot.duration_sec * zoom, 2);

  if (state === "gap") {
    return (
      <div
        className="absolute top-0 h-full border-l border-dashed border-pipeline-blue/50"
        style={{ left, width }}
        data-state="gap"
        title={`gap · ${slot.duration_sec.toFixed(2)}s`}
      />
    );
  }

  // UI-05b: amber outline + badge for slots whose derivation is
  // drifting against the preference ledger.  Never mutates the
  // OTIO itself -- paint-only, driven entirely by SSE.
  const badgeText = driftStage || "re-manifesting";
  const tooltip = drifting
    ? `${slotTooltip(slot)}\ndrift: ${badgeText}`
    : slotTooltip(slot);

  return (
    <button
      type="button"
      onClick={() => onSelect(slot.slot_id)}
      title={tooltip}
      data-drifting={drifting ? "true" : undefined}
      data-state={state}
      className={
        "group absolute top-1 flex h-[calc(100%-6px)] items-stretch overflow-hidden rounded text-[10px] transition " +
        SLOT_STATE_CLASSES[state] +
        (selected ? " ring-2 ring-pipeline-accent" : "") +
        (drifting
          ? " outline outline-2 outline-amber-400 outline-offset-[-2px] animate-pulse"
          : "")
      }
      style={{ left, width }}
    >
      <div className="flex h-full w-full flex-col items-start justify-between p-1 text-left">
        <div className="line-clamp-2 font-medium">
          {state === "flagged" && slot.failure_reason
            ? `⚠ ${slot.failure_reason}`
            : state === "generating"
            ? `${slot.rung || "running"}…`
            : slot.label}
        </div>
        <div className="flex w-full items-center justify-between gap-1 text-[9px] opacity-75">
          <span>{slot.duration_sec.toFixed(2)}s</span>
          {drifting && (
            <span className="rounded bg-amber-500/90 px-1 py-[1px] text-[8px] font-semibold uppercase tracking-wide text-amber-950">
              {badgeText}
            </span>
          )}
        </div>
      </div>

      {/* Delivered video: thumbnail strip. ``drafted`` / ``approved`` /
        * ``locked`` all imply the clip was produced and B2-uploaded. */}
      {slot.status === "delivered" && slot.track === "V1_Video" && slot.thumbnail_url && (
        /* eslint-disable-next-line @next/next/no-img-element */
        <img
          src={BACKEND_URL + slot.thumbnail_url}
          alt="thumb"
          className="pointer-events-none absolute inset-0 h-full w-full object-cover opacity-70"
        />
      )}

      {/* Delivered audio: waveform strip */}
      {slot.status === "delivered" && slot.waveform_url && (
        <WaveformStrip url={BACKEND_URL + slot.waveform_url} />
      )}
    </button>
  );
}

function slotTooltip(slot: OtioSlot): string {
  const parts = [
    `${slot.slot_id}`,
    `${slot.duration_sec.toFixed(2)}s @ ${slot.start_sec.toFixed(2)}s`,
    `status: ${slot.status}`,
  ];
  if (slot.scripted_duration_sec != null) {
    parts.push(`scripted ${slot.scripted_duration_sec.toFixed(2)}s`);
  }
  if (slot.measured_duration_sec != null) {
    parts.push(`measured ${slot.measured_duration_sec.toFixed(2)}s`);
  }
  if (slot.failure_reason) {
    parts.push(`failure: ${slot.failure_reason}`);
  }
  return parts.join("\n");
}

function WaveformStrip({ url }: { url: string }) {
  const [samples, setSamples] = useState<number[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then((data: { samples: number[] }) => {
        if (!cancelled) setSamples(data.samples || []);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [url]);

  if (!samples || samples.length === 0) return null;

  return (
    <svg
      className="pointer-events-none absolute inset-0 h-full w-full opacity-80"
      viewBox={`0 0 ${samples.length} 100`}
      preserveAspectRatio="none"
    >
      <g>
        {samples.map((s, i) => {
          const h = Math.max(2, s * 100);
          return (
            <rect
              key={i}
              x={i}
              y={50 - h / 2}
              width={1}
              height={h}
              fill="currentColor"
              className="text-white/40"
            />
          );
        })}
      </g>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Zoom controls
// ---------------------------------------------------------------------------

function ZoomControls({
  zoom,
  onChange,
}: {
  zoom: number;
  onChange: (next: number) => void;
}) {
  // UX-05 (#247): the internal ``px/s`` readout moves to a tooltip so
  // the primary dashboard shows only a compact zoom label. The numeric
  // value is still surfaced on hover (and in the data-zoom attribute)
  // for power users and tests.
  return (
    <div
      className="flex items-center gap-1"
      title={`Zoom: ${zoom.toFixed(0)} px/s`}
      data-zoom={zoom.toFixed(0)}
      data-testid="otio-zoom-controls"
    >
      <button
        type="button"
        className="rounded bg-pipeline-bg px-2 py-0.5 hover:bg-pipeline-blue/30"
        onClick={() => onChange(Math.max(6, zoom / 1.4))}
        aria-label="Zoom out"
      >
        −
      </button>
      <span className="w-10 text-center text-[11px] text-pipeline-muted">
        Zoom
      </span>
      <button
        type="button"
        className="rounded bg-pipeline-bg px-2 py-0.5 hover:bg-pipeline-blue/30"
        onClick={() => onChange(Math.min(240, zoom * 1.4))}
        aria-label="Zoom in"
      >
        +
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// InfoDot — tiny pill used to tuck internal status (draft/authoritative,
// duration, SSE live, zoom) behind a tooltip so the primary dashboard
// surface stays plain-English. (UX-05, #247)
// ---------------------------------------------------------------------------

function InfoDot({
  label,
  tooltip,
  tone = "muted",
  testId,
}: {
  label: string;
  tooltip: string;
  tone?: "ok" | "warn" | "muted";
  testId?: string;
}) {
  const toneClass =
    tone === "ok"
      ? "bg-emerald-900/30 text-emerald-200 border-emerald-700/60"
      : tone === "warn"
      ? "bg-amber-900/30 text-amber-200 border-amber-700/60"
      : "bg-pipeline-bg text-pipeline-muted border-pipeline-blue/60";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${toneClass}`}
      title={tooltip}
      data-testid={testId}
    >
      <span aria-hidden="true">ⓘ</span>
      <span>{label}</span>
    </span>
  );
}

function derivedTotalDuration(timeline: OtioTimelineStatus): number {
  let max = 0;
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      max = Math.max(max, slot.start_sec + slot.duration_sec);
    }
  }
  return max;
}
