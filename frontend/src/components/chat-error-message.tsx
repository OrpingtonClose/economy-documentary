"use client";

/**
 * UX-02 (#244) — CopilotChat ``ErrorMessage`` override that suppresses
 * the "First event must be 'RUN_STARTED'" banner on reconnect.
 *
 * Context
 * -------
 * ``@ag-ui/client`` validates the SSE event lifecycle and throws when
 * any content event arrives before a ``RUN_STARTED`` frame. On the
 * happy path the backend (``server.py::event_generator``) always emits
 * ``RUN_STARTED`` first, so this invariant holds.
 *
 * On reconnect the browser re-opens ``/api/copilotkit`` with a fresh
 * request. The backend's replay buffer forwards buffered events from
 * the last ack'd sequence; ``RUN_STARTED`` is already in the past, so
 * the client validator sees a ``TEXT_MESSAGE_*`` / ``CUSTOM_EVENT``
 * frame first and throws. The error bubbled up as a red banner that
 * confused non-technical observers even though the stream was actually
 * healthy (previous events were already rendered).
 *
 * We suppress exactly this one validation error here. Any other error
 * (network drop, backend 5xx, tool crash) still renders through the
 * stock ``DefaultErrorMessage`` component so real problems stay loud.
 * The full fix (server-side ``RunStartedEvent`` re-emit on reconnect)
 * is tracked in the parent UX-02 issue; this filter removes the noise
 * for users in the meantime.
 */

import * as React from "react";
import type { ErrorMessageProps } from "@copilotkit/react-ui";

const RUN_STARTED_ERROR_RE =
  /first event must be.*['"]?RUN_STARTED['"]?/i;

export function ChatErrorMessage(
  props: ErrorMessageProps,
): React.ReactElement | null {
  const err = props.error as unknown;
  const message = extractMessage(err);
  if (message && RUN_STARTED_ERROR_RE.test(message)) {
    return null;
  }
  // Fallback renderer — @copilotkit/react-ui does not export a default
  // ErrorMessage component we can delegate to, so we render a minimal
  // rose-tone banner that matches the CopilotKit chat palette.
  return (
    <div
      className="copilotKitMessage copilotKitErrorMessage"
      role="alert"
      data-testid="chat-error-message"
    >
      <div className="rounded border border-rose-500/60 bg-rose-900/40 px-3 py-2 text-sm text-rose-100">
        {message || "Something went wrong."}
      </div>
    </div>
  );
}

function extractMessage(err: unknown): string {
  if (!err) return "";
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message;
  if (typeof err === "object") {
    const asObj = err as Record<string, unknown>;
    if (typeof asObj.message === "string") return asObj.message;
    if (typeof asObj.error === "string") return asObj.error;
    try {
      return JSON.stringify(asObj);
    } catch {
      return "";
    }
  }
  return String(err);
}
