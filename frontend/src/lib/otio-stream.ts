"use client";

import { useEffect, useRef, useState } from "react";
import type {
  ApprovalGateEvent,
  DirectiveAppliedEvent,
  DriftState,
  OtioTimelineStatus,
  ReManifestationProgressEvent,
  SlotStateEvent,
  SlotStatus,
} from "@/lib/types";
import { subscribeAguiStream } from "@/lib/agui-stream";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

const EMPTY_DRIFT: DriftState = {
  slotIds: new Set(),
  sceneNums: new Set(),
  slotStages: {},
};

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
 * 4. UI-05b: tracks transient drift state derived from
 *    `directive_applied` + `re_manifestation_progress` events.  The
 *    drift set clears automatically as each step's terminal phase
 *    arrives; the state is never persisted.
 */
export function useOtioStream(): {
  timeline: OtioTimelineStatus | null;
  error: string | null;
  connected: boolean;
  openGates: ApprovalGateEvent[];
  drift: DriftState;
} {
  const [timeline, setTimeline] = useState<OtioTimelineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [openGates, setOpenGates] = useState<ApprovalGateEvent[]>([]);
  const [drift, setDrift] = useState<DriftState>(EMPTY_DRIFT);
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
        // UI-03a (#198): inline approval card driver.
        "approval_gate_opened",
        "approval_gate_closed",
        // UI-05b: drift badges on the timeline.
        "directive_applied",
        "re_manifestation_progress",
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
        } else if (evt === "directive_applied") {
          try {
            const env = JSON.parse(e.data) as { data: DirectiveAppliedEvent };
            const payload = env.data;
            setDrift((prev) => seedDrift(prev, payload));
          } catch {
            /* ignore */
          }
        } else if (evt === "re_manifestation_progress") {
          try {
            const env = JSON.parse(e.data) as {
              data: ReManifestationProgressEvent;
            };
            const payload = env.data;
            setDrift((prev) => applyProgress(prev, payload));
          } catch {
            /* ignore */
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

  return { timeline, error, connected, openGates, drift };
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

function seedDrift(prev: DriftState, payload: DirectiveAppliedEvent): DriftState {
  const slotIds = new Set(prev.slotIds);
  const sceneNums = new Set(prev.sceneNums);
  const slotStages = { ...prev.slotStages };
  for (const id of payload.drifted_slot_ids ?? []) {
    slotIds.add(id);
    if (!slotStages[id]) {
      slotStages[id] = "re-manifesting";
    }
  }
  for (const n of payload.drifted_scene_nums ?? []) {
    sceneNums.add(n);
  }
  return { slotIds, sceneNums, slotStages };
}

function applyProgress(
  prev: DriftState,
  payload: ReManifestationProgressEvent,
): DriftState {
  const slotIds = new Set(prev.slotIds);
  const sceneNums = new Set(prev.sceneNums);
  const slotStages = { ...prev.slotStages };
  const ids = payload.slot_ids ?? [];
  const stageLabel = stageLabelFor(payload);

  const sceneKnown =
    payload.scene_num !== null && payload.scene_num !== undefined;
  // Scene-wide steps (no clip id) paint every slot in the scene via
  // sceneNums; slot-specific steps paint only their own triples.  We
  // mirror that asymmetry on teardown so the two sets can each clear
  // independently as their terminal events arrive.
  const sceneWide = ids.length === 0 && sceneKnown;

  if (payload.phase === "start") {
    for (const id of ids) {
      slotIds.add(id);
      slotStages[id] = stageLabel;
    }
    if (sceneWide) {
      sceneNums.add(payload.scene_num as number);
    }
  } else if (
    payload.phase === "complete" ||
    payload.phase === "failed"
  ) {
    for (const id of ids) {
      slotIds.delete(id);
      delete slotStages[id];
    }
    if (sceneWide) {
      sceneNums.delete(payload.scene_num as number);
    }
  }
  return { slotIds, sceneNums, slotStages };
}

function stageLabelFor(payload: ReManifestationProgressEvent): string {
  const stage = payload.stage_name
    ? payload.stage_name.replace(/_/g, " ")
    : "";
  return `re-manifesting ${stage}`.trim();
}
