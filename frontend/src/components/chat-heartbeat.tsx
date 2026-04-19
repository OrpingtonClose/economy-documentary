"use client";

/**
 * UX-03 (#245) — immediate acknowledgement + ≤60s heartbeat.
 *
 * The primary complaint on UX-03 was that after a user submits a brief
 * the chat log can go silent for minutes while the scenario director
 * warms up, which looks indistinguishable from a crash. The real fix
 * lives in ``agents/chat_narrator.py`` (server side) and will emit an
 * ack turn on brief submit plus a heartbeat turn every ≤60 s during
 * active stages.
 *
 * This component is the *frontend-only* leg of that fix — per UX-02/03
 * hard constraints we cannot touch the backend in this PR. It renders
 * an always-mounted status row below the CopilotKit chat that polls the
 * dashboard snapshot and surfaces one of three affordances:
 *
 *   - ``idle``      — nothing to say; component collapses to null.
 *   - ``starting``  — ack: "Got it — warming up the scenario director…"
 *   - ``running``   — heartbeat: "Still working on <Stage> · <elapsed>s"
 *                     with a ≤60 s staleness warning once the backend
 *                     stops updating.
 *
 * When the server-side narrator ack/heartbeat turns do arrive through
 * the SSE stream the heartbeat row stays quiet (it only drives off the
 * dashboard snapshot) so the two signals coexist rather than fight.
 */

import { useEffect, useMemo, useState } from "react";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// Poll cadence for the dashboard snapshot. The dashboard endpoint is
// cheap (reads an in-memory model) and 2 s gives us fresh-enough data
// without hammering the backend during dev.
const POLL_INTERVAL_MS = 2000;

// Heartbeat staleness threshold: the issue spec calls for "heartbeat
// turn every ≤60 s during active stages", so we start warning after
// 60 s without a dashboard-snapshot update.
const STALE_AFTER_MS = 60_000;

type DashboardSnapshot = {
  run_id?: string | null;
  status?: string;
  active_phase?: string | null;
  elapsed_sec?: number | null;
  last_update_ms?: number | null;
};

const STAGE_LABELS: Record<string, string> = {
  brief: "the brief",
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

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        try {
          const res = await fetch(`${BACKEND_URL}/dashboard/latest`);
          if (res.ok) {
            const data = (await res.json()) as DashboardSnapshot;
            if (!cancelled) {
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
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  const { kind, message, tone } = useMemo(() => {
    if (!snap || !snap.run_id) {
      return { kind: "idle" as const, message: "", tone: "muted" as const };
    }
    if (snap.status === "completed") {
      return {
        kind: "done" as const,
        message: "Finished — your film is ready.",
        tone: "ok" as const,
      };
    }
    if (snap.status === "error") {
      return {
        kind: "error" as const,
        message: "Pipeline hit a problem — check the Advanced tab for details.",
        tone: "warn" as const,
      };
    }
    const phase = snap.active_phase;
    if (!phase) {
      return {
        kind: "starting" as const,
        message: "Got it — warming up the scenario director…",
        tone: "muted" as const,
      };
    }
    const label = STAGE_LABELS[phase] ?? phase;
    const elapsed = snap.elapsed_sec ?? 0;
    const stale =
      lastUpdateMs !== null && now - lastUpdateMs > STALE_AFTER_MS;
    return {
      kind: "running" as const,
      message: stale
        ? `Still working on ${label} · ${formatElapsed(elapsed)} elapsed · last update ${formatAgo(
            now - (lastUpdateMs ?? now),
          )} ago`
        : `Still working on ${label} · ${formatElapsed(elapsed)} elapsed`,
      tone: stale ? "warn" : "muted",
    };
  }, [snap, lastUpdateMs, now]);

  if (kind === "idle") return null;

  const toneClass =
    tone === "ok"
      ? "bg-emerald-900/40 text-emerald-100 border-emerald-700/60"
      : tone === "warn"
      ? "bg-amber-900/30 text-amber-100 border-amber-700/60"
      : "bg-pipeline-card text-pipeline-muted border-pipeline-blue/60";

  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex items-center gap-2 border-t px-4 py-2 text-xs ${toneClass}`}
      data-testid="chat-heartbeat"
      data-kind={kind}
    >
      <span aria-hidden="true">•</span>
      <span className="flex-1">{message}</span>
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
