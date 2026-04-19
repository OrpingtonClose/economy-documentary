"use client";

/**
 * DESIGN-09 (#261): Halt / pause button, styled as a pause (amber),
 * not an emergency (red).
 *
 * Red is reserved for genuine problems (worker down, QA reject,
 * abandon-run). Pausing is reversible and safe, so this button uses
 * an amber treatment and plain-English copy:
 *
 *   - idle, not running: the button hides entirely (UX-06 policy).
 *   - idle, running: "Pause production" (amber outline).
 *   - submitting: "Pausing…" (amber, disabled).
 *
 * When the halt flag is already engaged the button hides -- the
 * :class:`HaltResumeCard` takes over and offers the three documented
 * exit modes (resume, rewind, exit).  That keeps exactly one control
 * on screen at a time and avoids the "two Resume buttons" footgun.
 *
 * Pressing "Pause production" does NOT immediately post -- it opens a
 * :func:`CostPreviewDialog` with a description-only body explaining
 * that pausing is free ("nothing lost — resume anytime"). The actual
 * halt request fires when the reviewer confirms.
 */

import * as React from "react";

import { CostPreviewDialog } from "@/components/cost-preview-dialog";

export type HaltPauseButtonProps = {
  /** Is the pipeline currently running (i.e. does pausing make sense)? */
  running: boolean;
  /** Is the halt flag currently engaged? */
  halted: boolean;
  /** Invoked after the reviewer confirms "Pause production". */
  onConfirmPause: () => void | Promise<void>;
  /** True while the pause request is in-flight. */
  submitting?: boolean;
};

export const PAUSE_DESCRIPTION =
  "This will pause at the next safe checkpoint, nothing lost — resume anytime.";

export function HaltPauseButton({
  running,
  halted,
  onConfirmPause,
  submitting,
}: HaltPauseButtonProps) {
  const [dialogOpen, setDialogOpen] = React.useState(false);

  const handleConfirm = React.useCallback(async () => {
    await onConfirmPause();
    setDialogOpen(false);
  }, [onConfirmPause]);

  // Hide entirely when halted (HaltResumeCard owns the resume flow)
  // or when the pipeline is idle (UX-06 red-only-for-real-problems).
  if (halted || !running) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setDialogOpen(true)}
        disabled={submitting}
        className="rounded border border-amber-500/70 bg-amber-900/20 px-3 py-2 text-sm font-semibold text-amber-100 hover:bg-amber-900/40 disabled:opacity-60"
        data-testid="halt-pause-button"
        title="Pause the pipeline at the next safe checkpoint"
      >
        {submitting ? "Pausing…" : "Pause production"}
      </button>
      <CostPreviewDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        title="Pause production"
        description={PAUSE_DESCRIPTION}
        // Pausing has no extra cost -- we render the description only
        // and skip the numeric estimate block.
        estimate={null}
        onConfirm={handleConfirm}
        confirmLabel="Pause production"
        cancelLabel="Keep running"
        submitting={submitting}
        dataTestId="halt-pause-dialog"
      />
    </>
  );
}
