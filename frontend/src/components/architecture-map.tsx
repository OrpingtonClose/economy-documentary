"use client";

/**
 * DESIGN-10 — Live Architecture Map.
 *
 * Surfaces the canonical architecture diagrams from
 * `docs/ARCHITECTURE_DIAGRAMS.md` (see
 * `./architecture-map/diagrams.ts` for line-range citations) and
 * lights them up live with the pipeline's current state.
 *
 * Hard constraints from issue #262:
 *   - built only on shadcn primitives (Sheet, Tabs, Card, Badge,
 *     Collapsible) and the STACK-01 `MermaidDiagram`;
 *   - plain English in every label / tooltip / overlay — no jargon
 *     leaks onto the primary surface;
 *   - overlays are applied as CSS classes on the SVG Mermaid emits;
 *     the chart source strings are never mutated (see `diagrams.ts`);
 *   - gates, artefact pulses, and back-edge pulses are driven by the
 *     existing `/agui` endpoints and SSE stream.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Map as MapIcon } from "lucide-react";

import { MermaidDiagram } from "@/components/mermaid-diagram";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

import {
  DIAGRAMS,
  DIAGRAM_IDS,
  GATE_IDS,
  GATE_META,
  STAGE_IDS,
  STAGE_META,
  type DiagramId,
  type GateId,
  type StageId,
} from "./architecture-map/diagrams";
import {
  useArchitectureState,
  type ArtefactKind,
  type BackEdgeEvent,
  type GateState,
} from "./architecture-map/use-architecture-state";

export interface ArchitectureMapProps {
  /** Stage the pipeline is currently executing. Lands the soft green pulse. */
  currentStage?: StageId | null;
  /** Stages the pipeline has already passed through. Rendered in a warm muted tone. */
  visitedStages?: StageId[] | null;
  /** Manual gate-state overrides; skip the backend fetch for the provided gates. */
  gateStates?: Partial<Record<GateId, GateState>>;
  /** Artefact pulse overrides (test / storybook). */
  artefactPulses?: Partial<Record<ArtefactKind, boolean>>;
  /** Back-edge pulse override (test / storybook). */
  backEdgePulse?: BackEdgeEvent | null;
  /** Which diagram to show by default. Defaults to `d1` — the top-level flow. */
  defaultDiagram?: DiagramId;
  /**
   * When true, renders the surface directly (no Sheet or trigger).
   * Used by unit tests and by the `/ui-kit` preview page.
   */
  inline?: boolean;
  /** Sheet open state for the wrapped (non-inline) variant. */
  defaultOpen?: boolean;
  /** Disables backend fetches + SSE subscription. Tests should pass `true`. */
  disableBackend?: boolean;
  className?: string;
}

const HUMAN_GATE_STATE: Record<GateState, string> = {
  passed: "ready",
  pending: "in progress",
  failed: "blocked",
  unknown: "not started",
};

const GATE_BADGE_VARIANT: Record<
  GateState,
  "default" | "secondary" | "destructive" | "outline"
> = {
  passed: "default",
  pending: "secondary",
  failed: "destructive",
  unknown: "outline",
};

interface OverlayState {
  currentStage: StageId | null;
  visited: Set<StageId>;
  gateStates: Record<GateId, GateState>;
  artefactPulses: Record<ArtefactKind, boolean>;
  backEdgePulse: BackEdgeEvent | null;
}

const ARTEFACT_NODE_IDS: Record<ArtefactKind, string[]> = {
  otio: ["OTIO"],
  blackboard: ["BB"],
  ledger: ["PROMPT"],
};

function resolveNodeId(rawId: string): string | null {
  if (!rawId) return null;
  const prefixed = /^flowchart-(.+?)(?:-\d+)?$/.exec(rawId);
  if (prefixed) return prefixed[1];
  return rawId;
}

