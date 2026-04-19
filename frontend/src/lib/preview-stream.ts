"use client";

/**
 * UI-06b (#209) — preview_ready / preview_failed SSE hook + pure
 * helpers.  The backend emits these events through ``emit_agui_event``
 * (see ``server/previews/consumers.py``) alongside every other AG-UI
 * pipeline event.  We subscribe through the shared
 * ``subscribeAguiStream`` singleton (``@/lib/agui-stream``) so there
 * is exactly one EventSource open per dashboard regardless of how many
 * hooks / components mount — ``useOtioStream`` and ``usePreviewStream``
 * multiplex over the same connection (ARCH-H invariant preserved).
 *
 * Each boundary surfaces at most one preview at a time.  Previews are
 * keyed by their canonical boundary label (``narration_locked``,
 * ``scene_001_complete``, …) so repeated rebuilds at the same boundary
 * simply replace the entry — no list growth.
 *
 * Stale detection (UI-06b): whenever a ``slot_state`` event arrives we
 * stamp ``lastSlotUpdateAt`` with the wall-clock time.  A preview is
 * considered "stale" iff its ``rendered_at`` timestamp is earlier than
 * ``lastSlotUpdateAt`` for any slot within the preview's boundary
 * scope.  The UI uses this to dim the ▶ marker and show a "rebuild
 * pending" caption instead of pretending the bytes are current.
 */

import { useEffect, useState } from "react";
import type { SlotStateEvent } from "@/lib/types";
import { subscribeAguiStream } from "@/lib/agui-stream";

export type PreviewStatus = "ready" | "failed";

export interface PreviewEntry {
  /** Canonical boundary label, e.g. ``narration_locked`` / ``scene_001_complete``. */
  boundary: string;
  status: PreviewStatus;
  /** Total runtime of the preview in seconds (0 for failures). */
  durationSec: number;
  /** URL the dashboard can fetch — relative to the backend origin. */
  fileUrl: string;
  /** UTC ISO-8601 timestamp the backend emitted. */
  renderedAt: string;
  /** Monotonic millisecond wall-clock for stale comparison. */
  renderedAtMs: number;
  /** Upstream trigger verbatim (may be more detailed than boundary). */
  triggerReason: string;
  /** SHA-256 of the plan when the preview was built. */
  inputHash: string;
  /** Error string on failures, empty on success. */
  error: string;
  /** Rebuild nonce — ticks on every re-emission so UI can re-announce. */
  revision: number;
}

export interface PreviewState {
  /** Boundary → latest preview entry. */
  entries: Record<string, PreviewEntry>;
  /** Wall-clock timestamp (ms) of the last ``slot_state`` event ever seen. */
  lastSlotUpdateAt: number;
  /** Per-scene slot-update timestamps for scoped stale detection. */
  lastUpdateByScene: Record<number, number>;
  /** Most recent slot update on the A1_Narration track (for narration_locked). */
  lastNarrationUpdateAt: number;
}

const INITIAL_STATE: PreviewState = {
  entries: {},
  lastSlotUpdateAt: 0,
  lastUpdateByScene: {},
  lastNarrationUpdateAt: 0,
};

/** Public hook — subscribe to preview_ready / preview_failed. */
export function usePreviewStream(): {
  state: PreviewState;
  connected: boolean;
  error: string | null;
} {
  const [state, setState] = useState<PreviewState>(INITIAL_STATE);
  const [connected, setConnected] = useState(false);
  const [error] = useState<string | null>(null);

  useEffect(() => {
    const unsubscribe = subscribeAguiStream({
      events: ["preview_ready", "preview_failed", "slot_state"],
      onConnected: setConnected,
      onEvent: (evt, e) => {
        // The AG-UI envelope wraps every event as
        //   {"data": <payload>, "timestamp": <server-seconds>}
        // (see ``server/agui.py::emit_agui_event``).  We thread the
        // server timestamp through so stale detection stays inside the
        // server's clock domain, matching ``rendered_at``.
        let serverTsSec: number | undefined;
        let payload: unknown;
        try {
          const env = JSON.parse(e.data) as {
            data: unknown;
            timestamp?: number;
          };
          payload = env.data;
          if (typeof env.timestamp === "number") {
            serverTsSec = env.timestamp;
          }
        } catch {
          return; // malformed envelope
        }

        if (evt === "preview_ready") {
          const entry = parsePreviewReady(payload, serverTsSec);
          if (entry) setState((prev) => upsertPreview(prev, entry));
        } else if (evt === "preview_failed") {
          const entry = parsePreviewFailed(payload, serverTsSec);
          if (entry) setState((prev) => upsertPreview(prev, entry));
        } else if (evt === "slot_state") {
          if (!payload || typeof payload !== "object") return;
          const slot = payload as SlotStateEvent;
          setState((prev) => applySlotUpdate(prev, slot, serverTsSec));
        }
      },
    });
    return unsubscribe;
  }, []);

  return { state, connected, error };
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for direct unit testing)
// ---------------------------------------------------------------------------

