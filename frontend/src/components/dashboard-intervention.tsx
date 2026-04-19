"use client";

/**
 * ARCH-H4 (#159): dashboard intervention controls.
 *
 * Renders the two proactive-L4 entry points alongside the existing
 * reactive approval-gate flow:
 *
 * 1. **Halt-anywhere button** -- the red "I don't like this movie" button
 *    in the top-right of the dashboard shell. Posts to `POST /api/halt`,
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
};

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
   * The slot the reviewer currently has selected (if any), piped through
   * from the H3 slot detail panel. When present, the directive is scoped
   * to this slot by default; only an explicit "global" control would
   * override.
   */
  selectedSlot?: SlotContext | null;

  /**
   * Fires after each successful directive submission. The parent ledger
   * history panel uses this to highlight the newly-added entries.
   */
  onRecordsAppended?: (records: DirectiveRecord[]) => void;
};

export function DashboardIntervention({
  selectedSlot,
  onRecordsAppended,
}: DashboardInterventionProps) {
  const [directive, setDirective] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [haltSubmitting, setHaltSubmitting] = useState(false);
  const [haltState, setHaltState] = useState<HaltState | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
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

  const releaseHalt = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/halt/release`, {
        method: "POST",
      });
      if (res.ok) {
        const data: HaltState = await res.json();
        setHaltState(data);
        pushToast({ kind: "success", message: "Halt released — pipeline resuming" });
      }
    } catch {
      // ignore
    }
  }, [pushToast]);

  const submitDirective = useCallback(
    async (ev?: React.FormEvent<HTMLFormElement>) => {
      ev?.preventDefault();
      const text = directive.trim();
      if (!text || submitting) return;
      setSubmitting(true);
      try {
        const res = await fetch(`${BACKEND_URL}/api/directive`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            directive: text,
            slot_context: selectedSlot ?? null,
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
      } catch (err) {
        pushToast({
          kind: "error",
          message: "Directive request failed",
          detail: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setSubmitting(false);
      }
    },
    [directive, submitting, selectedSlot, pushToast, onRecordsAppended],
  );

  const haltEngaged = haltState?.halt_requested === true;
  const scopeLabel = selectedSlot
    ? describeSelectedSlot(selectedSlot)
    : "global (no slot selected)";

  return (
    <div className="flex flex-col gap-2 border-b border-pipeline-blue bg-pipeline-card px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col">
          <span className="text-xs font-semibold uppercase tracking-wide text-pipeline-muted">
            Human intervention (ARCH-H4)
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
              Scope: <span className="text-pipeline-text">{scopeLabel}</span>
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {haltEngaged ? (
            <button
              type="button"
              onClick={releaseHalt}
              className="rounded bg-pipeline-blue px-3 py-2 text-sm font-medium text-pipeline-text hover:bg-pipeline-accent"
              data-testid="halt-release-button"
            >
              Release halt
            </button>
          ) : (
            <button
              type="button"
              onClick={submitHalt}
              disabled={haltSubmitting}
              className="rounded bg-red-600 px-3 py-2 text-sm font-semibold text-white hover:bg-red-500 disabled:opacity-60"
              data-testid="halt-button"
              title="Pause the pipeline at the next safe checkpoint"
            >
              {haltSubmitting ? "Halting…" : "I don't like this movie"}
            </button>
          )}
        </div>
      </div>
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
          {submitting ? "Sending…" : "Send directive"}
        </button>
      </form>
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
