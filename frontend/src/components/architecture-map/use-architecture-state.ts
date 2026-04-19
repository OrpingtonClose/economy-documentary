"use client";

import { useEffect, useState } from "react";

import { subscribeAguiStream } from "@/lib/agui-stream";

import type { GateId } from "./diagrams";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export type GateState = "passed" | "pending" | "failed" | "unknown";

export type ArtefactKind = "otio" | "blackboard" | "ledger";

export interface BackEdgeEvent {
  from: string;
  to: string;
}

export interface ArchitectureState {
  gateStates: Record<GateId, GateState>;
  artefactPulses: Record<ArtefactKind, boolean>;
  backEdgePulse: BackEdgeEvent | null;
}

const INITIAL: ArchitectureState = {
  gateStates: {
    G0: "unknown",
    G1: "unknown",
    G2: "unknown",
    G3: "unknown",
    G4: "unknown",
  },
  artefactPulses: { otio: false, blackboard: false, ledger: false },
  backEdgePulse: null,
};

interface OtioTrackSlot {
  status?: string | null;
}

interface OtioTrack {
  name?: string;
  slots?: OtioTrackSlot[];
}

interface OtioStateResponse {
  state?: string;
  tracks?: OtioTrack[];
}

interface RestatedBriefResponse {
  present?: boolean;
}

interface BackEdgePayload {
  from?: string;
  to?: string;
}

type AguiEnvelope<T> = { data?: T } | T;

function unwrap<T>(value: AguiEnvelope<T> | null | undefined): T | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "object" && "data" in (value as Record<string, unknown>)) {
    const inner = (value as { data?: T }).data;
    return inner ?? null;
  }
  return value as T;
}

/**
 * DESIGN-10 live-state hook for the Architecture Map.
 *
 * Reads:
 *   - `GET /agui/restated_brief` for the INTENT (R0) gate — G0 is
 *     "passed" once the Intent Extractor has written a brief, "pending"
 *     otherwise.
 *   - `GET /agui/otio/state` for the stage gates G1..G4. G2 flips to
 *     "passed" when the authoritative OTIO crystallises; G3/G4 are
 *     derived from V1_Video slot statuses.
 *   - `/agui/stream` SSE, via the shared `subscribeAguiStream`
 *     singleton, for live overlays: `otio_authoritative`,
 *     `slot_state`, `artifact_update`, `directive_applied`, and the
 *     bespoke `pipeline_back_edge_fired` event.
 *
 * TODO(architecture-map): the pipeline does not yet emit
 * `pipeline_back_edge_fired` on the AG-UI bus. Until it does, the
 * dev-only synthetic stub `window.__fireArchitectureBackEdge(from, to)`
 * drives the amber edge pulse; remove the stub once the backend
 * lands the real event.
 */
