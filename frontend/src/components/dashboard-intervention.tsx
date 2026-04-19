"use client";

/**
 * ARCH-H4 (#159): dashboard intervention controls.
 *
 * Renders the two proactive-L4 entry points alongside the existing
 * reactive approval-gate flow:
 *
 * 1. **Halt-anywhere button** -- the red "Pause production" button in
 *    the top-right of the dashboard shell. Posts to `POST /api/halt`,
 *    which engages the disk-backed halt flag. The approval-gate poll
 *    loop observes the flag and pauses the pipeline at the next safe
 *    checkpoint; workers finish their in-flight tool call first.
 *    Submitting also focuses the directive input so the reviewer can
 *    leave a note about *why* the halt fired.
 *
 * 2. **Directive input** -- always-visible text field that posts to
 *    `POST /api/directive`. The Preference Interpreter (ARCH-A2) parses
 *    the free-form text, the Preference Ledger (ARCH-A1) appends the
 *    resulting records, and the consistency checker (ARCH-A5) drifts
 *    the affected stages so surgical re-manifestation (A6/B3) re-runs.
 *    A selected slot (from the H3 slot detail panel) is passed as
 *    ``slot_context`` so the directive is scoped to that slot by default.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOtioStream } from "@/lib/otio-stream";
import type { OtioSlot, OtioTimelineStatus } from "@/lib/types";
import {
  clearSelection,
  useSelection,
} from "@/lib/stores/selection";
import { CostPreviewDialog } from "@/components/cost-preview-dialog";
import { HaltPauseButton } from "@/components/halt-pause-button";
import { RewindDropdown } from "@/components/rewind-dropdown";
import {
  fetchDirectiveEstimate,
  type CostEstimate,
} from "@/lib/cost-estimate";

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

type DirectiveResponse = {
  status: string;
  l4_event_id: string;
  record_ids: number[];
  records: DirectiveRecord[];
  re_manifestation_plans: Array<{
    plan_id: string | null;
    stage_name: string | null;
    step_count: number;
    error: string | null;
  }>;
  scope_hint: { scope: string; scope_ref: string | null } | null;
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

function summariseRecord(r: DirectiveRecord): string {
  const scope =
    r.scope_ref !== null && r.scope_ref !== undefined
      ? `${r.scope}:${r.scope_ref}`
      : r.scope;
  return `${r.polarity} ${r.subject} @ ${scope}`;
}

export type DashboardInterventionProps = {
  /**
   * Override the selection source. When omitted (the default), the
   * intervention bar subscribes to the UI-02a shared selection store
   * and derives `slot_context` from the corresponding OTIO slot. The
   * prop is retained so existing tests (and any future non-OTIO
   * callers) can still inject an explicit context.
   */
  selectedSlot?: SlotContext | null;

  /**
   * Fires after each successful directive submission. The parent ledger
   * history panel uses this to highlight the newly-added entries.
   */
  onRecordsAppended?: (records: DirectiveRecord[]) => void;
};

