"use client";

/**
 * UI-03b (#199): inline approval gate card on the OTIO timeline.
 *
 * Rendered at the stage boundary where the gate fired.  Two actions:
 *
 *   - **Approve**   → POST ``/agui/approve`` with ``{stage}``.  Reuses
 *                     the reactive-L4 approval endpoint; no new path.
 *   - **Reject with note** → UI-03c (#200).  Opens an inline directive
 *                     input pre-scoped with ``{scope: "stage",
 *                     scope_ref: <stage>}`` and posts to
 *                     ``/api/directive``.  The backend appends a
 *                     ledger record and releases the gate (via
 *                     :func:`callbacks.approval_gate.approve_stage`)
 *                     so the pipeline keeps moving with the new drift
 *                     applied.
 *
 * The card auto-unmounts when ``approval_gate_closed`` arrives via the
 * unified AG-UI SSE bus — regardless of whether the approve button or
 * the reject-with-note button fired the close.
 */

import { useCallback, useState } from "react";
import type { ApprovalGateEvent } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

const STAGE_LABELS: Record<string, string> = {
  scenario: "Scenario",
  audio: "Audio",
  prompts: "Visual prompts",
  clips: "Production clips",
  timeline: "Timeline",
  assembly: "Final assembly",
};

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

export type ApprovalCardProps = {
  gate: ApprovalGateEvent;
};

export function ApprovalCard({ gate }: ApprovalCardProps) {
  const [mode, setMode] = useState<"idle" | "rejecting">("idle");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState<"approve" | "reject" | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const approve = useCallback(async () => {
    setSubmitting("approve");
    setError(null);
    try {
      const res = await fetch(`${BACKEND_URL}/agui/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage: gate.stage }),
      });
      if (!res.ok) {
        const text = await res.text();
        setError(`Approve failed (${res.status}): ${text.slice(0, 160)}`);
      }
      // On success the backend flips the approval flag; the
      // wait_for_approval poll loop emits approval_gate_closed on the
      // unified stream, which unmounts this card via useOtioStream.
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubmitting(null);
    }
  }, [gate.stage]);

  const rejectWithNote = useCallback(
    async (ev: React.FormEvent<HTMLFormElement>) => {
      ev.preventDefault();
      const trimmed = note.trim();
      if (!trimmed) return;
      setSubmitting("reject");
      setError(null);
      try {
        const res = await fetch(`${BACKEND_URL}/api/directive`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            directive: trimmed,
            slot_context: { scope: "stage", scope_ref: gate.stage },
            reviewer: "dashboard-user",
          }),
        });
        if (!res.ok) {
          const payload = await res.json().catch(() => ({}));
          setError(
            typeof payload?.error === "string"
              ? payload.error
              : `Directive rejected (${res.status})`,
          );
          return;
        }
        // Success: backend appended a ledger record AND called
        // approve_stage(gate.stage); wait_for_approval will emit
        // approval_gate_closed within one poll tick, unmounting us.
        setNote("");
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setSubmitting(null);
      }
    },
    [gate.stage, note],
  );

  return (
    <div
      className="pointer-events-auto rounded-lg border border-amber-400/60 bg-amber-900/30 p-3 text-xs text-amber-50 shadow-lg backdrop-blur"
      data-testid={`approval-card-${gate.stage}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-200/80">
            Approval gate · {stageLabel(gate.stage)}
          </div>
          <div className="mt-0.5 text-sm text-amber-50">
            {stageLabel(gate.stage)} ready — approve to proceed, or reject
            with a note.
          </div>
        </div>
        {mode === "idle" && (
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={approve}
              disabled={submitting !== null}
              className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-60"
              data-testid={`approval-card-approve-${gate.stage}`}
            >
              {submitting === "approve" ? "Approving…" : "Approve"}
            </button>
            <button
              type="button"
              onClick={() => setMode("rejecting")}
              disabled={submitting !== null}
              className="rounded border border-amber-300/60 bg-amber-800/40 px-3 py-1.5 text-xs font-semibold text-amber-50 hover:bg-amber-700/50 disabled:opacity-60"
              data-testid={`approval-card-reject-${gate.stage}`}
            >
              Reject with note
            </button>
          </div>
        )}
      </div>

      {mode === "rejecting" && (
        <form
          onSubmit={rejectWithNote}
          className="mt-3 flex flex-col gap-2"
          data-testid={`approval-card-reject-form-${gate.stage}`}
        >
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={`What would you like changed about the ${stageLabel(
              gate.stage,
            ).toLowerCase()}?`}
            rows={3}
            autoFocus
            className="w-full resize-none rounded border border-amber-300/60 bg-pipeline-bg px-2 py-1 text-sm text-pipeline-text placeholder:text-pipeline-muted focus:border-pipeline-accent focus:outline-none"
            data-testid={`approval-card-reject-note-${gate.stage}`}
          />
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setMode("idle");
                setNote("");
                setError(null);
              }}
              disabled={submitting !== null}
              className="rounded px-3 py-1.5 text-xs font-medium text-amber-100/80 hover:text-amber-50 disabled:opacity-60"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting !== null || !note.trim()}
              className="rounded bg-red-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-500 disabled:opacity-50"
              data-testid={`approval-card-reject-submit-${gate.stage}`}
            >
              {submitting === "reject" ? "Sending…" : "Send directive"}
            </button>
          </div>
        </form>
      )}

      {error && (
        <div
          className="mt-2 rounded border border-red-400/60 bg-red-900/40 px-2 py-1 text-[11px] text-red-100"
          role="alert"
        >
          {error}
        </div>
      )}
    </div>
  );
}
