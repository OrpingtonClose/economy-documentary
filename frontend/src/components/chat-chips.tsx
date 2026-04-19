"use client";

/**
 * UI-01c (#195) -- chat chip components.
 *
 * `SlotChip` renders an inline, keyboard-activable pill that emits a
 * slot-selection action on click / Enter / Space.  `PreviewChip` is the
 * same ergonomics for previews, with a play-icon prefix to signal it
 * opens the preview modal (implemented under UI-06c).
 *
 * Both chips are designed to live inline inside prose, so they are
 * rendered as `<button>` elements (not `<a>`) to make their non-URL
 * nature explicit to assistive tech.
 */

import * as React from "react";

import { parseChatTokens } from "@/lib/chat-tokens";
import {
  dispatchPreviewOpen,
  dispatchSlotSelection,
} from "@/lib/selection-bus";

type ChipProps = {
  /** Extra classes to compose with the default chip styling. */
  className?: string;
};

export function SlotChip({
  slotId,
  className,
}: { slotId: string } & ChipProps): React.ReactElement {
  const onActivate = React.useCallback(() => {
    dispatchSlotSelection({ slotId, source: "chat-chip" });
  }, [slotId]);

  const onKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>) => {
      // Space on a button fires click on keyup, not keydown -- explicit
      // handling keeps behaviour predictable across browsers and under
      // synthetic event dispatch in tests.
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onActivate();
      }
    },
    [onActivate]
  );

  return (
    <button
      type="button"
      data-slot-chip={slotId}
      aria-label={`Select slot ${slotId}`}
      onClick={onActivate}
      onKeyDown={onKeyDown}
      className={[
        "inline-flex items-center gap-1 rounded-full",
        "px-2 py-0.5 text-xs font-medium",
        "bg-indigo-100 text-indigo-900 hover:bg-indigo-200",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500",
        "border border-indigo-200",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span aria-hidden="true">◎</span>
      <span className="font-mono">{slotId}</span>
    </button>
  );
}

export function PreviewChip({
  boundary,
  className,
}: { boundary: string } & ChipProps): React.ReactElement {
  const onActivate = React.useCallback(() => {
    dispatchPreviewOpen({ boundary, source: "chat-chip" });
  }, [boundary]);

  const onKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onActivate();
      }
    },
    [onActivate]
  );

  return (
    <button
      type="button"
      data-preview-chip={boundary}
      aria-label={`Open ${boundary} preview`}
      onClick={onActivate}
      onKeyDown={onKeyDown}
      className={[
        "inline-flex items-center gap-1 rounded-full",
        "px-2 py-0.5 text-xs font-medium",
        "bg-emerald-100 text-emerald-900 hover:bg-emerald-200",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500",
        "border border-emerald-200",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span aria-hidden="true">▶</span>
      <span>{boundary}</span>
    </button>
  );
}

/**
 * Render an assistant chat turn, inlining `[[slot:…]]` / `[[preview:…]]`
 * tokens as clickable chips.  Plain text segments preserve surrounding
 * whitespace so the reviewer sees the narrator's exact prose.
 */
export function NarratorChatText({
  text,
  className,
}: {
  text: string;
  className?: string;
}): React.ReactElement {
  const segments = React.useMemo(() => parseChatTokens(text), [text]);
  return (
    <span className={className} data-narrator-chat-text>
      {segments.map((segment, i) => {
        if (segment.kind === "slot") {
          return <SlotChip key={`slot-${i}-${segment.id}`} slotId={segment.id} />;
        }
        if (segment.kind === "preview") {
          return (
            <PreviewChip
              key={`preview-${i}-${segment.boundary}`}
              boundary={segment.boundary}
            />
          );
        }
        return <React.Fragment key={`text-${i}`}>{segment.text}</React.Fragment>;
      })}
    </span>
  );
}