/** Parse a ``preview_ready`` payload into an entry.  ``serverTsSec``
 *  is the envelope-level server timestamp (seconds since epoch); when
 *  provided it is preferred over ``rendered_at`` for ``renderedAtMs``
 *  so stale comparisons stay in the server clock domain. */
export function parsePreviewReady(
  raw: unknown,
  serverTsSec?: number,
): PreviewEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const boundary = typeof r.boundary === "string" ? r.boundary : "";
  if (!boundary) return null;
  return {
    boundary,
    status: "ready",
    durationSec:
      typeof r.duration_sec === "number"
        ? r.duration_sec
        : typeof r.total_duration_sec === "number"
          ? r.total_duration_sec
          : 0,
    fileUrl: typeof r.file_url === "string" ? r.file_url : "",
    renderedAt: typeof r.rendered_at === "string" ? r.rendered_at : "",
    renderedAtMs: renderedAtMsFor(r.rendered_at, serverTsSec),
    triggerReason:
      typeof r.trigger_reason === "string" ? r.trigger_reason : boundary,
    inputHash: typeof r.input_hash === "string" ? r.input_hash : "",
    error: "",
    revision: 1,
  };
}

export function parsePreviewFailed(
  raw: unknown,
  serverTsSec?: number,
): PreviewEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const boundary = typeof r.boundary === "string" ? r.boundary : "";
  if (!boundary) return null;
  return {
    boundary,
    status: "failed",
    durationSec: 0,
    fileUrl: typeof r.file_url === "string" ? r.file_url : "",
    renderedAt: typeof r.rendered_at === "string" ? r.rendered_at : "",
    renderedAtMs: renderedAtMsFor(r.rendered_at, serverTsSec),
    triggerReason:
      typeof r.trigger_reason === "string" ? r.trigger_reason : boundary,
    inputHash: typeof r.input_hash === "string" ? r.input_hash : "",
    error: typeof r.error === "string" ? r.error : "preview failed",
    revision: 1,
  };
}

/** Immutable upsert — boundary key replaces prior entry, revision
 *  ticks so callers that memoise on identity rerender. */
export function upsertPreview(
  prev: PreviewState,
  entry: PreviewEntry,
): PreviewState {
  const prior = prev.entries[entry.boundary];
  const next: PreviewEntry = {
    ...entry,
    revision: (prior?.revision ?? 0) + 1,
  };
  return {
    ...prev,
    entries: { ...prev.entries, [entry.boundary]: next },
  };
}

/** Record a slot update.  ``serverTsSec`` is the envelope-level server
 *  timestamp (seconds since epoch) — using it keeps stale detection
 *  inside the same clock domain as ``rendered_at`` timestamps emitted
 *  by ``emit_preview_ready`` / ``emit_preview_failed``.  When the
 *  envelope did not carry a timestamp (legacy event, tests, …) we
 *  fall back to ``Date.now()`` — this preserves the previous
 *  behaviour but triggers the same class of skew warnings. */
export function applySlotUpdate(
  prev: PreviewState,
  evt: SlotStateEvent,
  serverTsSec?: number,
): PreviewState {
  const now =
    typeof serverTsSec === "number" && Number.isFinite(serverTsSec)
      ? serverTsSec * 1000
      : Date.now();
  const sceneUpdates = { ...prev.lastUpdateByScene };
  if (typeof evt.scene_num === "number") {
    sceneUpdates[evt.scene_num] = now;
  }
  const lastNarration =
    evt.track === "A1_Narration" ? now : prev.lastNarrationUpdateAt;
  return {
    ...prev,
    lastSlotUpdateAt: now,
    lastUpdateByScene: sceneUpdates,
    lastNarrationUpdateAt: lastNarration,
  };
}

