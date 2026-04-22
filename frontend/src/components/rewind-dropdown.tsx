"use client";

/**
 * DESIGN-08 (#260): Rewind dropdown affordance.
 *
 * Adds a small "Rewind" trigger to the dashboard controls area. Each
 * item maps to one of the known pipeline stages but is labelled in
 * plain English so non-engineers can reason about it:
 *
 *   - "Rewind to scenario"        → backend stage ``scenario``
 *   - "Rewind to narration"       → backend stage ``audio``
 *   - "Rewind to visuals"         → backend stage ``visual_director``
 *   - "Rewind to production"      → backend stage ``video``
 *   - "Rewind to final touches"   → backend stage ``assembly``
 *
 * Clicking a stage opens the shared DESIGN-07 :func:`CostPreviewDialog`
 * with a plain-English cost summary. On confirm we POST to
 * ``/agui/rewind_to_stage``.
 *
 * An additional "Abandon run" item lives at the bottom of the menu.
 * It is the only red affordance here (DESIGN-09 reserves red for
 * genuine problems, and abandoning is irreversible). Clicking it
 * opens an :func:`AlertDialog` that requires the reviewer to type
 * ``abandon`` before the Confirm button enables.
 */

import * as React from "react";
import { ChevronDown, History, AlertCircle } from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CostPreviewDialog } from "@/components/cost-preview-dialog";
import { cn } from "@/lib/utils";
import {
  fetchDirectiveEstimate,
  type CostEstimate,
} from "@/lib/cost-estimate";

/**
 * Pipeline stage identifiers paired with their plain-English labels.
 * The stage names must match ``server/dashboard/sse.py::KNOWN_PIPELINE_STAGES``
 * -- DESIGN-08 forbids introducing new stage names.
 */
export const REWIND_STAGES: ReadonlyArray<{
  stage: string;
  label: string;
  description: string;
}> = [
  {
    stage: "scenario",
    label: "Rewind to scenario",
    description:
      "Go back to the scenario draft. Every later step (narration, visuals, production) will be redone.",
  },
  {
    stage: "audio",
    label: "Rewind to narration",
    description:
      "Go back to narration recording. Visuals and production will be redone to match.",
  },
  {
    stage: "visual_director",
    label: "Rewind to visuals",
    description:
      "Go back to visual direction. Production will be redone with the new visuals.",
  },
  {
    stage: "video",
    label: "Rewind to production",
    description:
      "Go back to video production. Final assembly will be redone with the new clips.",
  },
  {
    stage: "assembly",
    label: "Rewind to final touches",
    description:
      "Go back to the final assembly step. The finished film will be re-rendered.",
  },
] as const;

const BACKEND_URL =
  (typeof process !== "undefined"
    ? process.env.NEXT_PUBLIC_BACKEND_URL
    : undefined) || "http://localhost:8000";

export type RewindDropdownProps = {
  /** Called after a successful rewind request so the parent can toast. */
  onRewindAccepted?: (stage: string, label: string) => void;
  /** Called when the backend rejects a rewind request. */
  onRewindFailed?: (stage: string, detail: string) => void;
  /** Called after a successful abandon request so the parent can toast. */
  onAbandonAccepted?: () => void;
  /** Called when the backend rejects an abandon request. */
  onAbandonFailed?: (detail: string) => void;
  /** Override the backend URL (used from tests). */
  backendUrl?: string;
  /**
   * Disable every item in the menu. Typically wired to "pipeline isn't
   * running" so abandoned / rewound requests don't fire against an
   * idle backend.
   */
  disabled?: boolean;
};

type PendingStage = {
  stage: string;
  label: string;
  description: string;
};

