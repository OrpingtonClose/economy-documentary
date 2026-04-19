"use client";

/**
 * UI-06 (#191) — preview chips strip.
 *
 * A compact row of ▶ chips that surfaces the same preview entries as
 * the OTIO timeline ribbon (UI-06b #209), but placed next to the chat
 * so the observer always sees "a preview just rendered" without
 * having to be on the OTIO tab.  Clicking a chip opens the same modal
 * player used by the timeline (UI-06c #210).
 *
 * The parent dashboard renders the chip strip at the top of the chat
 * pane; preview events arrive on the shared ``/agui/stream``
 * connection, so no extra SSE channel is opened (ARCH-H invariant).
 */

import { useState } from "react";
import {
  usePreviewStream,
  boundaryLabel,
  isPreviewStale,
} from "@/lib/preview-stream";
import { PreviewModal } from "@/components/preview-modal";

export function PreviewChips() {
  const { state } = usePreviewStream();
  const [openBoundary, setOpenBoundary] = useState<string | null>(null);
  const entries = Object.values(state.entries);
  if (entries.length === 0) return null;

  // Sort: ready first, then stale, then failed — within each group
  // newest renderedAtMs on top so the most interesting chip is first.
  const sorted = [...entries].sort((a, b) => {
    const rank = (x: typeof a) =>
      x.status === "failed" ? 2 : isPreviewStale(x, state) ? 1 : 0;
    const ra = rank(a);
    const rb = rank(b);
    if (ra !== rb) return ra - rb;
    return b.renderedAtMs - a.renderedAtMs;
  });

  const openPreview = openBoundary ? state.entries[openBoundary] : null;

  return (
    <div
      data-testid="preview-chips"
      className="flex flex-wrap items-center gap-2 border-b border-pipeline-blue bg-pipeline-card/70 px-3 py-2"
      aria-label="Rendered previews"
    >
      <span className="text-[11px] uppercase tracking-wider text-pipeline-muted">
        Previews
      </span>
      {sorted.map((p) => {
        const stale = isPreviewStale(p, state);
        const failed = p.status === "failed";
        return (
          <button
            key={p.boundary}
            type="button"
            onClick={() => setOpenBoundary(p.boundary)}
            title={`${boundaryLabel(p.boundary)} — ${
              failed ? "failed" : stale ? "stale" : "ready"
            }`}
            aria-label={`Open preview: ${boundaryLabel(p.boundary)}${
              failed ? " (failed)" : stale ? " (stale)" : ""
            }`}
            data-testid={`preview-chip-${p.boundary}`}
            data-stale={stale ? "true" : "false"}
            data-status={p.status}
            className={
              "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium shadow " +
              (failed
                ? "border-red-500/70 bg-red-900/60 text-red-100 hover:bg-red-900/80"
                : stale
                  ? "border-amber-500/70 bg-amber-900/50 text-amber-100 hover:bg-amber-900/70"
                  : "border-emerald-500/70 bg-emerald-900/60 text-emerald-100 hover:bg-emerald-900/80")
            }
          >
            <span aria-hidden="true">{failed ? "⚠" : "▶"}</span>
            <span className="whitespace-nowrap">
              {boundaryLabel(p.boundary)}
            </span>
          </button>
        );
      })}

      {openPreview && (
        <PreviewModal
          preview={openPreview}
          stale={isPreviewStale(openPreview, state)}
          onClose={() => setOpenBoundary(null)}
        />
      )}
    </div>
  );
}
