"use client";

/**
 * ARCH-H4 (#159) — halt controls.
 *
 * After DESIGN-06 (#258) moved the natural-language directive input
 * into the new `<IntentBar />`, this component is reduced to the halt
 * surface: the amber "Pause production" button, the halt-engaged
 * indicator, and the halt-resume card exposing the resume / rewind /
 * exit paths (``POST /api/halt/release``). DESIGN-09 will redesign the
 * pause experience itself; this PR deliberately leaves the button and
 * its behaviour unchanged.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export type SlotContext = {
  scope?: string;
  scope_ref?: string | null;
  scene_id?: string;
  scene_num?: number;
  voice_block_id?: string;
  clip_id?: string;
  stage?: string;
  [k: string]: unknown;
};

export type DirectiveRecord = {
  revision: number;
  scope: string;
  scope_ref: string | null;
  polarity: string;
  subject: string;
  content: string;
};

type HaltState = {
  halt_requested: boolean;
  halted_at_stage: string | null;
  halt_reviewer: string | null;
  halt_reason: string | null;
  halt_timestamp: number | null;
  halt_last_checkpoint?: string | null;
  halt_exit_requested?: boolean;
};

type HaltReleaseMode = "resume" | "rewind" | "exit";

type Toast = {
  id: number;
  kind: "success" | "error" | "halt";
  message: string;
  detail?: string;
};

export type DashboardInterventionProps = {
  /**
   * Retained for backward compatibility with callers that still pass an
   * explicit slot context. The halt surface itself ignores it — the
   * intent bar (DESIGN-06) owns the directive path.
   */
  selectedSlot?: SlotContext | null;
  /**
   * Retained for backward compatibility; never fires from the halt
   * surface. Directive submission moved to `<IntentBar />`.
   */
  onRecordsAppended?: (records: DirectiveRecord[]) => void;
};

