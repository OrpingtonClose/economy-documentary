"use client";

/**
 * UI-06c (#210) — modal preview player.
 *
 * Renders a full-screen overlay containing:
 *   - an HTML5 ``<video>`` element bound to the preview's ``file_url``,
 *   - a scrubber / seek bar with current + total timestamps,
 *   - keyboard controls: Space (play/pause), ← / → (seek ±5s), Esc (close),
 *   - accessibility affordances (``role="dialog"``, ``aria-modal``,
 *     ``aria-labelledby``) and initial focus on the close button so the
 *     Esc handler works even when the user hasn't clicked into the video.
 *
 * The component is read-only — it never mutates the OTIO — so it's safe
 * against an authoritative timeline (ARCH-F immutability invariant).
 */

import { useEffect, useRef, useState } from "react";
import type { PreviewEntry } from "@/lib/preview-stream";
import { boundaryLabel } from "@/lib/preview-stream";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export interface PreviewModalProps {
  preview: PreviewEntry;
  stale: boolean;
  onClose: () => void;
}

export function PreviewModal({ preview, stale, onClose }: PreviewModalProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(preview.durationSec || 0);
  const [playing, setPlaying] = useState(false);

  // Auto-focus close button so Esc / Space work immediately without
  // requiring an initial click on the overlay.
  useEffect(() => {
    closeBtnRef.current?.focus();
  }, []);

  // Keyboard controls attach to the whole document so the handlers
  // work regardless of which element currently owns focus — as long
  // as the modal is open.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === " " || e.code === "Space") {
        e.preventDefault();
        togglePlay();
        return;
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        seekBy(5);
        return;
      }
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        seekBy(-5);
        return;
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // We intentionally re-bind when preview changes so the handlers
    // close over the latest video element / duration.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preview.boundary, preview.revision, onClose]);

  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      void v.play();
    } else {
      v.pause();
    }
  }

  function seekBy(deltaSec: number) {
    const v = videoRef.current;
    if (!v) return;
    const next = Math.max(0, Math.min(v.duration || duration, v.currentTime + deltaSec));
    v.currentTime = next;
    setCurrentTime(next);
  }

  function onBackdropClick(e: React.MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget) onClose();
  }

  function onScrub(e: React.ChangeEvent<HTMLInputElement>) {
    const v = videoRef.current;
    const t = Number(e.target.value);
    if (v && Number.isFinite(t)) {
      v.currentTime = t;
      setCurrentTime(t);
    }
  }

  const fullUrl = absoluteUrl(preview.fileUrl);
  const titleId = "preview-modal-title";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onClick={onBackdropClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6"
      data-testid="preview-modal"
    >
      <div className="flex w-full max-w-4xl flex-col gap-3 rounded-lg border border-pipeline-blue/60 bg-pipeline-card p-4 shadow-2xl">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h2 id={titleId} className="text-lg font-semibold text-pipeline-accent">
              {boundaryLabel(preview.boundary)}
            </h2>
            <p className="text-xs text-pipeline-muted">
              {preview.status === "failed" ? (
                <span className="text-red-300">Failed: {preview.error}</span>
              ) : (
                <>
                  Rendered at {preview.renderedAt || "—"}
                  {stale && (
                    <span className="ml-2 rounded bg-amber-900/50 px-2 py-0.5 text-amber-200">
                      stale — rebuild pending
                    </span>
                  )}
                </>
              )}
            </p>
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            aria-label="Close preview"
            onClick={onClose}
            className="rounded-md border border-pipeline-blue/60 px-3 py-1 text-sm text-pipeline-muted hover:bg-pipeline-blue/20 focus:outline-none focus:ring-2 focus:ring-pipeline-accent"
          >
            Close (Esc)
          </button>
        </header>

        {preview.status === "ready" && fullUrl ? (
          <>
            <video
              ref={videoRef}
              src={fullUrl}
              className="w-full rounded-md bg-black"
              controls
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onLoadedMetadata={(e) => {
                const d = (e.currentTarget.duration || preview.durationSec) ?? 0;
                if (Number.isFinite(d)) setDuration(d);
              }}
              onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
              aria-label={`Preview video for ${boundaryLabel(preview.boundary)}`}
              data-testid="preview-modal-video"
            />
            <div className="flex items-center gap-3 text-xs text-pipeline-muted">
              <button
                type="button"
                aria-label={playing ? "Pause" : "Play"}
                onClick={togglePlay}
                className="rounded border border-pipeline-blue/60 px-2 py-1 hover:bg-pipeline-blue/20"
              >
                {playing ? "Pause" : "Play"}
              </button>
              <span className="font-mono">{formatTime(currentTime)}</span>
              <input
                type="range"
                min={0}
                max={duration || preview.durationSec || 0}
                step={0.1}
                value={currentTime}
                onChange={onScrub}
                aria-label="Preview scrubber"
                className="flex-1 accent-pipeline-accent"
              />
              <span className="font-mono">
                {formatTime(duration || preview.durationSec || 0)}
              </span>
            </div>
            <p className="text-[11px] text-pipeline-muted/80">
              Keyboard: Space play/pause · ← / → seek ±5s · Esc close.
            </p>
          </>
        ) : (
          <div className="rounded-md bg-red-950/50 p-4 text-sm text-red-200">
            {preview.status === "failed"
              ? `Preview render failed: ${preview.error || "unknown error"}`
              : "No preview available at this boundary yet."}
          </div>
        )}
      </div>
    </div>
  );
}

export function formatTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function absoluteUrl(url: string): string {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return url;
  return `${BACKEND_URL}${url.startsWith("/") ? "" : "/"}${url}`;
}
