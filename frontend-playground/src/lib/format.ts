/**
 * Small display-only helpers shared by Server and Client Components.
 *
 * All functions here are pure and framework-agnostic so the unit
 * tests can call them without a DOM or a network.
 */

import type {
  ComponentKind,
  RunStatus,
  EvaluateStatus,
} from "./types";

/**
 * Human-friendly label for the component kind chip. Mirrors the
 * taxonomy used by the test-case atlas so the two surfaces read the
 * same.
 */
export function kindLabel(kind: ComponentKind): string {
  switch (kind) {
    case "leaf":
      return "leaf";
    case "tool":
      return "tool";
    case "loop":
      return "loop";
    case "gate":
      return "gate";
    case "graph":
      return "graph";
    default:
      return kind;
  }
}

/**
 * Tailwind class for the chip background, keyed by kind. Kept
 * alongside the label so adding a new kind requires updating both in
 * one place.
 */
export function kindChipClass(kind: ComponentKind): string {
  switch (kind) {
    case "leaf":
      return "bg-pg-surface text-pg-text";
    case "tool":
      return "bg-pg-green/10 text-pg-green";
    case "loop":
      return "bg-pg-accent/20 text-pg-accent";
    case "gate":
      return "bg-pg-amber/20 text-pg-amber";
    case "graph":
      return "bg-pg-green/20 text-pg-green";
    default:
      return "bg-pg-surface text-pg-muted";
  }
}

/**
 * Tailwind class for the run status pill. Green = OK, red = any
 * terminal failure, amber = reachable but structurally incomplete.
 * The status-to-colour mapping is deliberately collapsed: every
 * non-OK status is a failure at the same user-facing severity, but
 * ``NO_TASK_ADAPTER`` and ``MODEL_UNREACHABLE`` are infrastructure
 * failures (amber) rather than content failures (red).
 */
export function runStatusClass(status: RunStatus): string {
  switch (status) {
    case "OK":
      return "bg-pg-green/20 text-pg-green";
    case "MODEL_UNREACHABLE":
    case "NO_TASK_ADAPTER":
      return "bg-pg-amber/20 text-pg-amber";
    case "TASK_ERROR":
      return "bg-pg-red/20 text-pg-red";
    default:
      return "bg-pg-surface text-pg-muted";
  }
}

export function evaluateStatusClass(status: EvaluateStatus): string {
  switch (status) {
    case "OK":
      return "bg-pg-green/20 text-pg-green";
    case "NO_EVALUATORS":
      return "bg-pg-amber/20 text-pg-amber";
    case "EVALUATOR_ERROR":
      return "bg-pg-red/20 text-pg-red";
    default:
      return "bg-pg-surface text-pg-muted";
  }
}

/**
 * Format a number as a fixed-precision score, or return a placeholder
 * when the server sent ``null`` (evaluator exception branch).
 */
export function formatScore(value: number | null): string {
  if (value === null || Number.isNaN(value)) {
    return "—";
  }
  return value.toFixed(2);
}

/**
 * Best-effort JSON prettification. Returns the original string when
 * parsing fails so the UI can still show the raw text instead of
 * throwing.
 */
export function prettyJson(value: unknown): string {
  if (value === undefined) {
    return "";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * Parse a JSON textarea, returning either the parsed value or a
 * descriptive error message. Used by the input editor so the Run
 * button can refuse to dispatch an invalid payload instead of
 * sending unparsable text to the server.
 */
export function parseJsonSafe(
  text: string,
): { ok: true; value: unknown } | { ok: false; error: string } {
  if (text.trim() === "") {
    return { ok: true, value: undefined };
  }
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, error: message };
  }
}
