"use client";

/**
 * DESIGN-02 (#254) — Progress Ribbon.
 *
 * Replaces the UX-08 (#250) copy-level placeholder with the designer-spec'd
 * sticky-top progress ribbon. Seven human-readable steps map 1:1 to the
 * pipeline's seven stages; the active step soft-pulses green, completed
 * steps are a solid muted green, and future steps are outline-only.
 *
 * Colour language (DESIGN-04, #256):
 *   - Nothing red unless action needed.
 *   - Amber for "needs attention", green pulse for "working now".
 *
 * All values come from the existing ``/dashboard/latest`` snapshot. Three
 * fields are treated as optional forward-compat additions and are hidden
 * gracefully when absent:
 *
 *   - ``eta_seconds``       — ETA countdown beside the stage label.
 *   - ``cost_spent_usd``    — small ``$`` meter on the right.
 *   - ``current_scene_num`` + ``total_scenes`` — ``Scene 3 of 12`` counter.
 */

import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// Seven plain-English steps, in pipeline order. Each step maps to one of
// the backend ``active_phase`` values; the label never surfaces the
// internal phase name.
const STAGES = [
  {
    id: "brief",
    label: "Understanding your brief",
    hover:
      "Reading your topic, tone and any constraints so the rest of the pipeline knows what film to make.",
  },
  {
    id: "scenario",
    label: "Writing the scenario",
    hover:
      "Drafting the scene-by-scene screenplay — the backbone every later stage depends on.",
  },
  {
    id: "audio",
    label: "Narrating the voice-over",
    hover:
      "Generating the narration audio. Real durations here set the beat for every clip that follows.",
  },
  {
    id: "visual_direction",
    label: "Imagining the visuals",
    hover:
      "Turning each narration phrase into a shot list — camera, mood, LoRA and timing.",
  },
  {
    id: "production",
    label: "Rendering clips",
    hover:
      "Running the video model to produce one clip per narrated phrase.",
  },
  {
    id: "assembly",
    label: "Stitching the film",
    hover:
      "Cutting clips, narration and music to the locked timeline and exporting the master.",
  },
  {
    id: "completed",
    label: "Final touches",
    hover:
      "Quality checks, subtitles and the last polish before the film is ready to watch.",
  },
] as const;

type StageId = (typeof STAGES)[number]["id"];

type StageState = "done" | "active" | "pending";

/** Shape of the ``/dashboard/latest`` snapshot we consume.
 *
 * ``eta_seconds``, ``cost_spent_usd``, ``current_scene_num`` and
 * ``total_scenes`` are treated as optional forward-compat fields. The
 * backend may not emit them yet — we hide the corresponding UI instead
 * of printing an "N/A" placeholder.
 */
type DashboardSnapshot = {
  run_id?: string | null;
  status?: string;
  active_phase?: string | null;
  elapsed_sec?: number | null;
  eta_sec?: number | null;
  eta_seconds?: number | null;
  cost_spent_usd?: number | null;
  current_scene_num?: number | null;
  total_scenes?: number | null;
};

