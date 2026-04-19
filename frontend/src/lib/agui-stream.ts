"use client";

/**
 * Shared ``/agui/stream`` EventSource singleton.
 *
 * ARCH-H mandates a single SSE connection per dashboard — pipeline
 * events (otio_snapshot, slot_state, preview_ready, …) and agent
 * turns all ride the same stream.  Multiple hooks
 * (``useOtioStream`` / ``usePreviewStream`` / …) want to react to
 * disjoint event types, so each one subscribes to this shared
 * connection instead of opening its own EventSource.
 *
 * Semantics:
 *   - The first ``subscribe(listener)`` call opens the EventSource.
 *   - Subsequent subscribers share the same connection.
 *   - When the last subscriber disconnects we close the EventSource
 *     so SSR / hot-reload doesn't leak connections.
 *   - ``connected`` / ``error`` are reported through the listener so
 *     every consumer sees the same connection health.
 */

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export interface AguiStreamListener {
  /** Event types to subscribe to; ``*`` means raw ``message`` events. */
  events: string[];
  /** Called with the parsed event and its ``MessageEvent`` wrapper. */
  onEvent(eventType: string, ev: MessageEvent): void;
  /** Called with the current connection state after ``open`` / ``error``. */
  onConnected?: (connected: boolean) => void;
}

interface Registration {
  listener: AguiStreamListener;
  handlers: Array<[string, (ev: MessageEvent) => void]>;
}

let es: EventSource | null = null;
let connected = false;
const registrations = new Set<Registration>();
let openHandler: (() => void) | null = null;
let errorHandler: (() => void) | null = null;

function ensureConnection(): EventSource {
  if (es) return es;
  const next = new EventSource(`${BACKEND_URL}/agui/stream`);
  openHandler = () => {
    connected = true;
    for (const reg of registrations) reg.listener.onConnected?.(true);
  };
  errorHandler = () => {
    connected = false;
    for (const reg of registrations) reg.listener.onConnected?.(false);
  };
  next.addEventListener("open", openHandler);
  next.addEventListener("error", errorHandler);
  es = next;
  return next;
}

function teardownIfEmpty(): void {
  if (registrations.size > 0 || !es) return;
  if (openHandler) es.removeEventListener("open", openHandler);
  if (errorHandler) es.removeEventListener("error", errorHandler);
  es.close();
  es = null;
  openHandler = null;
  errorHandler = null;
  connected = false;
}

/** Attach a listener to the shared stream.  Returns an unsubscribe fn. */
export function subscribeAguiStream(listener: AguiStreamListener): () => void {
  const conn = ensureConnection();
  const reg: Registration = { listener, handlers: [] };
  for (const evt of listener.events) {
    const handler = (ev: MessageEvent) => listener.onEvent(evt, ev);
    reg.handlers.push([evt, handler]);
    conn.addEventListener(evt, handler as EventListener);
  }
  registrations.add(reg);
  // Deliver current connection state immediately so late subscribers
  // don't stay stuck on the initial "connecting" state when the socket
  // already opened.
  if (connected) listener.onConnected?.(true);
  return () => {
    for (const [evt, handler] of reg.handlers) {
      conn.removeEventListener(evt, handler as EventListener);
    }
    registrations.delete(reg);
    teardownIfEmpty();
  };
}

/** Test-only: reset module state between jest runs or HMR reloads. */
export function _resetAguiStreamForTests(): void {
  for (const reg of registrations) {
    for (const [evt, handler] of reg.handlers) {
      es?.removeEventListener(evt, handler as EventListener);
    }
  }
  registrations.clear();
  if (es) {
    if (openHandler) es.removeEventListener("open", openHandler);
    if (errorHandler) es.removeEventListener("error", errorHandler);
    es.close();
  }
  es = null;
  openHandler = null;
  errorHandler = null;
  connected = false;
}
