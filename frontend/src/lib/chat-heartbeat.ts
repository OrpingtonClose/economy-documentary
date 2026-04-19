/**
 * DESIGN-01 (#253) — chat heartbeat throttle primitives.
 *
 * Pure helpers extracted so the throttle logic can be unit-tested
 * without spinning up the React component (the component layer is
 * covered by a separate behavioural test that drives the same
 * function under jest fake timers).
 *
 * The rule mirrors the DESIGN-01 acceptance criterion: during an
 * active stage the chat must not go silent for more than 60 s, but
 * each steady-state heartbeat should arrive **at most** once per
 * interval so the chat doesn't churn on every poll tick.
 */

export type HeartbeatKind =
  | "idle"
  | "starting"
  | "running"
  | "done"
  | "error";

/** Interval between steady-state heartbeat updates. */
export const HEARTBEAT_INTERVAL_MS = 60_000;

export interface HeartbeatState {
  kind: HeartbeatKind;
  /** Active stage id, if any — distinct stages publish immediately. */
  stage: string | null;
  /** When this state was published (``Date.now()`` convention). */
  emittedAt: number;
}

/**
 * Decide whether the caller should publish ``next`` as a fresh
 * heartbeat.  State transitions (kind or stage change) always emit so
 * the user sees them immediately; otherwise we wait at least
 * ``minIntervalMs`` between emissions.
 */
export function shouldEmitHeartbeat({
  prev,
  next,
  now,
  minIntervalMs = HEARTBEAT_INTERVAL_MS,
}: {
  prev: Pick<HeartbeatState, "kind" | "stage" | "emittedAt"> | null;
  next: Pick<HeartbeatState, "kind" | "stage">;
  now: number;
  minIntervalMs?: number;
}): boolean {
  if (!prev) return true;
  if (prev.kind !== next.kind) return true;
  if (prev.stage !== next.stage) return true;
  return now - prev.emittedAt >= minIntervalMs;
}
