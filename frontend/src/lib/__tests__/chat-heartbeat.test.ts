/** @jest-environment node */

/**
 * DESIGN-01 (#253) — throttle semantics for the chat heartbeat.
 *
 * The chat column surfaces one status line per pipeline stage. Each
 * state transition must publish immediately (so a fresh run or a
 * stage change is visible within a second) but steady-state
 * heartbeats must be capped at one per ``HEARTBEAT_INTERVAL_MS`` so
 * the chat doesn't churn on every poll tick.
 */

import {
  HEARTBEAT_INTERVAL_MS,
  shouldEmitHeartbeat,
} from "@/lib/chat-heartbeat";

describe("shouldEmitHeartbeat", () => {
  test("publishes the first heartbeat immediately", () => {
    expect(
      shouldEmitHeartbeat({
        prev: null,
        next: { kind: "starting", stage: null },
        now: 1_000,
      }),
    ).toBe(true);
  });

  test("publishes on kind transitions", () => {
    expect(
      shouldEmitHeartbeat({
        prev: { kind: "starting", stage: null, emittedAt: 1_000 },
        next: { kind: "running", stage: "scenario" },
        now: 1_500,
      }),
    ).toBe(true);
  });

  test("publishes on stage transitions even while still running", () => {
    expect(
      shouldEmitHeartbeat({
        prev: { kind: "running", stage: "scenario", emittedAt: 1_000 },
        next: { kind: "running", stage: "audio" },
        now: 2_000,
      }),
    ).toBe(true);
  });

  test(
    "suppresses steady-state heartbeats until the interval has elapsed",
    () => {
      const emittedAt = 10_000;
      // Every poll inside the 60s window is suppressed.
      for (let dt = 1_000; dt < HEARTBEAT_INTERVAL_MS; dt += 1_000) {
        expect(
          shouldEmitHeartbeat({
            prev: { kind: "running", stage: "scenario", emittedAt },
            next: { kind: "running", stage: "scenario" },
            now: emittedAt + dt,
          }),
        ).toBe(false);
      }
      // Exactly at the boundary — publish a refreshed heartbeat.
      expect(
        shouldEmitHeartbeat({
          prev: { kind: "running", stage: "scenario", emittedAt },
          next: { kind: "running", stage: "scenario" },
          now: emittedAt + HEARTBEAT_INTERVAL_MS,
        }),
      ).toBe(true);
      // And clearly past the boundary too.
      expect(
        shouldEmitHeartbeat({
          prev: { kind: "running", stage: "scenario", emittedAt },
          next: { kind: "running", stage: "scenario" },
          now: emittedAt + HEARTBEAT_INTERVAL_MS + 500,
        }),
      ).toBe(true);
    },
  );

  test("respects a custom minIntervalMs override", () => {
    const emittedAt = 0;
    expect(
      shouldEmitHeartbeat({
        prev: { kind: "running", stage: "audio", emittedAt },
        next: { kind: "running", stage: "audio" },
        now: 4_000,
        minIntervalMs: 5_000,
      }),
    ).toBe(false);
    expect(
      shouldEmitHeartbeat({
        prev: { kind: "running", stage: "audio", emittedAt },
        next: { kind: "running", stage: "audio" },
        now: 5_000,
        minIntervalMs: 5_000,
      }),
    ).toBe(true);
  });
});