export function DashboardIntervention({
  selectedSlot: selectedSlotOverride,
  onRecordsAppended,
}: DashboardInterventionProps) {
  const { selectedSlotId } = useSelection();
  const { timeline } = useOtioStream();
  const selectedSlot = useMemo<SlotContext | null>(() => {
    if (selectedSlotOverride !== undefined) return selectedSlotOverride;
    if (!selectedSlotId) return null;
    const slot = timeline ? findSlotInSnapshot(timeline, selectedSlotId) : null;
    return deriveSlotContext(selectedSlotId, slot);
  }, [selectedSlotOverride, selectedSlotId, timeline]);
  // Only resolve meta from the store when there is no prop override.
  // If a parent is driving `selectedSlot` explicitly, the store's id is
  // irrelevant — the chip label and visibility must follow the override
  // so the UI agrees with the `slot_context` we actually POST.
  const selectedSlotMeta = useMemo(
    () =>
      selectedSlotOverride === undefined && selectedSlotId && timeline
        ? findSlotInSnapshot(timeline, selectedSlotId)
        : null,
    [selectedSlotOverride, selectedSlotId, timeline],
  );
  const [directive, setDirective] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [haltSubmitting, setHaltSubmitting] = useState(false);
  const [haltState, setHaltState] = useState<HaltState | null>(null);
  const [pipelineRunning, setPipelineRunning] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  // DESIGN-07 (#259): cost-preview state for the directive-submit path.
  // The dialog opens as soon as the reviewer hits Enter/Send; the
  // actual POST only fires after they click "Continue".
  const [directiveDialogOpen, setDirectiveDialogOpen] = useState(false);
  const [directiveEstimate, setDirectiveEstimate] =
    useState<CostEstimate | null>(null);
  const [directiveEstimateLoading, setDirectiveEstimateLoading] =
    useState(false);
  const pendingDirectiveRef = useRef<{
    text: string;
    slot_context: SlotContext | null;
  } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const toastIdRef = useRef(0);

  const pushToast = useCallback((toast: Omit<Toast, "id">) => {
    toastIdRef.current += 1;
    const id = toastIdRef.current;
    setToasts((prev) => [...prev, { id, ...toast }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 6000);
  }, []);

  // Poll halt state so the top-bar indicator stays live.
  //
  // UX-06 (#248): also derive whether the pipeline is actively running
  // from the dashboard snapshot, so the Pause-production button can
  // hide when idle (red-only-for-real-problems colour policy).
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
          // ignore network errors; polling resumes next tick
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
        detail: "Add a directive below to say what should change.",
      });
      inputRef.current?.focus();
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

  // Actually POST the pending directive after the reviewer confirms
  // the cost preview. Kept separate from ``openDirectivePreview`` so
  // the Cancel button simply closes the dialog without touching the
  // directive input.
  const postPendingDirective = useCallback(async () => {
    const pending = pendingDirectiveRef.current;
    if (!pending) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${BACKEND_URL}/api/directive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          directive: pending.text,
          slot_context: pending.slot_context,
          reviewer: "dashboard-user",
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        pushToast({
          kind: "error",
          message:
            res.status === 422
              ? "Directive couldn't be parsed"
              : `Directive rejected (${res.status})`,
          detail:
            typeof payload?.error === "string"
              ? payload.error
              : JSON.stringify(payload).slice(0, 200),
        });
        return;
      }
      const data = payload as DirectiveResponse;
      const summary = data.records.map(summariseRecord).join("\n");
      pushToast({
        kind: "success",
        message: `Directive accepted — ${data.records.length} record(s) added`,
        detail: summary,
      });
      onRecordsAppended?.(data.records);
      setDirective("");
      pendingDirectiveRef.current = null;
      setDirectiveDialogOpen(false);
    } catch (err) {
      pushToast({
        kind: "error",
        message: "Directive request failed",
        detail: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  }, [pushToast, onRecordsAppended]);

  // DESIGN-07 (#259): open the cost-preview dialog for the directive
  // the reviewer just typed. Stashes the text + slot_context so the
  // POST has something stable to reference even if the reviewer keeps
  // typing in the input while the estimate fetches.
  const submitDirective = useCallback(
    async (ev?: React.FormEvent<HTMLFormElement>) => {
      ev?.preventDefault();
      const text = directive.trim();
      if (!text || submitting) return;
      pendingDirectiveRef.current = {
        text,
        slot_context: selectedSlot ?? null,
      };
      setDirectiveEstimate(null);
      setDirectiveEstimateLoading(true);
      setDirectiveDialogOpen(true);
      try {
        const est = await fetchDirectiveEstimate({
          directive: text,
          slot_context: selectedSlot ?? null,
          action: "Apply directive",
        });
        setDirectiveEstimate(est);
      } finally {
        setDirectiveEstimateLoading(false);
      }
    },
    [directive, submitting, selectedSlot],
  );

  const haltEngaged = haltState?.halt_requested === true;
  const humanScopeLabel = selectedSlotMeta
    ? describeOtioSlot(selectedSlotMeta)
    : selectedSlot
    ? describeSelectedSlot(selectedSlot)
    : null;
  const scopeLabel = humanScopeLabel ?? "global (no slot selected)";
  // The chip mirrors whichever selection source is actually driving the
  // directive payload — the override prop when one is provided (even if
  // explicitly `null`, which means the parent is intentionally scoping
  // to global), and the store otherwise. This keeps the visible chip in
  // lockstep with the `slot_context` we POST.
  const showScopeChip =
    selectedSlotOverride !== undefined
      ? selectedSlotOverride != null
      : selectedSlotId != null;
  // Only the store-backed path can actually be cleared from here. When
  // a parent provides `selectedSlot` as a prop override, the parent owns
  // its lifecycle — rendering a × that calls `clearSelection()` would
  // appear non-functional (see Devin Review on PR #219).
  const showScopeClear =
    selectedSlotOverride === undefined && selectedSlotId != null;

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
              {humanScopeLabel ? (
                <>
                  Scope:{" "}
                  <span className="text-pipeline-text">{scopeLabel}</span>
                </>
              ) : (
                <span className="text-pipeline-text">
                  Applies to the whole film.
                </span>
              )}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* DESIGN-08 (#260): rewind dropdown. Only meaningful while
            * the pipeline is actively running; hidden otherwise so
            * the controls row stays empty when idle. */}
          {pipelineRunning && (
            <RewindDropdown
              disabled={haltSubmitting}
              onRewindAccepted={(_stage, label) =>
                pushToast({
                  kind: "success",
                  message: `${label} — pipeline will pause and roll back`,
                })
              }
              onRewindFailed={(_stage, detail) =>
                pushToast({
                  kind: "error",
                  message: "Rewind failed",
                  detail,
                })
              }
              onAbandonAccepted={() =>
                pushToast({
                  kind: "halt",
                  message:
                    "Abandon requested — pipeline will shut down at next checkpoint",
                })
              }
              onAbandonFailed={(detail) =>
                pushToast({
                  kind: "error",
                  message: "Abandon failed",
                  detail,
                })
              }
            />
          )}
          {/* DESIGN-09 (#261): amber pause button with a cost-preview
            * confirmation step.  The button hides entirely while the
            * halt flag is engaged -- HaltResumeCard owns that UI. */}
          <HaltPauseButton
            running={pipelineRunning}
            halted={haltEngaged}
            submitting={haltSubmitting}
            onConfirmPause={submitHalt}
          />
        </div>
      </div>
      {haltEngaged && (
        <HaltResumeCard
          haltState={haltState}
          onRelease={releaseHalt}
        />
      )}
      {showScopeChip && (
        <div
          className="flex items-center gap-2 text-xs"
          data-testid="directive-scope-chip"
        >
          <span className="text-pipeline-muted">scoped to</span>
          <span
            className="inline-flex items-center gap-1 rounded-full border border-pipeline-accent bg-pipeline-accent/20 px-2 py-0.5 text-[11px] text-pipeline-text"
            title={
              // UX-05 (#247): keep the internal slot id available on
              // hover while the primary label stays human-readable.
              selectedSlotMeta?.slot_id ??
              (selectedSlotOverride?.scope_ref as string | undefined) ??
              selectedSlotId ??
              undefined
            }
          >
            <span aria-hidden="true">◉</span>
            <span>{scopeLabel}</span>
            {showScopeClear && (
              <button
                type="button"
                onClick={() => clearSelection()}
                className="ml-1 rounded px-1 text-pipeline-muted hover:text-pipeline-text"
                aria-label="Clear slot scope (make directive global)"
                data-testid="directive-scope-clear"
              >
                ×
              </button>
            )}
          </span>
        </div>
      )}
      <form
        onSubmit={submitDirective}
        className="flex items-center gap-2"
        data-testid="directive-form"
      >
        <input
          ref={inputRef}
          type="text"
          value={directive}
          onChange={(e) => setDirective(e.target.value)}
          placeholder={
            selectedSlot
              ? "Directive for the selected slot (e.g. ‘Cassandra louder here’)"
              : "Directive (e.g. ‘prefer shorter narration globally’)"
          }
          className="flex-1 rounded border border-pipeline-blue bg-pipeline-bg px-3 py-2 text-sm text-pipeline-text placeholder:text-pipeline-muted focus:border-pipeline-accent focus:outline-none"
          data-testid="directive-input"
          disabled={submitting}
        />
        <button
          type="submit"
          disabled={submitting || !directive.trim()}
          className="rounded bg-pipeline-accent px-3 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
          data-testid="directive-submit"
        >
          {submitting ? "Sending…" : "Send a note to the producer"}
        </button>
      </form>
      {/* DESIGN-07 (#259): cost preview before the directive actually
        * hits the Preference Interpreter. Confirm fires the POST; cancel
        * leaves the input value untouched so the reviewer can edit. */}
      <CostPreviewDialog
        open={directiveDialogOpen}
        onOpenChange={(next) => {
          setDirectiveDialogOpen(next);
          if (!next) pendingDirectiveRef.current = null;
        }}
        title="Apply this directive?"
        estimate={directiveEstimate}
        loading={directiveEstimateLoading}
        onConfirm={postPendingDirective}
        submitting={submitting}
        dataTestId="directive-cost-dialog"
      />
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

/**
 * UI-05c: halt resume card rendered while the halt flag is engaged.
 *
 * Offers the three documented exit paths -- resume from current stage,
 * rewind to the last safe checkpoint, or exit the run.  All three go
 * through ``POST /api/halt/release`` with a ``mode`` parameter; the
 * backend decides the state transition (synthetic rewind directive,
 * sticky exit flag, or plain release).
 */
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
        <span className="text-sm font-semibold">
          Paused at {stage}.
        </span>
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

function findSlotInSnapshot(
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

/**
 * Translate an OTIO slot (from the UI-02a selection store) into the
 * `SlotContext` payload the backend's directive endpoint expects.
 */
export function deriveSlotContext(
  slotId: string,
  slot: OtioSlot | null,
): SlotContext {
  const ctx: SlotContext = {
    scope: "element",
    scope_ref: slotId,
    clip_id: slotId,
  };
  if (slot) {
    ctx.scene_num = slot.scene_num;
  }
  return ctx;
}

/**
 * UX-05 (#247): convert an OTIO slot into a plain-English label such as
 * "Scene 1 · opening" rather than an internal ``slot_id`` like
 * ``v1_video__scene_1__phrase_0``. Preference order:
 *
 *   1. the slot's own human ``label`` if one is set;
 *   2. ``Scene <n> · <track>`` otherwise (falling back to phrase idx when
 *      scene_num is unavailable).
 *
 * The raw slot id is kept as a tooltip (see the directive chip) so power
 * users can still see it without it being the first thing on screen.
 */
function describeOtioSlot(slot: OtioSlot): string {
  const trackLabel =
    slot.track === "V1_Video"
      ? "video"
      : slot.track === "A1_Narration"
      ? "narration"
      : slot.track === "A2_Music"
      ? "music"
      : slot.track;
  const sceneLabel =
    typeof slot.scene_num === "number"
      ? `Scene ${slot.scene_num}`
      : typeof slot.phrase_idx === "number"
      ? `Phrase ${slot.phrase_idx + 1}`
      : "Clip";
  const human = slot.label && slot.label.trim().length > 0
    ? slot.label.trim()
    : trackLabel;
  return `${sceneLabel} · ${human}`;
}


function describeSelectedSlot(slot: SlotContext): string {
  if (slot.scope && typeof slot.scope === "string") {
    const ref = slot.scope_ref ?? "";
    return ref ? `${slot.scope}:${ref}` : slot.scope;
  }
  if (typeof slot.scene_num === "number") return `scene-${slot.scene_num}`;
  if (typeof slot.scene_id === "string") return slot.scene_id;
  if (typeof slot.voice_block_id === "string")
    return `voice_block:${slot.voice_block_id}`;
  if (typeof slot.clip_id === "string") return `clip:${slot.clip_id}`;
  if (typeof slot.stage === "string") return `stage:${slot.stage}`;
  return "scoped";
}
