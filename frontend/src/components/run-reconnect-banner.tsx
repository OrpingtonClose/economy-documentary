"use client";

/**
 * UI-07c — banner that explains a reconnect/overflow to the user.
 *
 * Visibility rules:
 *  - "overflow": ring buffer evicted events the client hadn't seen yet,
 *    the snapshot has been refetched so state is still consistent but
 *    individual events between last-id and buffer tail are lost.
 *  - "replaying": we're actively draining the buffer (transient).
 *  - "unknown-run": URL referenced a run id the server no longer knows;
 *    URL has been cleared.
 *  - "error": replay fetch failed (network, 5xx, etc.) — surfaces so
 *    the user knows reconnect didn't complete.
 */

import type { RunSession } from "@/lib/run-session";

export function RunReconnectBanner({
  session,
}: {
  session: Pick<RunSession, "status" | "overflowMessage" | "dismissOverflow" | "runId">;
}) {
  const { status, overflowMessage, dismissOverflow, runId } = session;

  if (status === "idle" || status === "ready" || status === "probing") {
    return null;
  }

  let tone: "amber" | "rose" | "sky" = "sky";
  let text = "";

  if (status === "overflow" && overflowMessage) {
    tone = "amber";
    text = overflowMessage;
  } else if (status === "replaying") {
    tone = "sky";
    text = runId
      ? `Reconnecting to ${runId} — replaying buffered events…`
      : "Reconnecting — replaying buffered events…";
  } else if (status === "unknown-run") {
    tone = "rose";
    text =
      "The run id in your URL is no longer on the server. Submit a new topic to start a new run.";
  } else if (status === "error") {
    tone = "rose";
    text =
      "Couldn't reconnect to the pipeline run. The timeline snapshot may still be accurate, but live replay is disabled.";
  }

  if (!text) return null;

  const toneClass =
    tone === "amber"
      ? "bg-amber-900/60 border-amber-600/60 text-amber-100"
      : tone === "rose"
        ? "bg-rose-900/60 border-rose-600/60 text-rose-100"
        : "bg-sky-900/60 border-sky-600/60 text-sky-100";

  return (
    <div
      role="status"
      className={`flex items-center justify-between gap-3 border-b px-4 py-2 text-xs ${toneClass}`}
    >
      <span className="flex-1">{text}</span>
      {status === "overflow" && (
        <button
          type="button"
          onClick={dismissOverflow}
          className="rounded border border-current/40 px-2 py-0.5 text-xs hover:bg-white/10"
        >
          Dismiss
        </button>
      )}
    </div>
  );
}