function findNodesByMermaidId(root: Element, targetId: string): Element[] {
  const matches: Element[] = [];
  const nodes = root.querySelectorAll("g.node");
  nodes.forEach((node) => {
    const resolved =
      node.getAttribute("data-id") ??
      resolveNodeId(node.getAttribute("id") ?? "");
    if (resolved === targetId) matches.push(node);
  });
  // Mermaid may also emit a bare `<g id="<id>">` for some shapes.
  if (matches.length === 0) {
    const direct = root.querySelector(
      `g[id="${CSS.escape(targetId)}"]`,
    );
    if (direct) matches.push(direct);
  }
  return matches;
}

function findBackEdges(root: Element, from: string, to: string): Element[] {
  const matches: Element[] = [];
  // Mermaid v11 edge paths: `L_<from>_<to>_<n>`; older: `L-<from>-<to>-<n>`.
  const selector = "path.flowchart-link, path[id^='L']";
  root.querySelectorAll(selector).forEach((el) => {
    const id = el.getAttribute("id") ?? "";
    const match = /^L[_-](.+?)[_-](.+?)[_-]\d+$/.exec(id);
    if (match && match[1] === from && match[2] === to) matches.push(el);
  });
  return matches;
}

const NODE_CLASSES = [
  "architecture-node--active",
  "architecture-node--visited",
  "architecture-node--unvisited",
] as const;

const GATE_CLASSES = [
  "architecture-gate--passed",
  "architecture-gate--pending",
  "architecture-gate--failed",
  "architecture-gate--unknown",
] as const;

function applyOverlays(surface: HTMLElement, state: OverlayState) {
  const svg = surface.querySelector<SVGElement>(
    ".architecture-map-chart svg",
  );
  if (!svg) return;

  // Reset any previous overlay classes so transitions are idempotent.
  svg
    .querySelectorAll(NODE_CLASSES.map((c) => `.${c}`).join(","))
    .forEach((n) => n.classList.remove(...NODE_CLASSES));
  svg
    .querySelectorAll(GATE_CLASSES.map((c) => `.${c}`).join(","))
    .forEach((n) => n.classList.remove(...GATE_CLASSES));
  svg
    .querySelectorAll(".architecture-artefact--pulse")
    .forEach((n) => n.classList.remove("architecture-artefact--pulse"));
  svg
    .querySelectorAll(".architecture-edge--back-edge")
    .forEach((n) => n.classList.remove("architecture-edge--back-edge"));

  // Stage overlay: active / visited / unvisited.
  for (const stage of STAGE_IDS) {
    const nodes = findNodesByMermaidId(svg, stage);
    if (nodes.length === 0) continue;
    const role: "active" | "visited" | "unvisited" =
      state.currentStage === stage
        ? "active"
        : state.visited.has(stage)
          ? "visited"
          : "unvisited";
    const className = `architecture-node--${role}`;
    for (const node of nodes) {
      node.classList.add(className);
      node.setAttribute("data-id", stage);
      node.setAttribute("data-stage-role", role);
    }
  }

  // Gate overlay: passed / pending / failed / unknown.
  for (const gate of GATE_IDS) {
    const nodes = findNodesByMermaidId(svg, gate);
    if (nodes.length === 0) continue;
    const gs = state.gateStates[gate];
    const className = `architecture-gate--${gs}`;
    for (const node of nodes) {
      node.classList.add(className);
      node.setAttribute("data-gate-id", gate);
      node.setAttribute("data-gate-state", gs);
    }
  }

  // Artefact pulse: OTIO / Blackboard / Preference Ledger cylinders.
  for (const kind of Object.keys(ARTEFACT_NODE_IDS) as ArtefactKind[]) {
    if (!state.artefactPulses[kind]) continue;
    for (const rawId of ARTEFACT_NODE_IDS[kind]) {
      const nodes = findNodesByMermaidId(svg, rawId);
      for (const node of nodes) {
        node.classList.add("architecture-artefact--pulse");
        node.setAttribute("data-artefact-kind", kind);
      }
    }
  }

  // Back-edge pulse.
  if (state.backEdgePulse) {
    const edges = findBackEdges(
      svg,
      state.backEdgePulse.from,
      state.backEdgePulse.to,
    );
    for (const edge of edges) {
      edge.classList.add("architecture-edge--back-edge");
    }
  }
}

