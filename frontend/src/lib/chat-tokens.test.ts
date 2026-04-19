/**
 * UI-01c (#195) -- parser tests.
 *
 * Runs under Node's built-in test runner with ``--experimental-strip-types``
 * so the frontend doesn't need a bundler or dedicated test framework.
 *
 * Run from ``frontend/`` with:
 *
 *   node --test --experimental-strip-types \
 *     src/lib/chat-tokens.test.ts
 *
 * Cases mirror the acceptance criteria on issue #195:
 *
 *   * single chip
 *   * multiple chips
 *   * nested prose (text + chip + text)
 *   * malformed token ignored
 */

import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import {
  parseChatTokens,
  previewToken,
  slotToken,
  type ChatSegment,
} from "./chat-tokens.ts";

describe("parseChatTokens", () => {
  it("returns a single text segment for plain prose", () => {
    const out = parseChatTokens("hello world");
    assert.deepEqual(out, [{ kind: "text", text: "hello world" }]);
  });

  it("parses a single slot chip", () => {
    const out = parseChatTokens("[[slot:A1:3:0]]");
    assert.equal(out.length, 1);
    assert.equal(out[0].kind, "slot");
    if (out[0].kind === "slot") {
      assert.equal(out[0].id, "A1:3:0");
      assert.equal(out[0].raw, "[[slot:A1:3:0]]");
    }
  });

  it("parses a single preview chip", () => {
    const out = parseChatTokens("[[preview:scenario]] ready - 9s.");
    assert.equal(out.length, 2);
    assert.equal(out[0].kind, "preview");
    if (out[0].kind === "preview") {
      assert.equal(out[0].boundary, "scenario");
    }
    assert.deepEqual(out[1], { kind: "text", text: " ready - 9s." });
  });

  it("keeps surrounding text intact around a chip", () => {
    const out = parseChatTokens(
      "[[slot:V1:2:1]] take 2 retrying with higher denoise."
    );
    assert.equal(out.length, 2);
    assert.equal(out[0].kind, "slot");
    assert.equal(out[1].kind, "text");
    if (out[1].kind === "text") {
      assert.equal(out[1].text, " take 2 retrying with higher denoise.");
    }
  });

  it("parses multiple chips with interleaved prose", () => {
    const out = parseChatTokens(
      "Compare [[slot:A1:3:0]] against [[slot:A1:4:0]] please."
    );
    const kinds = out.map((s) => s.kind);
    assert.deepEqual(kinds, ["text", "slot", "text", "slot", "text"]);
    assert.equal((out[1] as Extract<ChatSegment, { kind: "slot" }>).id, "A1:3:0");
    assert.equal((out[3] as Extract<ChatSegment, { kind: "slot" }>).id, "A1:4:0");
  });

  it("mixes slot and preview chips in one turn", () => {
    const out = parseChatTokens(
      "[[slot:A1:3:0]] failed; see [[preview:scenario]] for context."
    );
    const kinds = out.map((s) => s.kind);
    assert.deepEqual(kinds, ["slot", "text", "preview", "text"]);
  });

  it("ignores a malformed token with empty id", () => {
    const out = parseChatTokens("[[slot:]] next line");
    // The literal characters are preserved as plain text so the reviewer
    // can see the broken token rather than a silently dropped chip.
    assert.equal(out.length, 1);
    assert.equal(out[0].kind, "text");
    if (out[0].kind === "text") {
      assert.ok(out[0].text.includes("[[slot:]]"));
    }
  });

  it("ignores a malformed token missing closing bracket", () => {
    const out = parseChatTokens("oops [[slot:A1:3:0 broken");
    assert.equal(out.length, 1);
    assert.equal(out[0].kind, "text");
    if (out[0].kind === "text") {
      assert.equal(out[0].text, "oops [[slot:A1:3:0 broken");
    }
  });

  it("does not greedily swallow across multiple tokens", () => {
    // The regex must be non-greedy so two adjacent chips are parsed as
    // two chips, not one chip whose id happens to contain ']]'.
    const out = parseChatTokens("[[slot:A1:1:0]][[slot:A1:2:0]]");
    const kinds = out.map((s) => s.kind);
    assert.deepEqual(kinds, ["slot", "slot"]);
    assert.equal(
      (out[0] as Extract<ChatSegment, { kind: "slot" }>).id,
      "A1:1:0"
    );
    assert.equal(
      (out[1] as Extract<ChatSegment, { kind: "slot" }>).id,
      "A1:2:0"
    );
  });

  it("returns empty array for empty input", () => {
    assert.deepEqual(parseChatTokens(""), []);
  });

  it("preserves unicode dashes and ellipses inside prose", () => {
    const out = parseChatTokens("Narration locked at 72.3s \u2014 within tolerance.");
    assert.equal(out.length, 1);
    if (out[0].kind === "text") {
      assert.ok(out[0].text.includes("\u2014"));
    }
  });

  it("round-trips slot ids through slotToken + parseChatTokens", () => {
    for (const id of ["A1:3:0", "V1:12:4", "audio-scene-1-block-2", "bare_id"]) {
      const text = slotToken(id);
      const out = parseChatTokens(text);
      assert.equal(out.length, 1);
      assert.equal(out[0].kind, "slot");
      if (out[0].kind === "slot") {
        assert.equal(out[0].id, id);
      }
    }
  });

  it("round-trips preview boundaries through previewToken + parseChatTokens", () => {
    for (const boundary of ["scenario", "audio", "act-1", "final_cut"]) {
      const text = previewToken(boundary);
      const out = parseChatTokens(text);
      assert.equal(out.length, 1);
      assert.equal(out[0].kind, "preview");
      if (out[0].kind === "preview") {
        assert.equal(out[0].boundary, boundary);
      }
    }
  });
});
