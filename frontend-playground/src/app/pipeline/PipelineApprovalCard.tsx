"use client";

/**
 * Approval-gate card for ``/pipeline?mode=live``.
 *
 * Renders one row per pending or resolved approval gate. For
 * pending gates (``resolved === false``) the card surfaces three
 * buttons — Approve / Edit / Reject — that POST to
 * ``/playground/approval/resume/{run_id}/{interrupt_id}`` via
 * :func:`resolvePipelineApproval`. The Edit button toggles a
 * minimal JSON-textarea editor so the operator can mutate the
 * tool args before re-planning.
 *
 * The component intentionally has zero direct knowledge of SSE —
 * its parent (:class:`PipelineOrchestrator`) derives
 * :class:`ApprovalState` from the run stream and feeds the card
 * a single approval. When the resume call returns the
 * orchestrator's gate loop wakes; the matching
 * ``pipeline.approval.resumed`` event flips ``approval.resolved``
 * and the card collapses into a "resumed: …" pill.
 */

import { useCallback, useState } from "react";

import {
  resolvePipelineApproval,
  type ApprovalDecisionBody,
} from "@/lib/api";

/** Subset of :class:`ApprovalState` the card actually consumes. */
export interface ApprovalCardApproval {
  readonly gate: string;
  readonly waitingSeq: number;
  readonly resolved: boolean;
  readonly decision: string | null;
  readonly runId: string | null;
  readonly interruptId: string | null;
  readonly args: Record<string, unknown> | null;
}

export interface PipelineApprovalCardProps {
  readonly approval: ApprovalCardApproval;
  /** Callback fired the instant a button click resolves; lets the
   * parent surface optimistic UI even before the SSE
   * ``pipeline.approval.resumed`` lands. ``null`` for tests that
   * don't care. */
  readonly onResolved?:
    | ((decision: ApprovalDecisionBody["type"]) => void)
    | null;
}

type CardMode = "idle" | "editing";

interface SubmissionState {
  readonly inFlight: boolean;
  readonly error: string | null;
}

/**
 * Render one pending/resolved approval gate row.
 *
 * Resolved gates show a single read-only pill. Pending gates show
 * three buttons + an inline edit panel. The component is a leaf
 * — all state lives locally; resolution propagates back via
 * ``onResolved`` and the SSE stream.
 */
