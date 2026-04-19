"use client";

/**
 * UX-04 (#246) — render the submitted brief as a plain chat bubble.
 *
 * CopilotKit's default ``UserMessage`` component wraps the user turn in
 * a panel whose chrome (card border, meta strip) made the *first* user
 * message — the documentary brief — look like a detached "brief card"
 * floating above the rest of the log. The reviewer feedback on #246
 * was: "the brief is a chat message, render it like one".
 *
 * We override ``UserMessage`` with a minimal bubble that:
 *
 *   * uses the same ``copilotKitMessage`` / ``copilotKitUserMessage``
 *     class names as the default so existing CSS (alignment, spacing,
 *     scroll-anchor behaviour) continues to apply;
 *   * drops the card chrome so the brief reads as an inline turn;
 *   * preserves multiline formatting via ``whitespace-pre-wrap``.
 */

import * as React from "react";
import type { UserMessageProps } from "@copilotkit/react-ui";

export function BriefUserMessage(
  props: UserMessageProps,
): React.ReactElement | null {
  const raw = props.message?.content ?? "";
  const text =
    typeof raw === "string"
      ? raw
      : Array.isArray(raw)
      ? raw
          .map((p) => (typeof p === "string" ? p : (p as { text?: string })?.text ?? ""))
          .join(" ")
      : String(raw ?? "");
  if (!text.trim()) return null;
  return (
    <div
      className="copilotKitMessage copilotKitUserMessage"
      data-testid="brief-user-message"
    >
      <div className="whitespace-pre-wrap break-words text-sm">{text}</div>
    </div>
  );
}
