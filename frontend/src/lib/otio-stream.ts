"use client";

import { useEffect, useRef, useState } from "react";
import type {
  OtioTimelineStatus,
  SlotStateEvent,
  SlotStatus,
} from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/**
 * SSE-driven hook for the OTIO centrepiece timeline.
 *
 * This hook owns the single EventSource used by the dashboard.  It:
 *
 * 1. Fetches the initial `/agui/otio/state` snapshot (and uses it as the
 *    fallback if the browser cannot open an SSE connection).
 * 2. Subscribes to `/agui/stream` and drives all subsequent updates off
 *    events — `otio_snapshot`, `slot_state`, `otio_authoritative`.  There
 *    is no `setInterval` anywhere in the timeline — the OTIO is always
 *    up to date because the orchestrator emits events onto the AG-UI
 *    bus when slot state transitions (ARCH-H1) or the OTIO crystallises
 *    (ARCH-H2).
 * 3. Batches slot-state updates inside a single render by merging them
 *    into a fresh `OtioTimelineStatus` on every event.  This keeps the
 *    frontend stateless w.r.t. the "last update" — the server is the
 *    source of truth.
 */
export function useOtioStream(): {
  timeline: OtioTimelineStatus | null;
  error: string | null;
  connected: boolean;
} {
  const [timeline, setTimeline] = useState<OtioTimelineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const hasSnapshotRef = useRef(false);

  useEffect(() => {
    let cancelled = false;

    // Bootstrap: grab the snapshot even if SSE is slow / blocked.
    // Only apply if the SSE stream hasn't already delivered a snapshot,
    // otherwise a late-arriving HTTP response can clobber slot_state
    // updates that have already been merged into the timeline.
    fetch(`${BACKEND_URL}/agui/otio/state`)
      .then((r) => r.json())
      .then((data: OtioTimelineStatus) => {
        if (!cancelled && !hasSnapshotRef.current) setTimeline(data);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });

    const es = new EventSource(`${BACKEND_URL}/agui/stream`);
    esRef.current = es;

    es.addEventListener("open", () => {
      setConnected(true);
      setError(null);
    });

    es.addEventListener("otio_snapshot", (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data) as OtioTimelineStatus;
        hasSnapshotRef.current = true;
        setTimeline(data);
      } catch {
        /* ignore malformed snapshot */
      }
    });

    es.addEventListener("slot_state", (e: MessageEvent) => {
      try {
        const env = JSON.parse(e.data) as { data: SlotStateEvent };
        const slot = env.data;
        setTimeline((prev) => (prev ? applySlotState(prev, slot) : prev));
      } catch {
        /* ignore malformed */
      }
    });

    es.addEventListener("otio_authoritative", (e: MessageEvent) => {
      try {
        JSON.parse(e.data);
      } catch {
        /* ok, payload may be empty */
      }
      setTimeline((prev) => (prev ? { ...prev, state: "authoritative" } : prev));
    });

    // artifact_update also moves slots; mirror through slot_state when
    // it already carried scene/phrase (the backend always does, so this
    // branch is a safety net for older pipelines).
    es.addEventListener("artifact_update", (e: MessageEvent) => {
      try {
        JSON.parse(e.data);
      } catch {
        /* ignore */
      }
    });

    es.addEventListener("error", () => {
      setConnected(false);
    });

    return () => {
      cancelled = true;
      es.close();
      esRef.current = null;
    };
  }, []);

  return { timeline, error, connected };
}

function applySlotState(
  prev: OtioTimelineStatus,
  evt: SlotStateEvent,
): OtioTimelineStatus {
  let touched = false;
  const tracks = prev.tracks.map((t) => {
    if (t.name !== evt.track) return t;
    const slots = t.slots.map((s) => {
      if (s.slot_id !== evt.slot_id) return s;
      touched = true;
      return {
        ...s,
        status: evt.status as SlotStatus,
        preview_url: evt.preview_url || s.preview_url,
      };
    });
    return { ...t, slots };
  });
  if (!touched) return prev;
  return { ...prev, tracks };
}
