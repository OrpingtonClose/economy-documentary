"use client";

/**
 * DESIGN-07 (#259): Cost-preview dialog.
 *
 * Wraps any control action that reruns work or changes the plan with a
 * plain-English "here's what this will cost" confirmation step. The
 * dialog shows three things in order:
 *
 *   1. A short, non-jargon action title ("Redo this scene", "Rewind to
 *      narration", "Pause production").
 *   2. A plain-English summary of the cost ("This will rerun 3 scenes,
 *      add about 20 minutes, and cost about $2.10."), plus three
 *      compact badges with the individual numbers for readers who
 *      prefer to scan.
 *   3. "Continue" + "Cancel" buttons. Neither is styled red -- we
 *      reserve red for genuine errors (see DESIGN-09). "Continue" is
 *      the default shadcn primary style; "Cancel" is an outline.
 *
 * The dialog is intentionally presentational: callers fetch the
 * :class:`CostEstimate` (via :func:`fetchDirectiveEstimate` or their
 * own source) and pass it in. That keeps the dialog reusable from the
 * intent bar, the rewind dropdown, and any future drilldown "Redo
 * this scene" entry point without each consumer having to agree on
 * the data-fetching strategy.
 */

import * as React from "react";
import { Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { CostEstimate } from "@/lib/cost-estimate";

export type CostPreviewDialogProps = {
  /** Controlled open state. */
  open: boolean;
  /** Controlled open-state setter. */
  onOpenChange: (open: boolean) => void;
  /** Short plain-English title ("Redo this scene"). */
  title: string;
  /**
   * Optional description rendered above the estimate. Used for the
   * pause button ("This will pause at the next safe checkpoint,
   * nothing lost — resume anytime.") where a numeric cost preview
   * would be misleading -- pausing has no extra cost.
   */
  description?: string;
  /** Cost estimate to render. ``null`` shows a loading placeholder. */
  estimate: CostEstimate | null;
  /** Loading indicator (e.g. while the backend estimate fetches). */
  loading?: boolean;
  /** Click handler for the primary "Continue" button. */
  onConfirm: () => void | Promise<void>;
  /** Override the primary-button label; defaults to "Continue". */
  confirmLabel?: string;
  /** Override the secondary-button label; defaults to "Cancel". */
  cancelLabel?: string;
  /** Disable the confirm button while the consumer is submitting. */
  submitting?: boolean;
  /**
   * Optional test-id for the dialog body. Defaults to
   * ``cost-preview-dialog`` so tests can locate it without reaching
   * through the portal.
   */
  dataTestId?: string;
};

export function CostPreviewDialog({
  open,
  onOpenChange,
  title,
  description,
  estimate,
  loading,
  onConfirm,
  confirmLabel = "Continue",
  cancelLabel = "Cancel",
  submitting,
  dataTestId = "cost-preview-dialog",
}: CostPreviewDialogProps) {
  const handleConfirm = React.useCallback(async () => {
    await onConfirm();
  }, [onConfirm]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid={dataTestId}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? (
            <DialogDescription data-testid={`${dataTestId}-description`}>
              {description}
            </DialogDescription>
          ) : null}
        </DialogHeader>
        <div className="space-y-3 text-sm">
          {loading ? (
            <div
              className="flex items-center gap-2 text-muted-foreground"
              data-testid={`${dataTestId}-loading`}
            >
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              <span>Working out what this will cost…</span>
            </div>
          ) : estimate === null ? null : (
            <>
              <p
                className="text-foreground"
                data-testid={`${dataTestId}-summary`}
              >
                {estimate.summary}
              </p>
              <div className="flex flex-wrap gap-2">
                <Badge
                  variant="secondary"
                  data-testid={`${dataTestId}-badge-stages`}
                >
                  {estimate.stages}{" "}
                  {estimate.stages === 1
                    ? estimate.stage_label
                    : `${estimate.stage_label}s`}
                </Badge>
                <Badge
                  variant="secondary"
                  data-testid={`${dataTestId}-badge-minutes`}
                >
                  ~{estimate.eta_minutes} min
                </Badge>
                <Badge
                  variant="secondary"
                  data-testid={`${dataTestId}-badge-dollars`}
                >
                  ~${estimate.dollars.toFixed(2)}
                </Badge>
              </div>
              {estimate.note ? (
                <p className="text-xs text-muted-foreground">{estimate.note}</p>
              ) : null}
            </>
          )}
        </div>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            data-testid={`${dataTestId}-cancel`}
            disabled={submitting}
          >
            {cancelLabel}
          </Button>
          <Button
            type="button"
            onClick={() => void handleConfirm()}
            data-testid={`${dataTestId}-confirm`}
            disabled={submitting || loading}
          >
            {submitting ? "Working…" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
