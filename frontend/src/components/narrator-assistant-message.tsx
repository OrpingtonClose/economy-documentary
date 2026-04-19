"use client";

/**
 * UI-01c (#195) -- CopilotChat assistant-message wrapper.
 *
 * Detects narrator chip tokens (`[[slot:…]]`, `[[preview:…]]`) emitted
 * by the backend (`agents/chat_narrator.py`) and renders them as
 * clickable pills via :mod:`./chat-chips`.  Falls back to the stock
 * Markdown renderer for any content without tokens so normal agent
 * prose keeps its existing formatting (code blocks, lists, etc.).
 */

import * as React from "react";
import { AssistantMessage as DefaultAssistantMessage } from "@copilotkit/react-ui";
import type { AssistantMessageProps } from "@copilotkit/react-ui";

import { NarratorChatText } from "@/components/chat-chips";

const NARRATOR_TOKEN_RE = /\[\[(slot|preview):[^\]]+\]\]/;

/**
 * A narrator turn is a short one-liner containing at least one chip
 * token.  Long assistant replies with incidental double brackets (e.g.
 * a code block with `[[` unrelated to chips) still fall back to the
 * stock renderer thanks to the `^` / `$` anchors not being required --
 * any line with a valid chip token uses the chip renderer.
 */
function looksLikeNarratorTurn(content: string): boolean {
  return NARRATOR_TOKEN_RE.test(content);
}

export function NarratorAssistantMessage(
  props: AssistantMessageProps
): React.ReactElement {
  const content = props.message?.content ?? "";
  if (content && looksLikeNarratorTurn(content)) {
    return (
      <div
        className="copilotKitMessage copilotKitAssistantMessage"
        data-narrator-turn
      >
        <NarratorChatText text={content} />
      </div>
    );
  }
  return <DefaultAssistantMessage {...props} />;
}