export function PipelineApprovalCard({
  approval,
  onResolved,
}: PipelineApprovalCardProps): JSX.Element {
  const [mode, setMode] = useState<CardMode>("idle");
  const [editsText, setEditsText] = useState<string>(() =>
    approval.args ? JSON.stringify(approval.args, null, 2) : "{}",
  );
  const [feedbackText, setFeedbackText] = useState<string>("");
  const [submission, setSubmission] = useState<SubmissionState>({
    inFlight: false,
    error: null,
  });

  const canDispatch =
    !approval.resolved &&
    approval.runId !== null &&
    approval.interruptId !== null;

  const dispatchDecision = useCallback(
    async (decision: ApprovalDecisionBody) => {
      if (!canDispatch || approval.runId === null || approval.interruptId === null) {
        setSubmission({
          inFlight: false,
          error: "Gate is missing run_id / interrupt_id; cannot resolve.",
        });
        return;
      }
      setSubmission({ inFlight: true, error: null });
      try {
        await resolvePipelineApproval(
          approval.runId,
          approval.interruptId,
          decision,
        );
        setSubmission({ inFlight: false, error: null });
        setMode("idle");
        if (onResolved) {
          onResolved(decision.type);
        }
      } catch (caught) {
        const message =
          caught instanceof Error ? caught.message : String(caught);
        setSubmission({ inFlight: false, error: message });
      }
    },
    [approval.runId, approval.interruptId, canDispatch, onResolved],
  );

  const onApprove = useCallback(() => {
    void dispatchDecision({ type: "approve" });
  }, [dispatchDecision]);

  const onReject = useCallback(() => {
    void dispatchDecision({
      type: "reject",
      feedback: feedbackText.trim() || "Rejected by operator",
    });
  }, [dispatchDecision, feedbackText]);

  const onEditToggle = useCallback(() => {
    setMode((current) => (current === "editing" ? "idle" : "editing"));
    setSubmission({ inFlight: false, error: null });
  }, []);

  const onEditSubmit = useCallback(() => {
    let edits: Record<string, unknown>;
    try {
      const parsed = JSON.parse(editsText) as unknown;
      if (
        !parsed ||
        typeof parsed !== "object" ||
        Array.isArray(parsed)
      ) {
        setSubmission({
          inFlight: false,
          error: "Edits must be a JSON object.",
        });
        return;
      }
      edits = parsed as Record<string, unknown>;
    } catch (caught) {
      const message =
        caught instanceof Error ? caught.message : String(caught);
      setSubmission({
        inFlight: false,
        error: `Invalid JSON: ${message}`,
      });
      return;
    }
    void dispatchDecision({
      type: "edit",
      edits,
      feedback: feedbackText.trim() || undefined,
    });
  }, [dispatchDecision, editsText, feedbackText]);

  return (
    <li
      key={`${approval.gate}-${approval.waitingSeq}`}
      className="flex flex-col gap-2 rounded border border-pg-border bg-pg-surface px-3 py-2 text-sm"
      data-testid={`pipeline-approval-${approval.gate}`}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-pg-text">{approval.gate}</span>
        {approval.resolved ? (
          <span
            className="rounded bg-pg-green/20 px-2 py-0.5 text-xs text-pg-green"
            data-testid="pipeline-approval-status-resolved"
          >
            resumed: {approval.decision ?? "approve"}
          </span>
        ) : (
          <span
            className="rounded bg-pg-amber/20 px-2 py-0.5 text-xs text-pg-amber"
            data-testid="pipeline-approval-status-waiting"
          >
            waiting…
          </span>
        )}
      </div>

      {approval.resolved ? null : (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded border border-pg-green/40 bg-pg-green/10 px-3 py-1 text-xs font-medium text-pg-green hover:bg-pg-green/20 disabled:opacity-50"
              data-testid="pipeline-approval-approve"
              disabled={!canDispatch || submission.inFlight}
              onClick={onApprove}
            >
              {submission.inFlight ? "…" : "Approve"}
            </button>
            <button
              type="button"
              className="rounded border border-pg-amber/40 bg-pg-amber/10 px-3 py-1 text-xs font-medium text-pg-amber hover:bg-pg-amber/20 disabled:opacity-50"
              data-testid="pipeline-approval-edit-toggle"
              disabled={!canDispatch || submission.inFlight}
              onClick={onEditToggle}
            >
              {mode === "editing" ? "Cancel edit" : "Edit"}
            </button>
            <button
              type="button"
              className="rounded border border-pg-red/40 bg-pg-red/10 px-3 py-1 text-xs font-medium text-pg-red hover:bg-pg-red/20 disabled:opacity-50"
              data-testid="pipeline-approval-reject"
              disabled={!canDispatch || submission.inFlight}
              onClick={onReject}
            >
              Reject
            </button>
          </div>

          {mode === "editing" ? (
            <div
              className="flex flex-col gap-2 rounded border border-pg-border bg-pg-bg p-3"
              data-testid="pipeline-approval-edit-panel"
            >
              <label className="flex flex-col gap-1 text-xs text-pg-muted">
                Tool args (JSON object)
                <textarea
                  className="min-h-[6rem] rounded border border-pg-border bg-pg-surface p-2 font-mono text-xs text-pg-text"
                  data-testid="pipeline-approval-edits-textarea"
                  value={editsText}
                  onChange={(event) => setEditsText(event.target.value)}
                  spellCheck={false}
                />
              </label>
              <label className="flex flex-col gap-1 text-xs text-pg-muted">
                Feedback (optional)
                <input
                  type="text"
                  className="rounded border border-pg-border bg-pg-surface p-2 font-mono text-xs text-pg-text"
                  data-testid="pipeline-approval-feedback-input"
                  value={feedbackText}
                  onChange={(event) => setFeedbackText(event.target.value)}
                />
              </label>
              <div className="flex gap-2">
                <button
                  type="button"
                  className="rounded border border-pg-amber/40 bg-pg-amber/10 px-3 py-1 text-xs font-medium text-pg-amber hover:bg-pg-amber/20 disabled:opacity-50"
                  data-testid="pipeline-approval-edit-submit"
                  disabled={submission.inFlight}
                  onClick={onEditSubmit}
                >
                  {submission.inFlight ? "Submitting…" : "Submit edit"}
                </button>
              </div>
            </div>
          ) : null}

          {!canDispatch ? (
            <span className="text-xs text-pg-muted">
              Gate received without resume coordinates; the demo simulator
              auto-approves.
            </span>
          ) : null}

          {submission.error ? (
            <span
              className="text-xs text-pg-red"
              data-testid="pipeline-approval-error"
            >
              {submission.error}
            </span>
          ) : null}
        </div>
      )}
    </li>
  );
}