/** Decide whether a preview is stale relative to subsequent slot
 *  updates.  Pure function — exported for tests.
 *
 *  Scope rules (boundary → what invalidates it):
 *  - ``narration_locked``  → any A1_Narration slot update.
 *  - ``scene_N_complete``  → any slot update touching scene_num=N.
 *  - ``halfway`` / ``final`` / ``act_N_complete`` / anything else →
 *    any slot update (these boundaries cover the whole preview). */
export function isPreviewStale(
  entry: PreviewEntry,
  state: PreviewState,
): boolean {
  if (!entry.renderedAtMs) return false;
  const b = entry.boundary;
  if (b === "narration_locked") {
    return state.lastNarrationUpdateAt > entry.renderedAtMs;
  }
  const m = b.match(/^scene_0*(\d+)_complete$/);
  if (m) {
    const scene = Number(m[1]);
    const ts = state.lastUpdateByScene[scene] ?? 0;
    return ts > entry.renderedAtMs;
  }
  return state.lastSlotUpdateAt > entry.renderedAtMs;
}

/** Return the on-timeline X position (in seconds) for a boundary.
 *
 *  Pure; caller multiplies by zoom to get pixels.  Boundaries that do
 *  not have a natural position (unknown trigger strings) return
 *  ``null`` — the caller decides whether to show an off-timeline chip.
 */
export function boundaryTimeSec(
  boundary: string,
  opts: {
    totalDuration: number;
    /** End-of-scene timestamps keyed by scene_num (start_sec + duration_sec). */
    sceneEndSecByNum: Record<number, number>;
    /** End-of-act timestamps keyed by act_num. */
    actEndSecByNum?: Record<number, number>;
  },
): number | null {
  if (!boundary) return null;
  if (boundary === "narration_locked") return 0;
  if (boundary === "halfway") return opts.totalDuration / 2;
  if (boundary === "final") return opts.totalDuration;
  const sm = boundary.match(/^scene_0*(\d+)_complete$/);
  if (sm) {
    const n = Number(sm[1]);
    const t = opts.sceneEndSecByNum[n];
    return typeof t === "number" ? t : null;
  }
  const am = boundary.match(/^act_0*(\d+)_complete$/);
  if (am) {
    const n = Number(am[1]);
    const t = opts.actEndSecByNum?.[n];
    return typeof t === "number" ? t : null;
  }
  return null;
}

/** Short human label for the ▶ marker tooltip / chip body. */
export function boundaryLabel(boundary: string): string {
  if (boundary === "narration_locked") return "Narration locked";
  if (boundary === "halfway") return "Halfway milestone";
  if (boundary === "final") return "Final cut";
  const sm = boundary.match(/^scene_0*(\d+)_complete$/);
  if (sm) return `Scene ${Number(sm[1])} complete`;
  const am = boundary.match(/^act_0*(\d+)_complete$/);
  if (am) return `Act ${Number(am[1])} complete`;
  return boundary;
}

function parseIsoToMs(iso: unknown): number {
  if (typeof iso !== "string" || !iso) return 0;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

/** Choose a ``renderedAtMs`` that stays inside the server clock domain.
 *
 *  Preference order:
 *    1. ``serverTsSec`` from the SSE envelope (``emit_agui_event``
 *       timestamps each event with ``time.time()``) — this is the same
 *       clock that stamps ``lastSlotUpdateAt`` via ``applySlotUpdate``,
 *       so the stale-vs-fresh comparison is consistent.
 *    2. ``rendered_at`` ISO-8601 string from the payload — also server
 *       clock, but parsed by the browser with ``Date.parse``.  Slot
 *       updates that use ``serverTsSec`` will still compare cleanly
 *       against this value because both originate on the server.
 *    3. ``0`` — ``isPreviewStale`` treats this as "never stale".
 */
export function renderedAtMsFor(
  renderedAt: unknown,
  serverTsSec: number | undefined,
): number {
  if (typeof serverTsSec === "number" && Number.isFinite(serverTsSec)) {
    return serverTsSec * 1000;
  }
  return parseIsoToMs(renderedAt);
}
