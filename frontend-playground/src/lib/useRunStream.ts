"use client";

/**
 * React hook that drives the live status line + interpretation card.
 *
 * Given a ``runId`` the hook opens an ``EventSource`` against the
 * playground's SSE endpoint and folds the resulting stream of
 * structured events into a derived state that the UI renders:
 *
 * - ``events``   — ordered list of all events seen so far; backs
 *                  the raw disclosure log.
 * - ``liveLine`` — the single line the primary feedback surface
 *                  should show *right now*. Prefers a fresh
 *                  ``narrate`` event; otherwise falls back to the
 *                  most recent raw event summary.
 * - ``stall``    — seconds since the last event landed, promoted
 *                  to an object once the grace window elapses.
 *                  Per your instruction: post-initiation silence
 *                  must be loud, so the frontend upgrades the line
 *                  into ``"stalled at <step> — Ns"`` when the bus
 *                  is quiet for too long.
 * - ``terminal`` — run-terminal payload derived from the
 *                  ``run.ok`` / ``run.error`` / ``run.cancelled``
 *                  event. ``interpretation`` is surfaced here.
 *
 * Connection failures (SSE closed unexpectedly, backend unreachable)
 * surface as ``connection = "lost"`` so the UI can render the
 * "lost connection to backend" line without euphemism.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import type { RunEvent, RunState, RunTerminal } from "./types";
import { getRunState, runEventsUrl } from "./api";

const TICK_INTERVAL_MS = 500;

/**
 * Per-step staleness budget in milliseconds. If a step type exceeds
 * its budget with no further events, the UI promotes the status
 * line to a stall indicator. LLM calls take longer than tool
 * dispatches — don't cry wolf on a legitimate 30s probe.
 */
const STEP_BUDGET_MS: Record<string, number> = {
  "run.dispatched": 3_000,
  "probe.start": 45_000,
  "probe.done": 3_000,
  "task.start": 60_000,
  "task.done": 3_000,
  "tool.called": 30_000,
  "tool.returned": 3_000,
  "evaluate.start": 30_000,
  "evaluate.scored": 3_000,
  narrate: 5_000,
};

const TERMINAL_KINDS = new Set([
  "run.ok",
  "run.error",
  "run.cancelled",
]);

const DEFAULT_BUDGET_MS = 5_000;

export type RunConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "lost";

export interface RunStall {
  readonly since_seq: number;
  readonly since_kind: string;
  readonly since_summary: string;
  readonly elapsed_ms: number;
  readonly budget_ms: number;
}

export interface RunStreamState {
  readonly events: readonly RunEvent[];
  readonly liveLine: string | null;
  readonly liveKind: string | null;
  readonly lastEvent: RunEvent | null;
  readonly lastNarration: RunEvent | null;
  readonly stall: RunStall | null;
  readonly terminal: RunTerminal | null;
  readonly connection: RunConnectionState;
  readonly error: string | null;
}

const INITIAL_STATE: RunStreamState = {
  events: [],
  liveLine: null,
  liveKind: null,
  lastEvent: null,
  lastNarration: null,
  stall: null,
  terminal: null,
  connection: "idle",
  error: null,
};