export interface ArchitectureMapSurfaceProps
  extends Omit<ArchitectureMapProps, "inline" | "defaultOpen"> {
  /** When true, renders a compact variant for embedding in a Sheet. */
  compact?: boolean;
}

export function ArchitectureMapSurface({
  currentStage = null,
  visitedStages,
  gateStates,
  artefactPulses,
  backEdgePulse,
  defaultDiagram = "d1",
  disableBackend = false,
  compact = false,
  className,
}: ArchitectureMapSurfaceProps) {
  const backend = useArchitectureState({ disabled: disableBackend });
  const [activeDiagram, setActiveDiagram] = useState<DiagramId>(defaultDiagram);
  const [expandedStage, setExpandedStage] = useState<StageId | null>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);

  const resolvedGateStates: Record<GateId, GateState> = useMemo(() => {
    return {
      G0: gateStates?.G0 ?? backend.gateStates.G0,
      G1: gateStates?.G1 ?? backend.gateStates.G1,
      G2: gateStates?.G2 ?? backend.gateStates.G2,
      G3: gateStates?.G3 ?? backend.gateStates.G3,
      G4: gateStates?.G4 ?? backend.gateStates.G4,
    };
  }, [gateStates, backend.gateStates]);

  const resolvedArtefacts: Record<ArtefactKind, boolean> = useMemo(() => {
    return {
      otio: artefactPulses?.otio ?? backend.artefactPulses.otio,
      blackboard:
        artefactPulses?.blackboard ?? backend.artefactPulses.blackboard,
      ledger: artefactPulses?.ledger ?? backend.artefactPulses.ledger,
    };
  }, [artefactPulses, backend.artefactPulses]);

  const resolvedBackEdge: BackEdgeEvent | null =
    backEdgePulse !== undefined ? backEdgePulse : backend.backEdgePulse;

  const visited = useMemo(
    () => new Set<StageId>(visitedStages ?? []),
    [visitedStages],
  );

  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface) return;
    const overlayState: OverlayState = {
      currentStage,
      visited,
      gateStates: resolvedGateStates,
      artefactPulses: resolvedArtefacts,
      backEdgePulse: resolvedBackEdge,
    };
    const apply = () => applyOverlays(surface, overlayState);
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(surface, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [
    activeDiagram,
    currentStage,
    visited,
    resolvedGateStates,
    resolvedArtefacts,
    resolvedBackEdge,
  ]);

  const active = DIAGRAMS[activeDiagram];

  return (
    <div
      ref={surfaceRef}
      data-stage={currentStage ?? ""}
      data-diagram={activeDiagram}
      className={cn(
        "architecture-map-surface flex flex-col gap-4",
        compact && "text-sm",
        className,
      )}
    >
      <div className="flex flex-col gap-2">
        <p className="text-sm text-muted-foreground">
          Where the pipeline is right now, drawn on the same diagrams the team
          uses in docs. The green glow follows the stage being worked on.
        </p>
        <Tabs
          value={activeDiagram}
          onValueChange={(v) => setActiveDiagram(v as DiagramId)}
        >
          <TabsList className="h-auto flex-wrap justify-start gap-1 bg-muted/50 p-1">
            {DIAGRAM_IDS.map((id) => (
              <TabsTrigger key={id} value={id} className="text-xs">
                {DIAGRAMS[id].label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">{active.label}</CardTitle>
          <CardDescription>{active.summary}</CardDescription>
        </CardHeader>
        <CardContent>
          <MermaidDiagram
            key={active.id}
            chart={active.chart}
            className="architecture-map-chart overflow-x-auto"
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Gates</CardTitle>
          <CardDescription>
            Each gate turns green when the pipeline has cleared it. Amber means
            the work is in progress; red means something is blocked.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {GATE_IDS.map((gate) => {
            const meta = GATE_META[gate];
            const gs = resolvedGateStates[gate];
            return (
              <Badge
                key={gate}
                variant={GATE_BADGE_VARIANT[gs]}
                data-gate-id={gate}
                data-gate-state={gs}
                title={meta.blurb}
                className="font-normal"
              >
                <span className="font-semibold">{meta.name}</span>
                <span className="ml-1 opacity-80">· {HUMAN_GATE_STATE[gs]}</span>
              </Badge>
            );
          })}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Zoom in on a stage</CardTitle>
          <CardDescription>
            Open a stage to see the diagrams that explain how it works in
            detail.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {STAGE_IDS.map((stage) => {
            const meta = STAGE_META[stage];
            const subs = meta.subDiagrams;
            const isOpen = expandedStage === stage;
            return (
              <Collapsible
                key={stage}
                open={isOpen}
                onOpenChange={(next) => setExpandedStage(next ? stage : null)}
              >
                <CollapsibleTrigger asChild>
                  <Button
                    variant="ghost"
                    className="flex h-auto w-full items-start justify-between gap-3 px-3 py-2 text-left"
                    data-stage-trigger={stage}
                    disabled={subs.length === 0}
                    aria-label={`Show detail for ${meta.name}`}
                  >
                    <span className="flex flex-col items-start gap-0.5">
                      <span className="font-semibold">{meta.name}</span>
                      <span className="text-xs text-muted-foreground">
                        {meta.blurb}
                      </span>
                    </span>
                    <ChevronDown
                      className={cn(
                        "mt-1 shrink-0 transition-transform",
                        isOpen && "rotate-180",
                      )}
                      aria-hidden="true"
                    />
                  </Button>
                </CollapsibleTrigger>
                <CollapsibleContent className="mt-2 flex flex-col gap-3 border-l border-border pl-3">
                  {subs.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      No extra diagram — see the top-level flow above.
                    </p>
                  ) : (
                    subs.map((sub) => {
                      const subDiagram = DIAGRAMS[sub];
                      return (
                        <div
                          key={sub}
                          className="flex flex-col gap-1"
                          data-substage={sub}
                        >
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="text-sm font-medium">
                              {subDiagram.label}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              {subDiagram.summary}
                            </span>
                          </div>
                          <MermaidDiagram
                            chart={subDiagram.chart}
                            className="architecture-submap-chart overflow-x-auto rounded-md border bg-card p-2"
                          />
                        </div>
                      );
                    })
                  )}
                </CollapsibleContent>
              </Collapsible>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * Composite architecture map: a pinned "Show architecture map" button
 * opens the surface in a right-side Sheet. Pass `inline` to skip the
 * Sheet wrapper (useful for tests and the `/ui-kit` preview).
 */
export function ArchitectureMap(props: ArchitectureMapProps) {
  const { inline = false, defaultOpen = false, className, ...rest } = props;

  if (inline) {
    return (
      <ArchitectureMapSurface
        {...rest}
        className={cn("architecture-map-inline", className)}
      />
    );
  }

  return (
    <Sheet defaultOpen={defaultOpen}>
      <SheetTrigger asChild>
        <Button
          variant="outline"
          className={cn("architecture-map-trigger", className)}
          data-testid="architecture-map-trigger"
        >
          <MapIcon aria-hidden="true" />
          <span>Show architecture map</span>
        </Button>
      </SheetTrigger>
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-xl"
        data-testid="architecture-map-sheet"
      >
        <SheetHeader>
          <SheetTitle>Live architecture map</SheetTitle>
          <SheetDescription>
            The green glow follows the stage being worked on. Gates turn green
            as the pipeline clears them. Open any stage to zoom in.
          </SheetDescription>
        </SheetHeader>
        <div className="mt-4">
          <ArchitectureMapSurface {...rest} compact />
        </div>
      </SheetContent>
    </Sheet>
  );
}
