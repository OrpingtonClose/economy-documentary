"use client";

/**
 * UI-07 — client-side run persistence + reconnect glue.
 *
 * Responsibilities (tracked by issues #211 and #213):
 *
 * 1. **URL ↔ run id propagation.** On first mount we read `?run=<id>`
 *    from the URL. If it's present we hand it back so downstream
 *    components can hydrate in "resume" mode. When a fresh pipeline run
 *    starts, the backend emits a `run_started` custom event and also
 *    returns the id on the `X-Pipeline-Run-Id` response header; we poll
 *    `GET /api/current-run` as a simple fallback (CopilotKit owns the
 *    POST / socket so we can't hook its stream directly from the
 *    browser) and stamp the URL when it changes.
 *
 * 2. **Replay on reload.** If the URL has a run id that the server
 *    still knows about, we fetch the buffered events with
 *    `Last-Event-ID` set to whatever we last persisted. The SSE body is
 *    parsed manually (EventSource can't set headers); we walk the
 *    stream, bump the persisted last-event-id, detect a
 *    `buffer_overflow` marker, and collect any replayed agent text
 *    into a bounded chat transcript also stored per run id.
 *
 * 3. **Bounded localStorage per run id.** We cap the replay transcript
 *    at {@link CHAT_MAX_CHARS} characters so a month-old run can't
 *    silently eat megabytes of client storage.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

const LS_PREFIX = "dp:run:";
const CHAT_MAX_CHARS = 64_000;
const MAX_PERSISTED_RUNS = 8; // prune older runs once we exceed this.

export type RunReplayStatus =
  | "idle"
  | "probing"
  | "replaying"
  | "ready"
  | "overflow"
  | "unknown-run"
  | "error";

export type ReplayedMessage = {
  role: "user" | "assistant" | "system";
  content: string;
  /** Monotonic sequence id of the replay event that produced this chunk. */
  seq: number;
};

export type RunSession = {
  runId: string | null;
  setRunId: (id: string | null) => void;
  clearRun: () => void;
  lastEventId: number;
  status: RunReplayStatus;
  overflowMessage: string | null;
  dismissOverflow: () => void;
  /** Replayed assistant/user messages assembled from buffered events. */
  replayedMessages: ReplayedMessage[];
};

type StoredSession = {
  lastEventId: number;
  transcript: ReplayedMessage[];
  updatedAt: number;
};

// ---------------------------------------------------------------------------
// URL helpers — isolated so the tests can exercise them without jsdom.
// ---------------------------------------------------------------------------

function readRunIdFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  const url = new URL(window.location.href);
  const fromQuery = url.searchParams.get("run");
  if (fromQuery) return fromQuery;
  // Accept `#run=...` for legacy/manual pastes too — we write `?run=`
  // but copy-pasted links from older sessions may use the hash form.
  const hash = url.hash.replace(/^#/, "");
  const hashParams = new URLSearchParams(hash);
  return hashParams.get("run");
}

function writeRunIdToUrl(runId: string | null): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (runId) {
    url.searchParams.set("run", runId);
  } else {
    url.searchParams.delete("run");
  }
  window.history.replaceState(window.history.state, "", url.toString());
}

// ---------------------------------------------------------------------------
// localStorage helpers with a hard cap on stored run count + size.
// ---------------------------------------------------------------------------

function storageKey(runId: string): string {
  return `${LS_PREFIX}${runId}`;
}

function readStored(runId: string): StoredSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey(runId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredSession;
    if (!parsed || typeof parsed !== "object") return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeStored(runId: string, value: StoredSession): void {
  if (typeof window === "undefined") return;
  try {
    const trimmed = capTranscript(value.transcript);
    const payload: StoredSession = {
      lastEventId: value.lastEventId,
      transcript: trimmed,
      updatedAt: Date.now(),
    };
    window.localStorage.setItem(storageKey(runId), JSON.stringify(payload));
    pruneOldRuns();
  } catch {
    // ignore quota / private-mode failures; persistence is best-effort.
  }
}

function capTranscript(messages: ReplayedMessage[]): ReplayedMessage[] {
  let total = 0;
  const out: ReplayedMessage[] = [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    total += m.content.length;
    if (total > CHAT_MAX_CHARS) break;
    out.push(m);
  }
  return out.reverse();
}

function pruneOldRuns(): void {
  if (typeof window === "undefined") return;
  const entries: Array<{ key: string; updatedAt: number }> = [];
  for (let i = 0; i < window.localStorage.length; i++) {
    const key = window.localStorage.key(i);
    if (!key || !key.startsWith(LS_PREFIX)) continue;
    try {
      const parsed = JSON.parse(
        window.localStorage.getItem(key) || "{}",
      ) as StoredSession;
      entries.push({ key, updatedAt: parsed.updatedAt || 0 });
    } catch {
      entries.push({ key, updatedAt: 0 });
    }
  }
  if (entries.length <= MAX_PERSISTED_RUNS) return;
  entries.sort((a, b) => a.updatedAt - b.updatedAt);
  const toDrop = entries.slice(0, entries.length - MAX_PERSISTED_RUNS);
  for (const { key } of toDrop) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  }
}

