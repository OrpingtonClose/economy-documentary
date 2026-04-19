/** @jest-environment jsdom */

/**
 * DESIGN-10 (#262) — Live Architecture Map unit tests.
 *
 * The test renders `ArchitectureMap` with `currentStage='S3'` and
 * asserts the active-node overlay class (`architecture-node--active`)
 * is applied to the SVG node representing stage S3. The map overlays
 * are the only feature of the component we can reliably test in jsdom
 * without pulling in Mermaid's d3 rendering stack, so we mock the
 * `mermaid` module to return a minimal SVG that matches the output
 * shape Mermaid v11 emits for `docs/ARCHITECTURE_DIAGRAMS.md` diagram
 * 1 (`S1..S5`, `G0..G4`, `OTIO` / `BB` / `PROMPT` cylinders).
 */

import { act, render, waitFor } from "@testing-library/react";

// Hoisted stubs so `jest.mock` below can reference them before the
// actual `ArchitectureMap` import causes the mermaid module to load.
const stubSvg = `
<svg class="flowchart" xmlns="http://www.w3.org/2000/svg">
  <g class="nodes">
    <g class="node" id="flowchart-S1-0"><rect width="80" height="40"/></g>
    <g class="node" id="flowchart-S2-0"><rect width="80" height="40"/></g>
    <g class="node" id="flowchart-S3-0"><rect width="80" height="40"/></g>
    <g class="node" id="flowchart-S4-0"><rect width="80" height="40"/></g>
    <g class="node" id="flowchart-S5-0"><rect width="80" height="40"/></g>
    <g class="node" id="flowchart-G0-0"><polygon points="0,0 10,0 5,10"/></g>
    <g class="node" id="flowchart-G1-0"><polygon points="0,0 10,0 5,10"/></g>
    <g class="node" id="flowchart-G2-0"><polygon points="0,0 10,0 5,10"/></g>
    <g class="node" id="flowchart-G3-0"><polygon points="0,0 10,0 5,10"/></g>
    <g class="node" id="flowchart-G4-0"><polygon points="0,0 10,0 5,10"/></g>
    <g class="node" id="flowchart-OTIO-0"><path d="M0,0"/></g>
  </g>
  <g class="edges">
    <path class="flowchart-link" id="L_S5_S1_0" d="M0,0"/>
  </g>
</svg>`;

jest.mock("mermaid", () => ({
  __esModule: true,
  default: {
    initialize: jest.fn(),
    render: jest.fn(async (id: string) => ({ svg: stubSvg, bindFunctions: () => undefined })),
  },
}));

import { ArchitectureMap } from "@/components/architecture-map";

class StubEventSource {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 1;
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {}
}

beforeAll(() => {
  (global as unknown as { EventSource: typeof StubEventSource }).EventSource =
    StubEventSource;
});

beforeEach(() => {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn(
    async () =>
      ({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => "{}",
      }) as unknown as Response,
  ) as unknown as jest.Mock;
});

describe("ArchitectureMap overlays", () => {
  test("renders with currentStage='S3' and applies the active-node CSS class", async () => {
    const { container } = render(
      <ArchitectureMap currentStage="S3" inline disableBackend />,
    );

    const activeNode = await waitFor(() => {
      const node = container.querySelector(
        ".architecture-map-chart .architecture-node--active",
      );
      expect(node).not.toBeNull();
      return node as Element;
    });

    expect(activeNode.getAttribute("data-id")).toBe("S3");
    expect(activeNode.getAttribute("data-stage-role")).toBe("active");
    expect(activeNode.getAttribute("id")).toBe("flowchart-S3-0");

    // Only S3 is the active stage; other stages must carry the
    // neutral "unvisited" overlay when no `visitedStages` is provided.
    const unvisited = container.querySelectorAll(
      ".architecture-map-chart .architecture-node--unvisited",
    );
    const unvisitedIds = Array.from(unvisited).map((n) => n.getAttribute("data-id"));
    expect(unvisitedIds.sort()).toEqual(["S1", "S2", "S4", "S5"]);
  });

  test("marks visited stages with the warm muted overlay class", async () => {
    const { container } = render(
      <ArchitectureMap
        currentStage="S3"
        visitedStages={["S1", "S2"]}
        inline
        disableBackend
      />,
    );

    await waitFor(() => {
      const visited = container.querySelectorAll(
        ".architecture-map-chart .architecture-node--visited",
      );
      expect(visited.length).toBe(2);
    });

    const visited = container.querySelectorAll(
      ".architecture-map-chart .architecture-node--visited",
    );
    const visitedIds = Array.from(visited)
      .map((n) => n.getAttribute("data-id"))
      .sort();
    expect(visitedIds).toEqual(["S1", "S2"]);
  });

  test("gate states drive the gate overlay class on the matching node", async () => {
    const { container } = render(
      <ArchitectureMap
        currentStage="S3"
        gateStates={{ G0: "passed", G1: "passed", G2: "pending", G3: "unknown", G4: "unknown" }}
        inline
        disableBackend
      />,
    );

    await waitFor(() => {
      const g2 = container.querySelector(
        ".architecture-map-chart .architecture-gate--pending",
      );
      expect(g2).not.toBeNull();
      expect(g2?.getAttribute("data-gate-id")).toBe("G2");
    });

    const passed = container.querySelectorAll(
      ".architecture-map-chart .architecture-gate--passed",
    );
    const passedIds = Array.from(passed)
      .map((n) => n.getAttribute("data-gate-id"))
      .sort();
    expect(passedIds).toEqual(["G0", "G1"]);
  });
});