export function ProgressStrip() {
  const [snap, setSnap] = useState<DashboardSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        try {
          const res = await fetch(`${BACKEND_URL}/dashboard/latest`);
          if (res.ok) {
            const data = (await res.json()) as DashboardSnapshot;
            if (!cancelled) setSnap(data);
          }
        } catch {
          // ignore; next tick retries
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, []);

  const activeStage = resolveActiveStage(snap);
  const activeIdx = STAGES.findIndex((s) => s.id === activeStage);
  const activeLabel =
    activeIdx >= 0 ? STAGES[activeIdx].label : "Waiting to start";

  // Bar fill: "how far along the whole film are we?" — one seventh per
  // step, with the active step counted as half-complete so the bar
  // animates between checkpoints.
  const progressValue = useMemo(() => {
    if (activeIdx < 0) return 0;
    const per = 100 / STAGES.length;
    return Math.min(100, Math.round(activeIdx * per + per / 2));
  }, [activeIdx]);

  const sceneCount = formatSceneCount(snap);
  const etaLabel = formatEta(snap);
  const costLabel = formatCost(snap);

  return (
    <TooltipProvider delayDuration={200}>
      <div
        className="sticky top-0 z-30 flex items-center gap-4 border-b border-pipeline-blue bg-pipeline-bg/95 px-4 py-2 text-xs backdrop-blur supports-[backdrop-filter]:bg-pipeline-bg/80"
        data-testid="progress-strip"
      >
        {/* Seven stage dots + connectors. Tooltip on each dot so hovering
          * surfaces the plain-English description without crowding the
          * ribbon itself. */}
        <ol
          className="flex items-center gap-1.5"
          aria-label="Pipeline progress"
        >
          {STAGES.map((stage, idx) => {
            const state: StageState =
              activeIdx < 0
                ? "pending"
                : idx < activeIdx
                  ? "done"
                  : idx === activeIdx
                    ? "active"
                    : "pending";
            return (
              <li
                key={stage.id}
                className="flex items-center gap-1.5"
                data-stage={stage.id}
                data-state={state}
              >
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span
                      aria-label={`${stage.label} — ${state}`}
                      className={
                        "h-2.5 w-2.5 rounded-full border transition-colors " +
                        DOT_CLASSES[state]
                      }
                    />
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-xs">
                    <p className="font-semibold">{stage.label}</p>
                    <p className="opacity-80">{stage.hover}</p>
                  </TooltipContent>
                </Tooltip>
                {idx < STAGES.length - 1 && (
                  <span
                    aria-hidden="true"
                    className={
                      "h-px w-4 " +
                      (idx < activeIdx
                        ? "bg-emerald-500/60"
                        : "bg-pipeline-blue/60")
                    }
                  />
                )}
              </li>
            );
          })}
        </ol>

        {/* Stage label + optional scene count + optional ETA. The whole
          * cluster is a HoverCard so that a primary viewer can hover
          * for a one-line plain-English recap of the current step. */}
        <HoverCard openDelay={150} closeDelay={80}>
          <HoverCardTrigger asChild>
            <div
              className="flex min-w-0 items-center gap-2 text-pipeline-text"
              data-testid="progress-strip-stage-label"
            >
              <span className="truncate font-medium">{activeLabel}</span>
              {sceneCount && (
                <Badge
                  variant="outline"
                  className="border-pipeline-blue/60 text-pipeline-muted"
                  data-testid="progress-strip-scene-count"
                >
                  {sceneCount}
                </Badge>
              )}
              {etaLabel && (
                <span
                  className="text-pipeline-muted"
                  data-testid="progress-strip-eta"
                >
                  · {etaLabel}
                </span>
              )}
            </div>
          </HoverCardTrigger>
          <HoverCardContent side="bottom" align="start" className="w-72">
            <p className="text-sm font-semibold">{activeLabel}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {activeIdx >= 0
                ? STAGES[activeIdx].hover
                : "The pipeline is idle. Start a new run from the chat on the left."}
            </p>
          </HoverCardContent>
        </HoverCard>

        {/* The bar visually backs up the dots — "how far are we through
          * the whole film?". It tops out at 100% when the run ends. */}
        <Progress
          value={progressValue}
          className="h-1.5 flex-1 bg-pipeline-blue/40"
          data-testid="progress-strip-bar"
          aria-label="Overall pipeline progress"
        />

        {/* $ meter is the only surface that surfaces backend cost. It is
          * forward-compat: backends that do not emit ``cost_spent_usd``
          * simply do not render the meter. */}
        {costLabel && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge
                variant="secondary"
                className="shrink-0 bg-pipeline-card text-pipeline-text"
                data-testid="progress-strip-cost"
              >
                {costLabel}
              </Badge>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              Money spent on this run so far.
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </TooltipProvider>
  );
}

// ---------------------------------------------------------------------------
// Colour language
// ---------------------------------------------------------------------------

/** State-to-class map for the seven dots. DESIGN-04 hard constraint:
 *
 *   - Nothing red unless action needed.
 *   - Amber for "needs attention", green pulse for "working now".
 *
 * Past → solid muted green. Active → soft green pulse. Future → outline. */
const DOT_CLASSES: Record<StageState, string> = {
  done: "bg-emerald-500/80 border-emerald-400",
  active:
    "bg-emerald-400/80 border-emerald-300 animate-pulse shadow-[0_0_8px_rgba(52,211,153,0.6)]",
  pending: "bg-transparent border-pipeline-blue/60",
};

// ---------------------------------------------------------------------------
// Snapshot helpers
// ---------------------------------------------------------------------------

function resolveActiveStage(snap: DashboardSnapshot | null): StageId | null {
  if (!snap) return null;
  if (snap.status === "completed") return "completed";
  // ``run_id`` is only set once a run is in-flight. Before that, treat the
  // pipeline as still reading the brief so viewers see something already lit.
  if (!snap.run_id) return "brief";
  const phase = snap.active_phase;
  if (!phase || phase === "idle") return "brief";
  const match = STAGES.find((s) => s.id === phase);
  return match ? match.id : null;
}

function formatSceneCount(snap: DashboardSnapshot | null): string | null {
  if (!snap) return null;
  const current = snap.current_scene_num;
  const total = snap.total_scenes;
  if (typeof current !== "number" || typeof total !== "number") return null;
  if (total <= 0) return null;
  return `Scene ${current} of ${total}`;
}

function formatEta(snap: DashboardSnapshot | null): string | null {
  if (!snap) return null;
  if (snap.status === "completed") return "done";
  // Prefer the newer ``eta_seconds`` name from DESIGN-02 spec; fall back to
  // the legacy ``eta_sec`` field the placeholder was wired to.
  const eta =
    typeof snap.eta_seconds === "number"
      ? snap.eta_seconds
      : typeof snap.eta_sec === "number"
        ? snap.eta_sec
        : null;
  if (eta === null || eta <= 0) return null;
  const secs = Math.round(eta);
  if (secs < 60) return `${secs}s left`;
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  return rem === 0 ? `${mins}m left` : `${mins}m ${rem}s left`;
}

function formatCost(snap: DashboardSnapshot | null): string | null {
  if (!snap) return null;
  const cost = snap.cost_spent_usd;
  if (typeof cost !== "number" || cost < 0) return null;
  if (cost < 0.01) return "$0.00";
  if (cost < 10) return `$${cost.toFixed(2)}`;
  return `$${cost.toFixed(0)}`;
}