// ---------------------------------------------------------------------------
// SSE parsing + replay.
// ---------------------------------------------------------------------------

type ParsedEvent = {
  id: number | null;
  dataLines: string[];
};

function parseSseChunk(buffer: string): {
  events: ParsedEvent[];
  remainder: string;
} {
  const events: ParsedEvent[] = [];
  let idx = 0;
  while (true) {
    const sep = buffer.indexOf("\n\n", idx);
    if (sep === -1) break;
    const raw = buffer.slice(idx, sep);
    idx = sep + 2;
    const ev: ParsedEvent = { id: null, dataLines: [] };
    for (const line of raw.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const [key, ...rest] = line.split(": ");
      const value = rest.join(": ");
      if (key === "id") {
        const parsed = Number.parseInt(value, 10);
        ev.id = Number.isFinite(parsed) ? parsed : null;
      } else if (key === "data") {
        ev.dataLines.push(value);
      }
    }
    if (ev.dataLines.length > 0 || ev.id !== null) events.push(ev);
  }
  return { events, remainder: buffer.slice(idx) };
}

function extractAgentText(eventData: unknown): ReplayedMessage | null {
  if (!eventData || typeof eventData !== "object") return null;
  const obj = eventData as Record<string, unknown>;
  const type = String(obj.type || "").toUpperCase();
  if (type === "TEXT_MESSAGE_CONTENT" || type === "TEXT_MESSAGE_CHUNK") {
    const delta = obj.delta ?? obj.content;
    if (typeof delta === "string" && delta.length > 0) {
      return { role: "assistant", content: delta, seq: 0 };
    }
  }
  if (type === "TEXT_MESSAGE_END") {
    return null;
  }
  if (type === "USER_MESSAGE" && typeof obj.content === "string") {
    return { role: "user", content: obj.content, seq: 0 };
  }
  return null;
}

type ReplayResult = {
  finalSeq: number;
  overflow: boolean;
  transcript: ReplayedMessage[];
};

async function runReplay(
  runId: string,
  lastEventId: number,
  signal: AbortSignal,
): Promise<ReplayResult> {
  const res = await fetch(`${BACKEND_URL}/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      "X-Pipeline-Run-Id": runId,
      "Last-Event-ID": String(lastEventId),
    },
    body: "",
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`Replay fetch failed: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalSeq = lastEventId;
  let overflow = false;
  const transcript: ReplayedMessage[] = [];

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { events, remainder } = parseSseChunk(buffer);
    buffer = remainder;
    for (const ev of events) {
      if (ev.id !== null) finalSeq = Math.max(finalSeq, ev.id);
      const dataRaw = ev.dataLines.join("\n");
      if (!dataRaw) continue;
      let payload: unknown;
      try {
        payload = JSON.parse(dataRaw);
      } catch {
        continue;
      }
      const parsed = payload as Record<string, unknown>;
      const name =
        typeof parsed.name === "string" ? (parsed.name as string) : "";
      const value =
        parsed.value && typeof parsed.value === "object"
          ? (parsed.value as Record<string, unknown>)
          : parsed;
      if (name === "buffer_overflow" || value?.type === "buffer_overflow") {
        overflow = true;
        continue;
      }
      if (name === "replay_done" || value?.type === "replay_done") {
        continue;
      }
      const extracted = extractAgentText(payload);
      if (extracted) {
        transcript.push({ ...extracted, seq: ev.id ?? extracted.seq });
      }
    }
  }
  return { finalSeq, overflow, transcript };
}

// ---------------------------------------------------------------------------
// Hook.
// ---------------------------------------------------------------------------