export function useRunStream(runId: string | null): RunStreamState {
  const [events, setEvents] = useState<readonly RunEvent[]>([]);
  const [terminal, setTerminal] = useState<RunTerminal | null>(null);
  const [connection, setConnection] =
    useState<RunConnectionState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const runIdRef = useRef<string | null>(null);

  // Reset when the runId changes so stale events don't bleed
  // into a fresh run's feed.
  useEffect(() => {
    runIdRef.current = runId;
    setEvents([]);
    setTerminal(null);
    setError(null);
    if (!runId) {
      setConnection("idle");
      return;
    }
    setConnection("connecting");
  }, [runId]);

  // SSE subscription.
  useEffect(() => {
    if (!runId) return;
    if (typeof window === "undefined") return;
    const url = runEventsUrl(runId);
    let cancelled = false;
    let source: EventSource | null = null;
    try {
      source = new EventSource(url);
    } catch (err) {
      setConnection("lost");
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    source.onopen = () => {
      if (!cancelled) setConnection("open");
    };
    source.onmessage = (ev: MessageEvent<string>) => {
      if (cancelled) return;
      try {
        const parsed = JSON.parse(ev.data) as RunEvent;
        setEvents((prev) => mergeEvent(prev, parsed));
        if (TERMINAL_KINDS.has(parsed.kind)) {
          // Guard against a slow hydrate landing after the user has
          // already kicked off a second run. ``runIdRef.current`` is
          // updated synchronously by the reset effect the moment
          // ``runId`` changes, so comparing against it inside
          // ``hydrateTerminal`` is enough to drop stale payloads.
          void hydrateTerminal(runId, runIdRef, setTerminal);
        }
      } catch {
        // Malformed payloads are ignored — the raw stream UI will
        // still show the last well-formed event.
      }
    };
    source.onerror = () => {
      if (cancelled) return;
      if (source && source.readyState === EventSource.CLOSED) {
        setConnection("lost");
      }
    };
    return () => {
      cancelled = true;
      if (source) source.close();
    };
  }, [runId]);

  // 500ms tick so the stall counter can advance between events.
  useEffect(() => {
    if (!runId) return;
    const id = window.setInterval(() => {
      setTick((v) => v + 1);
    }, TICK_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [runId]);

  const derived = useMemo<RunStreamState>(() => {
    if (!runId) return INITIAL_STATE;
    const lastEvent = events.length ? events[events.length - 1] : null;
    const lastNarration = findLast(events, (e) => e.kind === "narrate");
    const { liveLine, liveKind } = pickLiveLine(events);
    const stall = computeStall(events, tick);
    const finalLine = stall
      ? `stalled at ${stall.since_kind} — ${Math.round(
          stall.elapsed_ms / 1000
        )}s`
      : liveLine;
    return {
      events,
      liveLine: finalLine,
      liveKind: stall ? stall.since_kind : liveKind,
      lastEvent,
      lastNarration,
      stall,
      terminal,
      connection,
      error,
    };
  }, [runId, events, terminal, connection, error, tick]);

  return derived;
}

function mergeEvent(
  prev: readonly RunEvent[],
  next: RunEvent
): readonly RunEvent[] {
  // SSE replays events that landed before the client connected —
  // dedupe by monotonic seq.
  if (prev.some((e) => e.seq === next.seq)) return prev;
  return [...prev, next].sort((a, b) => a.seq - b.seq);
}

function findLast<T>(arr: readonly T[], pred: (t: T) => boolean): T | null {
  for (let i = arr.length - 1; i >= 0; i -= 1) {
    const value = arr[i];
    if (value !== undefined && pred(value)) return value;
  }
  return null;
}

function pickLiveLine(
  events: readonly RunEvent[]
): { liveLine: string | null; liveKind: string | null } {
  if (events.length === 0) {
    return { liveLine: null, liveKind: null };
  }
  // Prefer the most recent narration as long as nothing fresher has
  // landed since. A narrate event that is older than the tail
  // raw event is stale — surface the raw line instead.
  const lastNarration = findLast(events, (e) => e.kind === "narrate");
  const tail = events[events.length - 1];
  if (tail === undefined) return { liveLine: null, liveKind: null };
  if (lastNarration && lastNarration.seq === tail.seq) {
    return { liveLine: lastNarration.summary, liveKind: "narrate" };
  }
  return { liveLine: tail.summary, liveKind: tail.kind };
}

function computeStall(
  events: readonly RunEvent[],
  _tick: number
): RunStall | null {
  if (events.length === 0) return null;
  // Once any terminal event has landed the run is done — the rail
  // must not keep ticking a stall counter against whatever event
  // happened to arrive last (for example ``interpret``, which is
  // emitted *after* ``run.ok`` as the final post-run narration).
  if (events.some((e) => TERMINAL_KINDS.has(e.kind))) return null;
  const tail = events[events.length - 1];
  if (tail === undefined) return null;
  const budget = STEP_BUDGET_MS[tail.kind] ?? DEFAULT_BUDGET_MS;
  const elapsed = Date.now() - tail.ts * 1000;
  if (elapsed < budget) return null;
  return {
    since_seq: tail.seq,
    since_kind: tail.kind,
    since_summary: tail.summary,
    elapsed_ms: elapsed,
    budget_ms: budget,
  };
}

async function hydrateTerminal(
  runId: string,
  currentRunIdRef: { readonly current: string | null },
  setTerminal: (t: RunTerminal | null) => void
): Promise<void> {
  try {
    const state: RunState = await getRunState(runId);
    // Drop the payload if the user has already moved on to another
    // run. Without this guard a slow ``GET /runs/{id}`` can overwrite
    // the freshly-reset terminal state of the new run, briefly
    // surfacing the previous run's output + interpretation card.
    if (currentRunIdRef.current !== runId) return;
    if (state.terminal) setTerminal(state.terminal);
  } catch {
    // terminal state remains null; the SSE ``run.error`` event is
    // still visible in the feed.
  }
}
