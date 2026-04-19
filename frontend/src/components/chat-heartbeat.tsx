"use client";

/**
 * UX-03 (#245) + DESIGN-01 (#253) — plain-English heartbeat row.
 *
 * When a documentary run is active the chat column shows a single
 * status line so silence never crosses the 60-second "is it crashed?"
 * threshold. Text updates are deliberately throttled to the heartbeat
 * cadence so the chat doesn't churn on every poll tick:
 *
 *   - status transitions (idle -> starting, starting -> running,
 *     running -> done/error, stage change) always publish immediately;
 *   - while the run is steady, the displayed text refreshes once per
 *     {@link HEARTBEAT_INTERVAL_MS} so the elapsed counter nudges
 *     forward at the same cadence the user would expect a narrator
 *     check-in.
 *
 * Data sources:
 *
 *   - ``/dashboard/latest`` snapshot polled every 2 s — cheap and
 *     gives us stage + elapsed time even if the SSE channel is idle.
 *   - ``/agui/stream`` ``run_started`` event via the shared
 *     {@link subscribeAguiStream} singleton — used to reset the
 *     throttle so a fresh run replaces a stale heartbeat immediately.
 *
 * The copy is plain English (DESIGN-01 constraint #2): no
 * ``RUN_STARTED``, ``SSE``, ``slot``, ``directive`` vocabulary.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { subscribeAguiStream } from "@/lib/agui-stream";
import {
  HEARTBEAT_INTERVAL_MS,
  shouldEmitHeartbeat,
  type HeartbeatKind,
} from "@/lib/chat-heartbeat";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// Poll cadence for the dashboard snapshot. The dashboard endpoint is
// cheap (reads an in-memory model) and 2 s gives us fresh-enough data
// without hammering the backend during dev.
const POLL_INTERVAL_MS = 2000;

// Heartbeat staleness threshold: the issue spec calls for "heartbeat
// turn every ≤60 s during active stages", so we warn once the backend
// has stopped updating for longer than the heartbeat interval.
const STALE_AFTER_MS = HEARTBEAT_INTERVAL_MS;

type DashboardSnapshot = {
  run_id?: string | null;
  status?: string;
  active_phase?: string | null;
  elapsed_sec?: number | null;
  last_update_ms?: number | null;
};

const STAGE_LABELS: Record<string, string> = {
  brief: "your brief",
  scenario: "the scenario",
  audio: "narration and music",
  visual_direction: "visual direction",
  production: "clip production",
  assembly: "final assembly",
  completed: "the final cut",
};

export function ChatHeartbeat() {
  const [snap, setSnap] = useState<DashboardSnapshot | null>(null);
  const [lastUpdateMs, setLastUpdateMs] = useState<number | null>(null);
  const [now, setNow] = useState<number>(() => Date.now());
  const [displayed, setDisplayed] = useState<{
    kind: HeartbeatKind;
    message: string;
    tone: "ok" | "warn" | "muted";
    stage: string | null;
    emittedAt: number;
  } | null>(null);
  const pollToken = useRef<number>(0);

  const startPoll = () => {
    const token = pollToken.current + 1;
    pollToken.current = token;
    const poll = async () => {
      while (pollToken.current === token) {
        try {
          const res = await fetch(`${BACKEND_URL}/dashboard/latest`);
          if (res.ok) {
            const data = (await res.json()) as DashboardSnapshot;
            if (pollToken.current === token) {
              setSnap((prev) => {
                // Only bump the update timestamp when a field we care
                // about actually changed — otherwise a motionless
                // backend would look "live" thanks to our own poll.
                //
                // NB: ``elapsed_sec`` is a wall-clock timer on the
                // backend (``round(time.time() - start_time, 1)`` in
                // ``server/dashboard/collector.py``) that ticks up on
                // every snapshot regardless of pipeline progress. We
                // therefore exclude it here: if we included it, the
                // staleness threshold (``STALE_AFTER_MS``) could never
                // be reached while the backend was still responding,
                // even if the pipeline itself was stuck on a phase.
                const changed =
                  !prev ||
                  prev.run_id !== data.run_id ||
                  prev.status !== data.status ||
                  prev.active_phase !== data.active_phase;
                if (changed) setLastUpdateMs(Date.now());
                return data;
              });
            }
          }
        } catch {
          // ignore; next tick retries
        }
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      }
    };
    void poll();
  };

  useEffect(() => {
    startPoll();
    return () => {
      pollToken.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fresh run = reset displayed heartbeat so the user sees an ack
  // immediately rather than waiting out the previous run's throttle
  // window.
  useEffect(() => {
    const unsubscribe = subscribeAguiStream({
      events: ["run_started"],
      onEvent: () => {
        setDisplayed(null);
        setLastUpdateMs(Date.now());
      },
    });
    return unsubscribe;
  }, []);

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  const candidate = useMemo(() => {
    if (!snap || !snap.run_id) {
      return {
        kind: "idle" as HeartbeatKind,
        message: "",
        tone: "muted" as const,
        stage: null as string | null,
      };
    }
    if (snap.status === "completed") {
      return {
        kind: "done" as HeartbeatKind,
        message: "Finished — your film is ready.",
        tone: "ok" as const,
        stage: "completed",
      };
    }
    if (snap.status === "error") {
      return {
        kind: "error" as HeartbeatKind,
        message:
          "Something went wrong. I'll keep you posted in chat with what I'm trying.",
        tone: "warn" as const,
        stage: "error",
      };
    }
    const phase = snap.active_phase;
    if (!phase) {
      return {
        kind: "starting" as HeartbeatKind,
        message: "Got it — I'm getting set up.",
        tone: "muted" as const,
        stage: null,
      };
    }
    const label = STAGE_LABELS[phase] ?? phase;
    const elapsed = snap.elapsed_sec ?? 0;
    const stale =
      lastUpdateMs !== null && now - lastUpdateMs > STALE_AFTER_MS;
    const base = `Still working on ${label} · ${formatElapsed(elapsed)} in.`;
    const msg = stale
      ? `${base} Nothing new from the pipeline in ${formatAgo(
          now - (lastUpdateMs ?? now),
        )} — I'll ping again the moment something changes.`
      : base;
    return {
      kind: "running" as HeartbeatKind,
      message: msg,
      tone: stale ? ("warn" as const) : ("muted" as const),
      stage: phase,
    };
  }, [snap, lastUpdateMs, now]);

  // Throttle: only publish a new heartbeat line on a state transition
  // or after HEARTBEAT_INTERVAL_MS has elapsed since the last emit.
  useEffect(() => {
    const prev = displayed
      ? { kind: displayed.kind, stage: displayed.stage, emittedAt: displayed.emittedAt }
      : null;
    if (
      shouldEmitHeartbeat({
        prev,
        next: { kind: candidate.kind, stage: candidate.stage },
        now,
        minIntervalMs: HEARTBEAT_INTERVAL_MS,
      })
    ) {
      setDisplayed({
        kind: candidate.kind,
        message: candidate.message,
        tone: candidate.tone,
        stage: candidate.stage,
        emittedAt: now,
      });
    }
  }, [candidate, displayed, now]);

  if (!displayed || displayed.kind === "idle") return null;

  const toneClass =
    displayed.tone === "ok"
      ? "bg-emerald-900/40 text-emerald-100 border-emerald-700/60"
      : displayed.tone === "warn"
      ? "bg-amber-900/30 text-amber-100 border-amber-700/60"
      : "bg-pipeline-card text-pipeline-muted border-pipeline-blue/60";

  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex items-center gap-2 border-t px-4 py-2 text-xs ${toneClass}`}
      data-testid="chat-heartbeat"
      data-kind={displayed.kind}
    >
      <span aria-hidden="true">•</span>
      <span className="flex-1">{displayed.message}</span>
    </div>
  );
}

function formatElapsed(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "0s";
  // Round to whole seconds first so fractional values near a minute
  // boundary (e.g. 119.5) do not produce nonsensical strings like
  // "1m 60s". Devin Review #271.
  const total = Math.round(sec);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total - m * 60;
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

function formatAgo(ms: number): string {
  const sec = Math.max(0, Math.round(ms / 1000));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}
