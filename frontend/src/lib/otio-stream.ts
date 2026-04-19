"use client";

import { useEffect, useRef, useState } from "react";
import type {
  ApprovalGateEvent,
  OtioTimelineStatus,
  SlotStateEvent,
  SlotStatus,
} from "@/lib/types";
import { subscribeAguiStream } from "@/lib/agui-stream";

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
  openGates: ApprovalGateEvent[];
} {
  const [timeline, setTimeline] = useState<OtioTimelineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [openGates, setOpenGates] = useState<ApprovalGateEvent[]>([]);
  const hasSnapshotRef = useRef(false);
  // Buffer for events that arrive before the initial snapshot lands.
  // When ``OtioTimeline`` remounts while another hook (e.g. the
  // preview chips) is already keeping ``/agui/stream`` open, the
  // server does *not* send a fresh ``otio_snapshot`` — it's only
  // emitted when the EventSource itself opens.  Until the HTTP
  // ``/agui/otio/state`` fetch resolves, ``timeline`` is ``null`` and
  // the reducers below would silently drop updates.  Queue them
  // instead and replay once a snapshot is available.
  const pendingRef = useRef<
    Array<{ kind: "slot"; slot: SlotStateEvent } | { kind: "authoritative" }>
  >([]);

  useEffect(() => {
    let cancelled = false;

    function applyOrQueue(
      reducer: (t: OtioTimelineStatus) => OtioTimelineStatus,
      queued:
        | { kind: "slot"; slot: SlotStateEvent }
        | { kind: "authoritative" },
    ) {
      setTimeline((prev) => {
        if (prev) return reducer(prev);
        pendingRef.current.push(queued);
        return prev;
      });
    }

    function flushPending(base: OtioTimelineStatus): OtioTimelineStatus {
      let next = base;
      for (const pending of pendingRef.current) {
        if (pending.kind === "slot") {
          next = applySlotState(next, pending.slot);
        } else if (pending.kind === "authoritative") {
          next = { ...next, state: "authoritative" };
        }
      }
      pendingRef.current = [];
      return next;
    }

    // Bootstrap: grab the snapshot even if SSE is slow / blocked.
    // Only apply if the SSE stream hasn't already delivered a snapshot,
    // otherwise a late-arriving HTTP response can clobber slot_state
    // updates that have already been merged into the timeline.
    fetch(`${BACKEND_URL}/agui/otio/state`)
      .then((r) => r.json())
      .then((data: OtioTimelineStatus) => {
        if (cancelled || hasSnapshotRef.current) return;
        setTimeline(flushPending(data));
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });

    const unsubscribe = subscribeAguiStream({
      events: [
        "otio_snapshot",
        "slot_state",
        "otio_authoritative",
        "artifact_update",
        // UI-03a (#198): inline approval card driver. Every
        // wait_for_approval entry emits approval_gate_opened; the paired
        // approval_gate_closed fires when the gate flips via
        // /agui/approve OR via a stage-scoped directive (UI-03c, #200).
        // The timeline listens here so the card mounts/unmounts on the
        // unified AG-UI event bus -- no polling.
        "approval_gate_opened",
        "approval_gate_closed",
      ],
      onConnected: (c) => {
        setConnected(c);
        if (c) setError(null);
      },
      onEvent: (evt, e) => {
        if (evt === "otio_snapshot") {
          try {
            const data = JSON.parse(e.data) as OtioTimelineStatus;
            hasSnapshotRef.current = true;
            setTimeline(flushPending(data));
          } catch {
            /* ignore malformed snapshot */
          }
        } else if (evt === "slot_state") {
          try {
            const env = JSON.parse(e.data) as { data: SlotStateEvent };
            const slot = env.data;
            applyOrQueue(
              (prev) => applySlotState(prev, slot),
              { kind: "slot", slot },
            );
          } catch {
            /* ignore malformed */
          }
        } else if (evt === "otio_authoritative") {
          try {
            JSON.parse(e.data);
          } catch {
            /* ok, payload may be empty */
          }
          applyOrQueue(
            (prev) => ({ ...prev, state: "authoritative" }),
            { kind: "authoritative" },
          );
        } else if (evt === "artifact_update") {
          // artifact_update also moves slots; mirror through
          // slot_state when it already carried scene/phrase (the
          // backend always does, so this branch is a safety net for
          // older pipelines).
          try {
            JSON.parse(e.data);
          } catch {
            /* ignore */
          }
        } else if (evt === "approval_gate_opened") {
          try {
            const env = JSON.parse(e.data) as { data: ApprovalGateEvent };
            const ev = env.data;
            if (!ev || !ev.stage) return;
            setOpenGates((prev) => {
              if (prev.some((g) => g.stage === ev.stage)) return prev;
              return [...prev, ev];
            });
          } catch {
            /* ignore malformed */
          }
        } else if (evt === "approval_gate_closed") {
          try {
            const env = JSON.parse(e.data) as { data: ApprovalGateEvent };
            const ev = env.data;
            if (!ev || !ev.stage) return;
            setOpenGates((prev) => prev.filter((g) => g.stage !== ev.stage));
          } catch {
            /* ignore malformed */
          }
        }
      },
    });

    return () => {
      cancelled = true;
      pendingRef.current = [];
      unsubscribe();
    };
  }, []);

  return { timeline, error, connected, openGates };
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