export function useArchitectureState(
  opts: { disabled?: boolean } = {},
): ArchitectureState {
  const { disabled = false } = opts;
  const [state, setState] = useState<ArchitectureState>(INITIAL);

  useEffect(() => {
    if (disabled) return;
    let cancelled = false;

    function safeSetState(updater: (s: ArchitectureState) => ArchitectureState) {
      if (cancelled) return;
      setState(updater);
    }

    fetch(`${BACKEND_URL}/agui/restated_brief`)
      .then((r) => (r.ok ? (r.json() as Promise<RestatedBriefResponse>) : null))
      .then((data) => {
        if (!data) return;
        const present = !!data.present;
        safeSetState((s) => ({
          ...s,
          gateStates: {
            ...s.gateStates,
            G0: present ? "passed" : "pending",
          },
        }));
      })
      .catch(() => {
        /* keep G0 as "unknown" when the endpoint is unreachable. */
      });

    function refreshOtio() {
      fetch(`${BACKEND_URL}/agui/otio/state`)
        .then((r) => (r.ok ? (r.json() as Promise<OtioStateResponse>) : null))
        .then((data) => {
          if (!data) return;
          const tracks = Array.isArray(data.tracks) ? data.tracks : [];
          const authoritative = data.state === "authoritative";
          const narration = tracks.find((t) => t.name === "A1_Narration")?.slots ?? [];
          const video = tracks.find((t) => t.name === "V1_Video")?.slots ?? [];
          const g1: GateState = narration.length > 0 ? "passed" : "pending";
          const g2: GateState = authoritative
            ? "passed"
            : narration.length > 0
              ? "pending"
              : "unknown";
          const g3: GateState =
            video.length === 0
              ? authoritative
                ? "pending"
                : "unknown"
              : video.every((s) => s.status && s.status !== "unplanned")
                ? "passed"
                : "pending";
          const g4: GateState =
            video.length === 0
              ? "unknown"
              : video.every((s) => s.status === "delivered")
                ? "passed"
                : "pending";
          safeSetState((s) => ({
            ...s,
            gateStates: { ...s.gateStates, G1: g1, G2: g2, G3: g3, G4: g4 },
          }));
        })
        .catch(() => {
          /* keep stage gates as "unknown" when the endpoint is unreachable. */
        });
    }
    refreshOtio();

    const pulseTimers = new Map<ArtefactKind, number>();
    function pulseArtefact(kind: ArtefactKind) {
      const existing = pulseTimers.get(kind);
      if (existing) window.clearTimeout(existing);
      safeSetState((s) => ({
        ...s,
        artefactPulses: { ...s.artefactPulses, [kind]: true },
      }));
      const t = window.setTimeout(() => {
        pulseTimers.delete(kind);
        safeSetState((s) => ({
          ...s,
          artefactPulses: { ...s.artefactPulses, [kind]: false },
        }));
      }, 1500);
      pulseTimers.set(kind, t);
    }

    let backEdgeTimer: number | null = null;
    function pulseBackEdge(edge: BackEdgeEvent) {
      if (backEdgeTimer) window.clearTimeout(backEdgeTimer);
      safeSetState((s) => ({ ...s, backEdgePulse: edge }));
      backEdgeTimer = window.setTimeout(() => {
        backEdgeTimer = null;
        safeSetState((s) => ({ ...s, backEdgePulse: null }));
      }, 1800);
    }

    const unsubscribe = subscribeAguiStream({
      events: [
        "otio_authoritative",
        "slot_state",
        "artifact_update",
        "directive_applied",
        "pipeline_back_edge_fired",
      ],
      onEvent: (eventType, ev) => {
        let parsed: unknown = null;
        try {
          parsed = JSON.parse(ev.data);
        } catch {
          /* ignore malformed event data */
        }
        switch (eventType) {
          case "otio_authoritative":
          case "slot_state":
            pulseArtefact("otio");
            refreshOtio();
            break;
          case "artifact_update":
            pulseArtefact("blackboard");
            break;
          case "directive_applied":
            pulseArtefact("ledger");
            break;
          case "pipeline_back_edge_fired": {
            const payload = unwrap<BackEdgePayload>(
              parsed as AguiEnvelope<BackEdgePayload> | null,
            );
            if (payload && payload.from && payload.to) {
              pulseBackEdge({
                from: String(payload.from),
                to: String(payload.to),
              });
            }
            break;
          }
        }
      },
    });

    // Synthetic stub: drives the amber edge pulse from the browser
    // console until the backend emits `pipeline_back_edge_fired` on
    // the AG-UI bus. See TODO at the top of this file.
    type Win = typeof window & {
      __fireArchitectureBackEdge?: (from: string, to: string) => void;
    };
    const w = window as Win;
    w.__fireArchitectureBackEdge = (from, to) =>
      pulseBackEdge({ from, to });

    return () => {
      cancelled = true;
      unsubscribe();
      for (const t of pulseTimers.values()) window.clearTimeout(t);
      if (backEdgeTimer) window.clearTimeout(backEdgeTimer);
      delete w.__fireArchitectureBackEdge;
    };
  }, [disabled]);

  return state;
}
