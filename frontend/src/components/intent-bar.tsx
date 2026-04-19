"use client";

/**
 * DESIGN-06 (#258) — Intent bar.
 *
 * A persistent input pinned at the bottom of the main content column.
 * The reviewer types (or clicks a suggestion chip) and the text POSTs
 * to the existing `/api/directive` endpoint with a scope derived from
 * the current shared selection:
 *
 *   - nothing selected  → scope omitted (the backend treats it as a
 *     whole-film directive);
 *   - a scene/slot selected → ``scope: "scene"`` with the scene number,
 *     so the reviewer never has to think about slot ids.
 *
 * Three to six suggestion chips render beneath the input and change
 * with the active pipeline stage:
 *
 *   - scenario      → "Make it tighter", "Friendlier tone",
 *                      "More grounded", "Fewer rhetorical questions"
 *   - audio         → "Calmer delivery", "More energy"
 *   - visuals/clips → "Darker aesthetic", "Slower shots",
 *                      "More kinetic", "Warmer palette"
 *
 * Clicking a chip pre-fills the input with the chip text and submits in
 * one step — the user never has to type a slot id, and the chip text is
 * the directive content as-is.
 *
 * This component replaces the natural-language text input in
 * `dashboard-intervention.tsx`. The Pause / halt-resume surface stays
 * there as DESIGN-09 owns the pause redesign.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { CostPreviewDialog } from "@/components/cost-preview-dialog";
import {
  fetchDirectiveEstimate,
  type CostEstimate,
} from "@/lib/cost-estimate";
import { useOtioStream } from "@/lib/otio-stream";
import { useSelection } from "@/lib/stores/selection";
import type { OtioSlot } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

type Stage =
  | "brief"
  | "scenario"
  | "audio"
  | "visual_direction"
  | "production"
  | "assembly"
  | "completed"
  | "idle";

type Chip = {
  /** Short label rendered on the chip. */
  label: string;
  /** The full directive text sent when the chip is submitted. */
  directive: string;
};

const SCENARIO_CHIPS: Chip[] = [
  { label: "Make it tighter", directive: "Make it tighter" },
  { label: "Friendlier tone", directive: "Friendlier tone" },
  { label: "More grounded", directive: "More grounded" },
  { label: "Fewer rhetorical questions", directive: "Fewer rhetorical questions" },
];

const AUDIO_CHIPS: Chip[] = [
  { label: "Calmer delivery", directive: "Calmer delivery" },
  { label: "More energy", directive: "More energy" },
];

const VISUAL_CHIPS: Chip[] = [
  { label: "Darker aesthetic", directive: "Darker aesthetic" },
  { label: "Slower shots", directive: "Slower shots" },
  { label: "More kinetic", directive: "More kinetic" },
  { label: "Warmer palette", directive: "Warmer palette" },
];

const DEFAULT_CHIPS: Chip[] = [
  { label: "Make it tighter", directive: "Make it tighter" },
  { label: "Friendlier tone", directive: "Friendlier tone" },
  { label: "More grounded", directive: "More grounded" },
];

export function chipsForStage(stage: Stage): Chip[] {
  switch (stage) {
    case "scenario":
      return SCENARIO_CHIPS;
    case "audio":
      return AUDIO_CHIPS;
    case "visual_direction":
    case "production":
      return VISUAL_CHIPS;
    default:
      return DEFAULT_CHIPS;
  }
}

type DashboardSnapshot = {
  run_id?: string | null;
  status?: string | null;
  active_phase?: string | null;
};

function useCurrentStage(): Stage {
  const [stage, setStage] = useState<Stage>("idle");
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        try {
          const res = await fetch(`${BACKEND_URL}/dashboard/latest`);
          if (res.ok) {
            const data = (await res.json()) as DashboardSnapshot;
            const next = (data.active_phase || data.status || "idle") as Stage;
            if (!cancelled) setStage(next);
          }
        } catch {
          // ignore network errors; polling resumes
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, []);
  return stage;
}

