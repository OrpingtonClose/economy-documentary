"use client";

/**
 * ARCH-H2 — Reconciliation overlay.
 *
 * While the OTIO is still ``draft``, we float an overlay above the
 * narration track that exposes, per block:
 *
 *   scripted_duration_sec  — what the script asked for
 *   measured_duration_sec  — what WhisperX actually produced
 *   skew_sec               — measured − scripted (the diff band)
 *
 * When the OTIO crystallises (``otio_authoritative`` SSE event), the
 * parent component drops ``state`` from ``draft`` and this component is
 * simply not rendered — there is no polling, no re-fetch, and no
 * animation to unmount manually.
 */

import type { OtioReconciliationRow } from "@/lib/types";

export function ReconciliationOverlay({
  reconciliation,
  zoom,
  leftPx,
  narrationTop,
}: {
  reconciliation: OtioReconciliationRow[];
  zoom: number;
  leftPx: number;
  narrationTop: number;
}) {
  if (!reconciliation || reconciliation.length === 0) {
    return (
      <div
        className="pointer-events-none absolute rounded border border-dashed border-amber-400/40 bg-amber-900/20 px-2 py-1 text-[10px] text-amber-200"
        style={{ left: leftPx + 8, top: narrationTop - 26 }}
      >
        draft OTIO · awaiting narration reconciliation
      </div>
    );
  }

  return (
    <>
      <div
        className="pointer-events-none absolute rounded border border-amber-400/50 bg-amber-900/30 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-amber-200"
        style={{ left: leftPx + 8, top: narrationTop - 26 }}
      >
        reconciliation overlay · scripted vs measured
      </div>
      {reconciliation.map((row) => {
        const scripted = row.scripted_duration_sec;
        const measured = row.measured_duration_sec ?? scripted;
        const left = leftPx + row.start_sec * zoom;
        const scriptedWidth = Math.max(scripted * zoom, 2);
        const measuredWidth = Math.max(measured * zoom, 2);
        const width = Math.max(scriptedWidth, measuredWidth);
        const skew = row.skew_sec ?? 0;
        return (
          <div
            key={row.slot_id}
            className="pointer-events-none absolute text-[9px]"
            style={{ left, top: narrationTop - 10, width, height: 8 }}
            title={`${row.slot_id} · scripted ${scripted.toFixed(2)}s, measured ${measured.toFixed(2)}s, skew ${skew.toFixed(2)}s`}
          >
            {/* scripted bar (outline) */}
            <div
              className="absolute top-0 h-[6px] border border-amber-300/70"
              style={{ left: 0, width: scriptedWidth }}
            />
            {/* measured bar (filled), coloured by skew sign */}
            <div
              className={
                "absolute top-0 h-[6px] " +
                (Math.abs(skew) < 0.2
                  ? "bg-emerald-400/80"
                  : skew > 0
                  ? "bg-red-400/80"
                  : "bg-sky-400/80")
              }
              style={{ left: 0, width: measuredWidth }}
            />
            {Math.abs(skew) >= 0.05 && (
              <span
                className="absolute -top-3 whitespace-nowrap text-amber-100/90"
                style={{ left: Math.min(scriptedWidth, measuredWidth) }}
              >
                {skew > 0 ? "+" : ""}
                {skew.toFixed(2)}s
              </span>
            )}
          </div>
        );
      })}
    </>
  );
}