export function RewindDropdown({
  onRewindAccepted,
  onRewindFailed,
  onAbandonAccepted,
  onAbandonFailed,
  backendUrl = BACKEND_URL,
  disabled,
}: RewindDropdownProps) {
  const [pending, setPending] = React.useState<PendingStage | null>(null);
  const [estimate, setEstimate] = React.useState<CostEstimate | null>(null);
  const [estimateLoading, setEstimateLoading] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  const [abandonOpen, setAbandonOpen] = React.useState(false);
  const [abandonText, setAbandonText] = React.useState("");
  const [abandonSubmitting, setAbandonSubmitting] = React.useState(false);

  // Monotonic fetch id guards against a stale estimate response
  // overwriting a newer one if the reviewer cancels and picks a
  // different stage before the first fetch settles. Without this,
  // the dialog could briefly show the previous stage's cost under
  // the newer stage's title.
  const fetchIdRef = React.useRef(0);

  const openRewindPreview = React.useCallback(
    (stage: PendingStage) => {
      setPending(stage);
      setEstimate(null);
      setEstimateLoading(true);
      const myId = ++fetchIdRef.current;
      // The helper never throws -- on any failure it returns a
      // client-side fallback estimate so the dialog always has
      // numbers to render.
      void fetchDirectiveEstimate(
        {
          stage: stage.stage,
          action: stage.label,
          directive: `rewind to ${stage.stage}`,
        },
        { backendUrl },
      ).then((est) => {
        // Drop stale responses so we never render cost numbers that
        // belong to a different stage than the one shown in the title.
        if (fetchIdRef.current !== myId) return;
        setEstimate(est);
        setEstimateLoading(false);
      });
    },
    [backendUrl],
  );

  const confirmRewind = React.useCallback(async () => {
    if (!pending) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${backendUrl}/agui/rewind_to_stage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stage: pending.stage,
          reason: pending.label,
          reviewer: "dashboard-user",
        }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        onRewindFailed?.(pending.stage, text.slice(0, 200));
        return;
      }
      onRewindAccepted?.(pending.stage, pending.label);
      setPending(null);
    } catch (err) {
      onRewindFailed?.(
        pending.stage,
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  }, [pending, backendUrl, onRewindAccepted, onRewindFailed]);

  const confirmAbandon = React.useCallback(async () => {
    setAbandonSubmitting(true);
    try {
      // Abandon-run is modelled as a halt-release with ``mode=exit`` --
      // this is the existing "sticky exit flag" path that tells the
      // pipeline to shut down at the next safe checkpoint.
      const res = await fetch(`${backendUrl}/api/halt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reviewer: "dashboard-user",
          reason: "abandon-run",
        }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        onAbandonFailed?.(text.slice(0, 200));
        return;
      }
      const res2 = await fetch(`${backendUrl}/api/halt/release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "exit" }),
      });
      if (!res2.ok) {
        const text = await res2.text().catch(() => "");
        onAbandonFailed?.(text.slice(0, 200));
        return;
      }
      onAbandonAccepted?.();
      setAbandonOpen(false);
      setAbandonText("");
    } catch (err) {
      onAbandonFailed?.(err instanceof Error ? err.message : String(err));
    } finally {
      setAbandonSubmitting(false);
    }
  }, [backendUrl, onAbandonAccepted, onAbandonFailed]);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            size="sm"
            className="gap-1"
            data-testid="rewind-dropdown-trigger"
            disabled={disabled}
          >
            <History className="h-4 w-4" aria-hidden="true" />
            Rewind
            <ChevronDown className="h-3 w-3 opacity-60" aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          <DropdownMenuLabel>Rewind the pipeline</DropdownMenuLabel>
          {REWIND_STAGES.map((stage) => (
            <DropdownMenuItem
              key={stage.stage}
              data-testid={`rewind-item-${stage.stage}`}
              onSelect={(event: Event) => {
                event.preventDefault();
                openRewindPreview(stage);
              }}
            >
              {stage.label}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-testid="rewind-item-abandon"
            className="text-destructive focus:text-destructive"
            onSelect={(event: Event) => {
              event.preventDefault();
              setAbandonOpen(true);
            }}
          >
            <AlertCircle className="mr-1 h-4 w-4" aria-hidden="true" />
            Abandon run
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <CostPreviewDialog
        open={pending !== null}
        onOpenChange={(next) => {
          if (!next) setPending(null);
        }}
        title={pending?.label ?? "Rewind"}
        description={pending?.description}
        estimate={estimate}
        loading={estimateLoading}
        onConfirm={confirmRewind}
        confirmLabel="Continue"
        cancelLabel="Cancel"
        submitting={submitting}
        dataTestId="rewind-cost-dialog"
      />

      <AlertDialog
        open={abandonOpen}
        onOpenChange={(next) => {
          setAbandonOpen(next);
          if (!next) setAbandonText("");
        }}
      >
        <AlertDialogContent data-testid="abandon-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Abandon this run?</AlertDialogTitle>
            <AlertDialogDescription>
              This stops the pipeline and throws away the current run.
              Type <span className="font-mono font-semibold">abandon</span> to
              confirm.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <input
            type="text"
            value={abandonText}
            onChange={(e) => setAbandonText(e.target.value)}
            placeholder="Type 'abandon' to confirm"
            className="w-full rounded border border-input bg-background px-3 py-2 text-sm focus:border-destructive focus:outline-none"
            data-testid="abandon-confirm-input"
            autoComplete="off"
          />
          <AlertDialogFooter>
            <AlertDialogCancel
              data-testid="abandon-cancel"
              disabled={abandonSubmitting}
            >
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="abandon-confirm"
              disabled={
                abandonText.trim().toLowerCase() !== "abandon" ||
                abandonSubmitting
              }
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                event.preventDefault();
                void confirmAbandon();
              }}
              className={cn(
                buttonVariants({ variant: "destructive" }),
                "bg-destructive text-destructive-foreground hover:bg-destructive/90",
              )}
            >
              {abandonSubmitting ? "Abandoning…" : "Abandon run"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
