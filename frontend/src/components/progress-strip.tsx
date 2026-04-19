"use client";

/**
 * UX-08 (#250) — Progress strip placeholder.
 *
 * A top-of-dashboard strip showing the seven pipeline stages as dots,
 * the current-stage label, and an ETA (``estimating`` when unavailable).
 *
 * This is a *copy-level* placeholder. The real visual ribbon is scoped
 * to DESIGN-02 (#254); landing that issue will replace this component
 * wholesale with the designer-spec'd ribbon, so this implementation
 * intentionally stays small:
 *
 *   * No new deps — Tailwind + a ``fetch`` against ``/dashboard/latest``.
 *   * No new SSE channels — poll the existing dashboard snapshot at 2 s.
 *   * No attempt at a precise ETA — we surface ``elapsed_sec`` and fall
 *     back to ``estimating`` when the backend has not produced one yet.
 */

import { useEffect, useState } from "react";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// Seven stages mirrors the pipeline's public surface: brief (intake) →
// scenario → audio → visual_direction → production → assembly →
// completed. Matches the existing ``PipelinePhase`` union in types.ts.
const STAGE_ORDER = [
  { id: "brief", label: "Brief" },
  { id: "scenario", label: "Scenario" },
  { id: "audio", label: "Audio" },
  { id: "visual_direction", label: "Visuals" },
  { id: "production", label: "Production" },
  { id: "assembly", label: "Assembly" },
  { id: "completed", label: "Completed" },
] as const;

type StageId = (typeof STAGE_ORDER)[number]["id"];

type DashboardSnapshot = {
  run_id?: string | null;
  status?: string;
  active_phase?: string | null;
  elapsed_sec?: number | null;
  eta_sec?: number | null;
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
  const activeIdx = STAGE_ORDER.findIndex((s) => s.id === activeStage);
  const activeLabel =
    activeIdx >= 0 ? STAGE_ORDER[activeIdx].label : "Waiting to start";
  const etaLabel = formatEta(snap);

  return (
    <div
      className="flex items-center justify-between gap-3 border-b border-pipeline-blue bg-pipeline-bg px-4 py-2 text-xs"
      data-testid="progress-strip"
    >
      <ol className="flex items-center gap-2" aria-label="Pipeline progress">
        {STAGE_ORDER.map((stage, idx) => {
          const state: "done" | "active" | "pending" =
            activeIdx < 0
              ? "pending"
              : idx < activeIdx
              ? "done"
              : idx === activeIdx
              ? "active"
              : "pending";
          const dotClass =
            state === "done"
              ? "bg-emerald-500/80 border-emerald-400"
              : state === "active"
              ? "bg-pipeline-accent border-pipeline-accent animate-pulse"
              : "bg-pipeline-bg border-pipeline-blue/60";
          return (
            <li
              key={stage.id}
              className="flex items-center gap-2"
              title={stage.label}
              data-stage={stage.id}
              data-state={state}
            >
              <span
                aria-hidden="true"
                className={`h-2.5 w-2.5 rounded-full border ${dotClass}`}
              />
              {idx < STAGE_ORDER.length - 1 && (
                <span
                  aria-hidden="true"
                  className="h-px w-4 bg-pipeline-blue/60"
                />
              )}
            </li>
          );
        })}
      </ol>
      <div className="flex items-center gap-3 text-pipeline-muted">
        <span data-testid="progress-strip-stage-label">
          <span className="text-pipeline-muted/70">Stage:</span>{" "}
          <span className="text-pipeline-text">{activeLabel}</span>
        </span>
        <span data-testid="progress-strip-eta">
          <span className="text-pipeline-muted/70">ETA:</span>{" "}
          <span className="text-pipeline-text">{etaLabel}</span>
        </span>
      </div>
    </div>
  );
}

function resolveActiveStage(snap: DashboardSnapshot | null): StageId | null {
  if (!snap || !snap.run_id) return null;
  if (snap.status === "completed") return "completed";
  const phase = snap.active_phase;
  if (!phase) return null;
  const match = STAGE_ORDER.find((s) => s.id === phase);
  return match ? match.id : null;
}

function formatEta(snap: DashboardSnapshot | null): string {
  if (!snap || !snap.run_id) return "—";
  if (snap.status === "completed") return "done";
  if (typeof snap.eta_sec === "number" && snap.eta_sec > 0) {
    return `~${Math.round(snap.eta_sec)}s`;
  }
  return "estimating…";
}
