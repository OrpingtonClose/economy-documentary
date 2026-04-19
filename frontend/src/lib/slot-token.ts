/**
 * UI-02 (#187) — shared `[[slot:ID]]` token grammar.
 *
 * The narrator in chat (UI-01c, #186) emits plain-English one-liners
 * with inline references to OTIO slots using the token:
 *
 *     "Scene 3 narration failed visual QA — re-run? [[slot:scene3_narr]]"
 *
 * The chat parser (UI-01c) walks each message, splits out the tokens,
 * and renders `<SlotChip />` in place. This module is the shared
 * grammar — it is imported by the parser *and* by component tests so
 * both sides agree on the format.
 *
 * The slot id grammar matches OTIO slot ids (same ids served by
 * `/agui/slots/{slot_id}/detail` and used as `slot_context` for the
 * directive API).
 */

/**
 * Matches `[[slot:<id>]]` with an id consisting of letters, digits,
 * underscores, dots, and hyphens. Intentionally excludes whitespace
 * and the closing `]]` — malformed tokens are left as literal text.
 */
export const SLOT_TOKEN_PATTERN = /\[\[slot:([A-Za-z0-9_.\-]+)\]\]/g;

export type SlotTokenSegment =
  | { type: "text"; value: string }
  | { type: "slot"; slotId: string; raw: string };

/**
 * Split a string into an ordered list of text + slot-token segments.
 * Preserves every character of the input exactly — concatenating the
 * segments back yields the original string.
 *
 * Empty input yields `[]`. Input with no slot tokens yields a single
 * text segment. The regex is instantiated per call so this function
 * is safe to use across threads / async boundaries.
 */
export function parseSlotTokens(source: string): SlotTokenSegment[] {
  if (!source) return [];
  const out: SlotTokenSegment[] = [];
  // Clone the pattern so we don't mutate the shared /g lastIndex.
  const re = new RegExp(SLOT_TOKEN_PATTERN.source, "g");
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(source)) !== null) {
    if (match.index > cursor) {
      out.push({ type: "text", value: source.slice(cursor, match.index) });
    }
    out.push({ type: "slot", slotId: match[1], raw: match[0] });
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) {
    out.push({ type: "text", value: source.slice(cursor) });
  }
  return out;
}

/** Convenience: returns only the slot ids referenced in a message. */
export function extractSlotIds(source: string): string[] {
  const out: string[] = [];
  for (const seg of parseSlotTokens(source)) {
    if (seg.type === "slot") out.push(seg.slotId);
  }
  return out;
}

/** Build the inverse: wrap an id in the canonical `[[slot:ID]]` form. */
export function formatSlotToken(slotId: string): string {
  return `[[slot:${slotId}]]`;
}
