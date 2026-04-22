/**
 * Unit tests for the display-only helpers in ``src/lib/format.ts``.
 *
 * These helpers do real work that the UI depends on — score
 * formatting, JSON parsing, kind→Tailwind class selection — so
 * keeping them pure and tested guards against drift when the
 * evaluator envelope or component taxonomy changes.
 */

import {
  evaluateStatusClass,
  formatScore,
  kindChipClass,
  kindLabel,
  parseJsonSafe,
  prettyJson,
  runStatusClass,
} from "@/lib/format";

describe("kindLabel", () => {
  it.each(["leaf", "loop", "gate", "graph"] as const)(
    "returns the literal kind token for %s",
    (kind) => {
      expect(kindLabel(kind)).toBe(kind);
    },
  );
});

describe("kindChipClass", () => {
  it("maps each kind to a distinct class", () => {
    const leaf = kindChipClass("leaf");
    const loop = kindChipClass("loop");
    const gate = kindChipClass("gate");
    const graph = kindChipClass("graph");
    expect(new Set([leaf, loop, gate, graph]).size).toBe(4);
  });
});

describe("runStatusClass", () => {
  it("uses green for OK", () => {
    expect(runStatusClass("OK")).toContain("pg-green");
  });
  it("uses amber for infrastructure failures", () => {
    // NO_TASK_ADAPTER and MODEL_UNREACHABLE are infra, not content.
    expect(runStatusClass("NO_TASK_ADAPTER")).toContain("pg-amber");
    expect(runStatusClass("MODEL_UNREACHABLE")).toContain("pg-amber");
  });
  it("uses red for TASK_ERROR", () => {
    expect(runStatusClass("TASK_ERROR")).toContain("pg-red");
  });
});

describe("evaluateStatusClass", () => {
  it("uses green for OK", () => {
    expect(evaluateStatusClass("OK")).toContain("pg-green");
  });
  it("uses amber for NO_EVALUATORS", () => {
    expect(evaluateStatusClass("NO_EVALUATORS")).toContain("pg-amber");
  });
  it("uses red for EVALUATOR_ERROR", () => {
    expect(evaluateStatusClass("EVALUATOR_ERROR")).toContain("pg-red");
  });
});

describe("formatScore", () => {
  it("renders two decimal places", () => {
    expect(formatScore(0.9)).toBe("0.90");
    expect(formatScore(1)).toBe("1.00");
  });
  it("renders a dash for null", () => {
    // The evaluator exception branch in the backend writes
    // ``mean_score: None``; the UI must not crash.
    expect(formatScore(null)).toBe("—");
  });
  it("renders a dash for NaN", () => {
    expect(formatScore(Number.NaN)).toBe("—");
  });
});

describe("prettyJson", () => {
  it("prints objects with 2-space indent", () => {
    expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
  it("returns empty string for undefined", () => {
    expect(prettyJson(undefined)).toBe("");
  });
});

describe("parseJsonSafe", () => {
  it("accepts an empty string as undefined", () => {
    // Matches the run request contract: no case + no custom_input
    // means the server picks a default.
    expect(parseJsonSafe("")).toEqual({ ok: true, value: undefined });
    expect(parseJsonSafe("   \n")).toEqual({ ok: true, value: undefined });
  });
  it("parses valid JSON", () => {
    expect(parseJsonSafe('{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
  });
  it("returns an error for invalid JSON", () => {
    const result = parseJsonSafe("not json");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toMatch(/JSON/i);
    }
  });
});