export function DashboardIntervention(_props: DashboardInterventionProps = {}) {
  const [haltSubmitting, setHaltSubmitting] = useState(false);
  const [haltState, setHaltState] = useState<HaltState | null>(null);
  const [pipelineRunning, setPipelineRunning] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastIdRef = useRef(0);

  const pushToast = useCallback((toast: Omit<Toast, "id">) => {
    toastIdRef.current += 1;
    const id = toastIdRef.current;
    setToasts((prev) => [...prev, { id, ...toast }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 6000);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        try {
          const res = await fetch(`${BACKEND_URL}/api/halt_state`);
          if (res.ok) {
            const data: HaltState = await res.json();
            if (!cancelled) setHaltState(data);
          }
        } catch {
          // ignore network errors; polling resumes
        }
        try {
          const res2 = await fetch(`${BACKEND_URL}/dashboard/latest`);
          if (res2.ok) {
            const snap = (await res2.json()) as {
              status?: string;
              active_phase?: string | null;
              run_id?: string | null;
            };
            if (!cancelled) {
              const running =
                !!snap.run_id &&
                snap.status !== "idle" &&
                snap.status !== "completed" &&
                snap.status !== "error" &&
                !!snap.active_phase;
              setPipelineRunning(running);
            }
          }
        } catch {
          // ignore
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, []);

  const submitHalt = useCallback(async () => {
    setHaltSubmitting(true);
    try {
      const res = await fetch(`${BACKEND_URL}/api/halt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer: "dashboard-user" }),
      });
      if (!res.ok) {
        const text = await res.text();
        pushToast({
          kind: "error",
          message: "Halt failed",
          detail: text.slice(0, 200),
        });
        return;
      }
      const data: HaltState = await res.json();
      setHaltState(data);
      pushToast({
        kind: "halt",
        message: "Halt requested — pipeline will pause at next safe checkpoint",
      });
    } catch (err) {
      pushToast({
        kind: "error",
        message: "Halt request failed",
        detail: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setHaltSubmitting(false);
    }
  }, [pushToast]);

  const releaseHalt = useCallback(
    async (mode: HaltReleaseMode = "resume") => {
      try {
        const res = await fetch(`${BACKEND_URL}/api/halt/release`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        });
        if (!res.ok) {
          const text = await res.text();
          pushToast({
            kind: "error",
            message: `Halt release failed (${mode})`,
            detail: text.slice(0, 200),
          });
          return;
        }
        const data: HaltState = await res.json();
        setHaltState(data);
        const message =
          mode === "rewind"
            ? "Rewind queued — rolling back to last checkpoint"
            : mode === "exit"
            ? "Exit requested — pipeline will shut down at next checkpoint"
            : "Halt released — pipeline resuming";
        pushToast({ kind: "success", message });
      } catch (err) {
        pushToast({
          kind: "error",
          message: `Halt release failed (${mode})`,
          detail: err instanceof Error ? err.message : String(err),
        });
      }
    },
    [pushToast],
  );

  const haltEngaged = haltState?.halt_requested === true;

  return (
    <div className="flex flex-col gap-2 border-b border-pipeline-blue bg-pipeline-card px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col">
          <span className="text-xs font-semibold uppercase tracking-wide text-pipeline-muted">
            Your controls
          </span>
          {haltEngaged ? (
            <span
              className="text-sm text-red-300"
              data-testid="halt-indicator"
            >
              Halt engaged
              {haltState?.halted_at_stage
                ? ` — paused at ${haltState.halted_at_stage}`
                : " — pausing at next safe checkpoint"}
            </span>
          ) : (
            <span className="text-sm text-pipeline-muted">
              <span className="text-pipeline-text">
                Use the bar at the bottom to steer the film.
              </span>
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* UX-06 (#248): only show Pause when the pipeline is actively
            * running. Red is reserved for real problems, so we use an
            * amber/grey treatment. DESIGN-09 will redesign this surface. */}
          {!haltEngaged && pipelineRunning && (
            <button
              type="button"
              onClick={submitHalt}
              disabled={haltSubmitting}
              className="rounded border border-amber-500/70 bg-amber-900/20 px-3 py-2 text-sm font-semibold text-amber-100 hover:bg-amber-900/40 disabled:opacity-60"
              data-testid="halt-button"
              title="Pause the pipeline at the next safe checkpoint"
            >
              {haltSubmitting ? "Pausing…" : "Pause production"}
            </button>
          )}
        </div>
      </div>
      {haltEngaged && (
        <HaltResumeCard haltState={haltState} onRelease={releaseHalt} />
      )}
      {toasts.length > 0 && (
        <ul
          className="flex flex-col gap-1 pt-1"
          data-testid="directive-toasts"
          aria-live="polite"
        >
          {toasts.map((t) => (
            <li
              key={t.id}
              className={`rounded border px-3 py-2 text-xs ${
                t.kind === "success"
                  ? "border-emerald-500/60 bg-emerald-900/30 text-emerald-100"
                  : t.kind === "halt"
                  ? "border-red-500/60 bg-red-900/30 text-red-100"
                  : "border-amber-500/60 bg-amber-900/30 text-amber-100"
              }`}
            >
              <div className="font-semibold">{t.message}</div>
              {t.detail && (
                <pre className="mt-1 whitespace-pre-wrap text-[11px] opacity-90">
                  {t.detail}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function HaltResumeCard({
  haltState,
  onRelease,
}: {
  haltState: HaltState | null;
  onRelease: (mode: HaltReleaseMode) => void | Promise<void>;
}) {
  const stage = haltState?.halted_at_stage ?? "next safe checkpoint";
  const checkpoint = haltState?.halt_last_checkpoint ?? null;
  const reason = haltState?.halt_reason ?? null;
  const rewindLabel = checkpoint
    ? `Rewind to ${checkpoint}`
    : "Rewind (no checkpoint)";
  return (
    <div
      className="flex flex-col gap-2 rounded border border-amber-500/70 bg-amber-900/20 px-3 py-2 text-amber-100"
      data-testid="halt-resume-card"
      role="region"
      aria-label="Halt resume options"
    >
      <div className="flex flex-col">
        <span className="text-sm font-semibold">Paused at {stage}.</span>
        <span className="text-xs text-amber-200/90">
          Last safe checkpoint:{" "}
          <span className="font-mono">{checkpoint ?? "none"}</span>
          {reason ? ` · reason: ${reason}` : ""}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void onRelease("resume")}
          className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
          data-testid="halt-resume-button"
        >
          Resume from here
        </button>
        <button
          type="button"
          onClick={() => void onRelease("rewind")}
          disabled={!checkpoint}
          className="rounded bg-amber-500 px-3 py-1.5 text-xs font-semibold text-amber-950 hover:bg-amber-400 disabled:cursor-not-allowed disabled:opacity-50"
          data-testid="halt-rewind-button"
          title={
            checkpoint
              ? `Roll back to ${checkpoint} and retry`
              : "No checkpoint recorded yet"
          }
        >
          {rewindLabel}
        </button>
        <button
          type="button"
          onClick={() => void onRelease("exit")}
          className="rounded bg-red-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-600"
          data-testid="halt-exit-button"
          title="Stop the pipeline after current in-flight work completes"
        >
          Exit run
        </button>
      </div>
    </div>
  );
}

/**
 * Retained for backward compatibility (bridge tests, `<SlotChip />` and
 * any future non-OTIO callers). The halt surface no longer uses it —
 * directive scoping moved to `<IntentBar />`.
 */
export function deriveSlotContext(
  slotId: string,
  slot: { scene_num?: number } | null,
): SlotContext {
  const ctx: SlotContext = {
    scope: "element",
    scope_ref: slotId,
    clip_id: slotId,
  };
  if (slot && typeof slot.scene_num === "number") {
    ctx.scene_num = slot.scene_num;
  }
  return ctx;
}