export function useRunSession(): RunSession {
  const [runId, setRunIdState] = useState<string | null>(null);
  const [status, setStatus] = useState<RunReplayStatus>("idle");
  const [lastEventId, setLastEventId] = useState<number>(0);
  const [overflowMessage, setOverflowMessage] = useState<string | null>(null);
  const [replayedMessages, setReplayedMessages] = useState<ReplayedMessage[]>(
    [],
  );
  const bootstrappedRef = useRef(false);
  const replayInFlightRef = useRef(false);

  const setRunId = useCallback((id: string | null) => {
    setRunIdState(id);
    writeRunIdToUrl(id);
    if (!id) {
      setLastEventId(0);
      setReplayedMessages([]);
      setOverflowMessage(null);
      setStatus("idle");
    }
  }, []);

  const clearRun = useCallback(() => setRunId(null), [setRunId]);

  // Dismissing the overflow banner also transitions status back to
  // ``ready`` so any downstream consumer polling ``status === "ready"``
  // sees the resume as complete. (The banner already hides itself the
  // moment ``overflowMessage`` goes null, but the status field is
  // exported for other consumers.)
  const dismissOverflow = useCallback(() => {
    setOverflowMessage(null);
    setStatus("ready");
  }, []);

  // -- bootstrap: read URL, fetch /api/current-run for fallback. -----------
  useEffect(() => {
    if (bootstrappedRef.current) return;
    bootstrappedRef.current = true;
    const fromUrl = readRunIdFromUrl();
    if (fromUrl) {
      setRunIdState(fromUrl);
      const stored = readStored(fromUrl);
      if (stored) {
        setLastEventId(stored.lastEventId);
        setReplayedMessages(stored.transcript);
      }
    }
  }, []);

  // -- URL propagation: poll current-run until we have a run id. ----------
  useEffect(() => {
    if (runId) return;
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        try {
          const res = await fetch(`${BACKEND_URL}/api/current-run`);
          if (res.ok) {
            const data = (await res.json()) as {
              run_id: string | null;
              exists: boolean;
            };
            if (!cancelled && data.exists && data.run_id) {
              setRunIdState(data.run_id);
              writeRunIdToUrl(data.run_id);
              return;
            }
          }
        } catch {
          /* swallow transient network errors */
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // -- Replay: when we have a run id from URL + server confirms it. -------
  useEffect(() => {
    if (!runId) return;
    if (replayInFlightRef.current) return;
    const controller = new AbortController();
    replayInFlightRef.current = true;
    setStatus("probing");
    (async () => {
      try {
        const probe = await fetch(
          `${BACKEND_URL}/api/runs/${encodeURIComponent(runId)}/exists`,
          { signal: controller.signal },
        );
        if (!probe.ok) throw new Error(`probe failed: ${probe.status}`);
        const probeData = (await probe.json()) as { exists: boolean };
        if (!probeData.exists) {
          // The URL pointed to a run the server no longer knows about
          // (server restarted, run aged out, etc.). Strip the query
          // param + localStorage cache *without* calling ``setRunId``,
          // which would stomp the status back to ``idle`` and hide the
          // "unknown run" banner before the user ever saw it.
          writeRunIdToUrl(null);
          setRunIdState(null);
          setLastEventId(0);
          setReplayedMessages([]);
          setOverflowMessage(null);
          setStatus("unknown-run");
          return;
        }
        setStatus("replaying");
        const stored = readStored(runId);
        const cursor = stored?.lastEventId ?? 0;
        const { finalSeq, overflow, transcript } = await runReplay(
          runId,
          cursor,
          controller.signal,
        );
        setLastEventId(finalSeq);
        setReplayedMessages((prev) => {
          const merged = [...prev, ...transcript];
          writeStored(runId, {
            lastEventId: finalSeq,
            transcript: merged,
            updatedAt: Date.now(),
          });
          return merged;
        });
        if (overflow) {
          setOverflowMessage(
            "Some events were evicted from the server buffer before you " +
              "reconnected. The timeline snapshot has been refetched.",
          );
          setStatus("overflow");
        } else {
          setStatus("ready");
        }
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.error("[run-session] replay failed", err);
        setStatus("error");
      } finally {
        replayInFlightRef.current = false;
      }
    })();
    return () => {
      controller.abort();
      replayInFlightRef.current = false;
    };
  }, [runId, setRunId]);

  return {
    runId,
    setRunId,
    clearRun,
    lastEventId,
    status,
    overflowMessage,
    dismissOverflow,
    replayedMessages,
  };
}

// Re-export the internals so the tests can exercise them without jsdom.
export const __testing__ = {
  parseSseChunk,
  capTranscript,
  extractAgentText,
  CHAT_MAX_CHARS,
  MAX_PERSISTED_RUNS,
  LS_PREFIX,
};
