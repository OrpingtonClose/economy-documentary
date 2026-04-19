/**
 * UI-01c (#195) -- chat-narrator token protocol.
 *
 * The backend narrator (`server/agents/chat_narrator.py`) serialises
 * clickable references to slots and previews as inline tokens:
 *
 *   [[slot:SLOT_ID]]
 *   [[preview:BOUNDARY]]
 *
 * `parseChatTokens` splits an assistant chat turn into a sequence of
 * plain-text segments and chip segments so the renderer can inline
 * clickable pills while keeping the surrounding prose intact.
 *
 * Round-trip guarantees (mirrored by the Python tests):
 *
 *   - `slot_token("A1:3:0")` -> `[[slot:A1:3:0]]` ->
 *      `{kind: "slot", id: "A1:3:0"}`.
 *   - Malformed tokens (missing closing `]]`, empty id) are ignored and
 *     surfaced as plain text.
 */

export type ChatSlotSegment = {
  kind: "slot";
  id: string;
  /** The raw token (e.g. `[[slot:A1:3:0]]`) for copy/paste round-trip. */
  raw: string;
};

export type ChatPreviewSegment = {
  kind: "preview";
  boundary: string;
  raw: string;
};

export type ChatTextSegment = {
  kind: "text";
  text: string;
};

export type ChatSegment =
  | ChatTextSegment
  | ChatSlotSegment
  | ChatPreviewSegment;

/**
 * Matches a balanced `[[kind:payload]]` token.  `payload` is any run of
 * characters that isn't the closing `]]` so ids with colons/hyphens
 * (`A1:3:0`, `audio-scene-1`) round-trip intact.
 *
 * The leading `[[` is intentional so we can reject a stray single `[`.
 */
const TOKEN_RE = /\[\[(slot|preview):([^\]]+)\]\]/g;

/**
 * Parse a narrator-rendered chat turn into printable segments.
 *
 * A malformed token (e.g. `[[slot:]]` with an empty id, or `[[slot:foo`
 * missing the closing `]]`) is left as plain text so the reviewer
 * always sees the original characters.
 */
export function parseChatTokens(input: string): ChatSegment[] {
  if (!input) return [];
  const segments: ChatSegment[] = [];
  let lastIndex = 0;
  // Use a fresh regex per call -- `TOKEN_RE` is module-scope with the
  // /g flag so its `lastIndex` is stateful between calls; reset it.
  TOKEN_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = TOKEN_RE.exec(input)) !== null) {
    const [raw, kind, payload] = match;
    const id = payload.trim();
    if (!id) {
      // `[[slot:]]` / `[[preview:]]` -- malformed, leave the literal
      // characters in place as plain text so the reviewer can see the
      // broken token rather than a silently dropped chip.
      continue;
    }
    if (match.index > lastIndex) {
      segments.push({ kind: "text", text: input.slice(lastIndex, match.index) });
    }
    if (kind === "slot") {
      segments.push({ kind: "slot", id, raw });
    } else if (kind === "preview") {
      segments.push({ kind: "preview", boundary: id, raw });
    }
    lastIndex = match.index + raw.length;
  }
  if (lastIndex < input.length) {
    segments.push({ kind: "text", text: input.slice(lastIndex) });
  }
  // All-plain-text input still returns a single text segment so callers
  // can iterate uniformly.
  if (segments.length === 0) {
    segments.push({ kind: "text", text: input });
  }
  return segments;
}

/**
 * Round-trip helpers so callers never construct raw strings and so the
 * parser + renderer stay in sync with the backend serialiser.
 */
export const SLOT_TOKEN_PREFIX = "[[slot:";
export const PREVIEW_TOKEN_PREFIX = "[[preview:";
export const TOKEN_SUFFIX = "]]";

export function slotToken(slotId: string): string {
  return `${SLOT_TOKEN_PREFIX}${slotId}${TOKEN_SUFFIX}`;
}

export function previewToken(boundary: string): string {
  return `${PREVIEW_TOKEN_PREFIX}${boundary}${TOKEN_SUFFIX}`;
}
