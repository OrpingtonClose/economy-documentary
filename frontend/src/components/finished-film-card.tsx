"use client";

/**
 * UX-01 — Finished documentary card + modal player.
 *
 * Rendered at the top of the OTIO canvas when the backend reports a
 * non-null ``timeline.finished_film``.  The card is the user's
 * definitive "the movie is ready" surface — it never appears before
 * assembly completes and it never disappears once shown.
 *
 * Clicking ``▶ Watch your film`` opens a modal player; clicking the
 * download icon streams the file through the browser's default
 * save-as flow.  For dual-language runs a small language selector
 * flips the source between the primary track and each alternate.
 *
 * The component is entirely read-only — it never mutates the OTIO —
 * so it is safe against the authoritative timeline invariant.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { FinishedFilm, FinishedFilmAlternate } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export interface FinishedFilmCardProps {
  film: FinishedFilm;
}

type Track = {
  url: string;
  duration_sec: number;
  language: string;
};

function formatDuration(totalSec: number): string {
  if (!Number.isFinite(totalSec) || totalSec <= 0) return "--:--";
  const m = Math.floor(totalSec / 60);
  const s = Math.floor(totalSec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function absoluteUrl(relative: string): string {
  if (/^https?:/i.test(relative)) return relative;
  return `${BACKEND_URL}${relative}`;
}

function languageLabel(lang: string): string {
  if (!lang) return "Primary";
  return lang.toUpperCase();
}

export function FinishedFilmCard({ film }: FinishedFilmCardProps) {
  const tracks: Track[] = useMemo(() => {
    const list: Track[] = [
      {
        url: film.url,
        duration_sec: film.duration_sec,
        language: film.language,
      },
    ];
    for (const alt of film.alternates || []) {
      list.push({
        url: alt.url,
        duration_sec: alt.duration_sec,
        language: alt.language,
      });
    }
    return list;
  }, [film]);

  const [selected, setSelected] = useState<number>(0);
  const [open, setOpen] = useState(false);

  const current = tracks[Math.min(selected, tracks.length - 1)] || tracks[0];

  return (
    <div
      data-testid="finished-film-card"
      className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 mb-3"
    >
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium text-emerald-700 dark:text-emerald-300">
            Your documentary is ready
          </div>
          <div className="text-xs text-muted-foreground">
            {formatDuration(current.duration_sec)} runtime
            {tracks.length > 1 ? ` · ${tracks.length} language tracks` : ""}
          </div>
        </div>
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700"
          aria-label="Watch your film"
        >
          ▶ Watch your film
        </button>
        <a
          href={absoluteUrl(current.url)}
          download
          className="rounded-md border border-border px-2 py-1.5 text-sm hover:bg-muted"
          aria-label="Download finished film"
        >
          ⬇
        </a>
      </div>

      {tracks.length > 1 && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">Language:</span>
          {tracks.map((t, i) => (
            <button
              key={`${t.url}-${i}`}
              type="button"
              onClick={() => setSelected(i)}
              aria-pressed={i === selected}
              className={
                i === selected
                  ? "rounded bg-emerald-600 px-2 py-0.5 text-white"
                  : "rounded border border-border px-2 py-0.5 hover:bg-muted"
              }
            >
              {languageLabel(t.language)}
            </button>
          ))}
        </div>
      )}

      {open && (
        <FinishedFilmModal
          url={absoluteUrl(current.url)}
          durationSec={current.duration_sec}
          language={current.language}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

interface ModalProps {
  url: string;
  durationSec: number;
  language: string;
  onClose: () => void;
}

function FinishedFilmModal({ url, durationSec, language, onClose }: ModalProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeBtnRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="finished-film-title"
      data-testid="finished-film-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative w-[min(960px,90vw)] rounded-lg bg-background p-4 shadow-xl">
        <div className="flex items-center gap-3 pb-2">
          <div id="finished-film-title" className="flex-1 text-sm font-medium">
            Finished documentary
            {language ? ` · ${language.toUpperCase()}` : ""} · {formatDuration(durationSec)}
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-muted"
            aria-label="Close finished film player"
          >
            ✕
          </button>
        </div>
        <video
          ref={videoRef}
          src={url}
          controls
          autoPlay
          className="w-full rounded-md bg-black"
        />
      </div>
    </div>
  );
}
