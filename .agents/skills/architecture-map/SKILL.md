# Architecture Map skill

This skill covers the **Live Architecture Map** surface
(`frontend/src/components/architecture-map.tsx`, issue
[#262](https://github.com/OrpingtonClose/economy-documentary/issues/262)).
The map renders the canonical architecture diagrams from
`docs/ARCHITECTURE_DIAGRAMS.md` and lights them up with the pipeline's
current state (active stage, visited stages, gate outcomes, artefact
pulses, back-edge retries).

## Files

- `frontend/src/components/architecture-map.tsx` — composite component.
  Exports two things:
  - `ArchitectureMap` — the pinned "Show architecture map" button that
    opens a right-side shadcn `Sheet`. Use this from the dashboard.
  - `ArchitectureMapSurface` — the surface itself (Mermaid +
    gates + stage drill-downs). Use this when embedding inline (tests,
    `/ui-kit` preview, future split-screen layouts).
- `frontend/src/components/architecture-map/diagrams.ts` — typed
  registry of every diagram copied verbatim from
  `docs/ARCHITECTURE_DIAGRAMS.md`. Each entry carries the line range it
  was copied from (e.g. `d1` → `ARCHITECTURE_DIAGRAMS.md L13-L83`).
  Also exposes `STAGE_META` (which stages map to which sub-diagrams)
  and `GATE_META` (plain-English gate names).
- `frontend/src/components/architecture-map/use-architecture-state.ts`
  — hook that fetches `/agui/restated_brief` + `/agui/otio/state` and
  subscribes to `/agui/stream` for live event overlays.
- `frontend/src/app/globals.css` — CSS classes the overlays apply
  (`.architecture-node--active`, `.architecture-node--visited`,
  `.architecture-gate--{passed,pending,failed,unknown}`,
  `.architecture-artefact--pulse`, `.architecture-edge--back-edge`).
  The classes are applied to the SVG Mermaid emits; the Mermaid source
  strings are never mutated.

## Hard invariants (from #262)

1. **Source graphs are verbatim.** The mermaid chart strings in
   `diagrams.ts` are copied verbatim from the fenced blocks of
   `docs/ARCHITECTURE_DIAGRAMS.md`. If the markdown changes, re-copy
   the block and update the `sourceRange` comment. Do **not** inline
   custom node styling into the chart — all lights are CSS.
2. **Plain English on the primary surface.** Gate names
   (`GATE_META`), stage names (`STAGE_META`), and overlay labels must
   be plain English. Jargon belongs inside the Mermaid diagrams, not
   on the surrounding chrome.
3. **shadcn primitives only.** Use `Sheet`, `Tabs`, `Card`, `Badge`,
   `Button`, and `Collapsible` from `@/components/ui`. Do not add
   competing UI libraries.

## How to extend with a new diagram

1. Add the fenced ```` ```mermaid ```` block to
   `docs/ARCHITECTURE_DIAGRAMS.md` with a `## N. <title>` heading.
2. Re-copy the block into `diagrams.ts`:
   - Append a new entry to `DIAGRAM_IDS` (e.g. `"d11"`). Keep the id
     naming tied to the diagram number so reviewers can cross-check.
   - Add a `DIAGRAMS.d11` entry with `label`, `summary`,
     `sourceRange: { start, end }` (1-indexed lines in
     `ARCHITECTURE_DIAGRAMS.md`), and `chart: "<raw mermaid text>"`.
   - Update the citation block at the top of `diagrams.ts`.
3. (Optional) If the new diagram is the canonical zoom-in for a
   pipeline stage, add its id to the relevant `STAGE_META.*.subDiagrams`
   array. The "Zoom in on a stage" `Collapsible` will pick it up
   automatically.
4. Run `npm run lint && npm run build && npm test`. The
   `architecture-map.test.tsx` suite renders the map with
   `currentStage='S3'`; add assertions for any new overlay behaviour.

## How to add a new event overlay

All overlays are applied by `applyOverlays()` in
`architecture-map.tsx`. To add one:

1. Decide the DOM signature. For nodes, target the node's Mermaid id
   (see `findNodesByMermaidId`, which handles both the `flowchart-<id>-<n>`
   and bare `<id>` conventions). For edges, use `findBackEdges` as a
   template — Mermaid v11 emits ids like `L_<from>_<to>_<n>`.
2. Add a class + CSS keyframe to `frontend/src/app/globals.css`,
   scoped under `.architecture-map-surface` so it never leaks into
   unrelated Mermaid diagrams elsewhere in the app.
3. Add the state shape to `ArchitectureState` in
   `use-architecture-state.ts`. Wire it to the matching AG-UI event
   type inside `subscribeAguiStream(...).onEvent`. Include a timer to
   clear the overlay (artefact pulses last 1.5s, back-edge 1.8s).
4. Apply the class inside `applyOverlays()` and reset it at the top of
   the same function so consecutive renders are idempotent.

## Backend event catalogue

The hook currently listens for these AG-UI stream event types:

- `otio_authoritative` — fires when the OTIO crystallises (stage 2
  completion). Pulses the OTIO artefact.
- `slot_state` — fires on every slot status transition. Refreshes the
  OTIO snapshot and pulses the OTIO artefact.
- `artifact_update` — fires when a Blackboard artefact is written.
  Pulses the Blackboard cylinder.
- `directive_applied` — fires when the Preference Interpreter appends
  a record to the ledger. Pulses the Preference Ledger cylinder.
- `pipeline_back_edge_fired` — **TODO**: the backend does not yet
  emit this event. A dev-only synthetic stub is attached to
  `window.__fireArchitectureBackEdge(from, to)` so you can drive the
  amber edge pulse from the browser console while the backend event
  is being built. Remove the stub once the pipeline emits the event
  on the AG-UI bus.

## Testing

Unit tests live at
`frontend/src/components/__tests__/architecture-map.test.tsx`. They
mock the `mermaid` module to return a minimal SVG that matches the
shape Mermaid v11 emits (`<g class="node" id="flowchart-<id>-<n>">`)
and assert the overlay classes are applied to the right nodes.

Run the suite with `cd frontend && npm test`.