function findSlot(
  timeline: ReturnType<typeof useOtioStream>["timeline"],
  slotId: string | null,
): OtioSlot | null {
  if (!timeline || !slotId) return null;
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      if (slot.slot_id === slotId) return slot;
    }
  }
  return null;
}

type SubmitStatus =
  | { kind: "idle" }
  | { kind: "ok"; message: string }
  | { kind: "error"; message: string };

export interface IntentBarProps {
  /** Test-only override so tests can bypass the polling stage hook. */
  stageOverride?: Stage;
}

export function IntentBar({ stageOverride }: IntentBarProps = {}) {
  const detectedStage = useCurrentStage();
  const stage = stageOverride ?? detectedStage;
  const chips = useMemo(() => chipsForStage(stage), [stage]);

  const { selectedSlotId } = useSelection();
  const { timeline } = useOtioStream();
  const selectedSlot = useMemo(
    () => findSlot(timeline, selectedSlotId),
    [timeline, selectedSlotId],
  );
  const sceneNum = selectedSlot?.scene_num ?? null;

  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [status, setStatus] = useState<SubmitStatus>({ kind: "idle" });
  const inputRef = useRef<HTMLInputElement>(null);

  // DESIGN-07 (#259): every directive opens a cost-preview dialog
  // first; the /api/directive POST only fires on confirmation.
  const [previewOpen, setPreviewOpen] = useState(false);
  const [pendingDirective, setPendingDirective] = useState<string>("");
  const [estimate, setEstimate] = useState<CostEstimate | null>(null);
  const [estimateLoading, setEstimateLoading] = useState(false);
  // Monotonic guard so a stale estimate can't land on a newer preview
  // (e.g. reviewer cancels chip A, clicks chip B before A resolves).
  const estimateFetchIdRef = useRef(0);

  const placeholder = sceneNum != null
    ? `Say something about Scene ${sceneNum}`
    : "Say something about the whole film";

  const scopeDescription = sceneNum != null
    ? `Applies to Scene ${sceneNum}.`
    : "Applies to the whole film.";

  const executeSubmit = useCallback(
    async (directive: string) => {
      const text = directive.trim();
      if (!text || submitting) return;
      setSubmitting(true);
      setStatus({ kind: "idle" });
      try {
        const slot_context = sceneNum != null
          ? {
              scope: "scene" as const,
              scope_ref: String(sceneNum),
              scene_num: sceneNum,
            }
          : null;
        const res = await fetch(`${BACKEND_URL}/api/directive`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            directive: text,
            slot_context,
            reviewer: "dashboard-user",
          }),
        });
        if (!res.ok) {
          const body = await res.text().catch(() => "");
          setStatus({
            kind: "error",
            message:
              res.status === 422
                ? "Couldn't make sense of that — try different words."
                : body.slice(0, 200) || `Request failed (${res.status}).`,
          });
          return;
        }
        setStatus({
          kind: "ok",
          message: sceneNum != null
            ? `Got it — updating Scene ${sceneNum}.`
            : "Got it — updating the whole film.",
        });
        setValue("");
        setShowSuggestions(false);
        setPreviewOpen(false);
      } catch (err) {
        setStatus({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setSubmitting(false);
      }
    },
    [submitting, sceneNum],
  );

  const openPreview = useCallback(
    (directive: string) => {
      const text = directive.trim();
      if (!text || submitting) return;
      setPendingDirective(text);
      setEstimate(null);
      setEstimateLoading(true);
      setPreviewOpen(true);
      setStatus({ kind: "idle" });
      const myId = ++estimateFetchIdRef.current;
      const slot_context = sceneNum != null
        ? {
            scope: "scene" as const,
            scope_ref: String(sceneNum),
            scene_num: sceneNum,
          }
        : null;
      void fetchDirectiveEstimate(
        { directive: text, slot_context },
        { backendUrl: BACKEND_URL },
      ).then((est) => {
        // Drop stale responses so a cancelled-then-reopened preview
        // never renders numbers that belong to a previous directive.
        if (estimateFetchIdRef.current !== myId) return;
        setEstimate(est);
        setEstimateLoading(false);
      });
    },
    [submitting, sceneNum],
  );

  const onChipClick = useCallback(
    (chip: Chip) => {
      setValue(chip.directive);
      openPreview(chip.directive);
    },
    [openPreview],
  );

  const onFormSubmit = useCallback(
    (ev: React.FormEvent<HTMLFormElement>) => {
      ev.preventDefault();
      openPreview(value);
    },
    [openPreview, value],
  );

  const confirmSubmit = useCallback(() => {
    void executeSubmit(pendingDirective);
  }, [executeSubmit, pendingDirective]);

  const previewTitle = sceneNum != null
    ? `Apply to Scene ${sceneNum}`
    : "Apply to the whole film";
  const previewDescription = `"${pendingDirective}" — ${scopeDescription}`;

  return (
    <TooltipProvider delayDuration={250}>
      <div
        className="flex flex-col gap-2 border-t bg-card p-3"
        data-testid="intent-bar"
      >
        <div className="flex flex-wrap items-center gap-2">
          {chips.map((chip) => (
            <Tooltip key={chip.label}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => onChipClick(chip)}
                  disabled={submitting}
                  className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs transition-colors hover:bg-muted disabled:opacity-50"
                  data-testid={`intent-chip-${chip.label.toLowerCase().replace(/\s+/g, "-")}`}
                >
                  {chip.label}
                </button>
              </TooltipTrigger>
              <TooltipContent className="text-xs">
                Suggestion · {scopeDescription}
              </TooltipContent>
            </Tooltip>
          ))}
          <Badge variant="outline" className="ml-auto text-[10px] font-normal">
            {scopeDescription}
          </Badge>
        </div>
        <form onSubmit={onFormSubmit} className="relative flex items-center gap-2">
          <div className="relative flex-1">
            <Input
              ref={inputRef}
              type="text"
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                setShowSuggestions(e.target.value.trim().length > 0);
              }}
              onFocus={() => {
                if (value.trim().length > 0) setShowSuggestions(true);
              }}
              onBlur={() => {
                // Defer so click on suggestion still fires
                window.setTimeout(() => setShowSuggestions(false), 120);
              }}
              placeholder={placeholder}
              disabled={submitting}
              data-testid="intent-input"
              aria-label={placeholder}
            />
            {showSuggestions && (
              <div
                className="absolute bottom-full left-0 right-0 z-20 mb-2 rounded-md border border-border bg-popover text-popover-foreground shadow-md"
                data-testid="intent-suggestions"
              >
                <Command shouldFilter>
                  <CommandList>
                    <CommandEmpty>No matching suggestion.</CommandEmpty>
                    <CommandGroup heading="Suggestions">
                      {chips.map((chip) => (
                        <CommandItem
                          key={chip.label}
                          value={chip.directive}
                          onSelect={() => onChipClick(chip)}
                          data-testid={`intent-suggestion-${chip.label.toLowerCase().replace(/\s+/g, "-")}`}
                        >
                          {chip.label}
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>
                </Command>
              </div>
            )}
          </div>
          <Button
            type="submit"
            disabled={submitting || !value.trim()}
            data-testid="intent-submit"
          >
            {submitting ? "Sending…" : "Send"}
          </Button>
        </form>
        {status.kind === "ok" && (
          <p
            className="text-xs text-emerald-700 dark:text-emerald-300"
            data-testid="intent-status-ok"
            role="status"
          >
            {status.message}
          </p>
        )}
        {status.kind === "error" && (
          <p
            className="text-xs text-destructive"
            data-testid="intent-status-error"
            role="alert"
          >
            {status.message}
          </p>
        )}
      </div>
      <CostPreviewDialog
        open={previewOpen}
        onOpenChange={(next) => {
          // Cancelling invalidates the in-flight estimate so its
          // response can't leak into a later preview.
          if (!next) estimateFetchIdRef.current += 1;
          setPreviewOpen(next);
        }}
        title={previewTitle}
        description={previewDescription}
        estimate={estimate}
        loading={estimateLoading}
        onConfirm={confirmSubmit}
        confirmLabel="Continue"
        cancelLabel="Cancel"
        submitting={submitting}
        dataTestId="intent-cost-preview"
      />
    </TooltipProvider>
  );
}
