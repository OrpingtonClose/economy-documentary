import {
  extractSlotIds,
  formatSlotToken,
  parseSlotTokens,
} from "@/lib/slot-token";

describe("UI-02 slot token grammar", () => {
  test("parses a single token", () => {
    const segs = parseSlotTokens("Re-run [[slot:scene3_narr]]?");
    expect(segs).toEqual([
      { type: "text", value: "Re-run " },
      { type: "slot", slotId: "scene3_narr", raw: "[[slot:scene3_narr]]" },
      { type: "text", value: "?" },
    ]);
  });

  test("parses multiple tokens in one message", () => {
    const segs = parseSlotTokens(
      "Scene 1 [[slot:scene1_vid]] failed, rerunning as [[slot:scene1_vid_v2]].",
    );
    expect(segs.filter((s) => s.type === "slot")).toHaveLength(2);
    expect(extractSlotIds("a [[slot:a]] b [[slot:b]] c")).toEqual(["a", "b"]);
  });

  test("ignores malformed tokens", () => {
    const segs = parseSlotTokens("no [[ slot :x ]] token here");
    expect(segs).toEqual([
      { type: "text", value: "no [[ slot :x ]] token here" },
    ]);
  });

  test("empty input yields empty array", () => {
    expect(parseSlotTokens("")).toEqual([]);
  });

  test("accepts ids with digits, underscores, dots, hyphens", () => {
    expect(extractSlotIds("[[slot:s-3.narr_v2]]")).toEqual(["s-3.narr_v2"]);
  });

  test("round-trip with formatSlotToken", () => {
    const id = "scene3_narr";
    expect(formatSlotToken(id)).toBe("[[slot:scene3_narr]]");
    expect(extractSlotIds(formatSlotToken(id))).toEqual([id]);
  });

  test("segments reconstruct original string", () => {
    const raw = "a [[slot:x]] b [[slot:y]] c";
    const joined = parseSlotTokens(raw)
      .map((s) => (s.type === "text" ? s.value : s.raw))
      .join("");
    expect(joined).toBe(raw);
  });
});
