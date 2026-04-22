/** @jest-environment jsdom */

/**
 * DESIGN-07 (#259): cost-preview dialog.
 *
 * These tests exercise the reusable ``CostPreviewDialog`` primitive
 * directly. Integration with the intervention-bar directive-submit path
 * is covered in dashboard-intervention-cost-preview.test.tsx.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CostPreviewDialog } from "@/components/cost-preview-dialog";
import {
  estimateDirectiveLocal,
  type CostEstimate,
} from "@/lib/cost-estimate";

function sampleEstimate(overrides: Partial<CostEstimate> = {}): CostEstimate {
  return {
    stages: 3,
    stage_label: "scene",
    eta_minutes: 20,
    dollars: 2.1,
    summary:
      "This will rerun 3 scenes, add about 20 minutes, and cost about $2.10.",
    ...overrides,
  };
}

describe("CostPreviewDialog", () => {
  test("renders the plain-English summary and three badges", () => {
    render(
      <CostPreviewDialog
        open
        onOpenChange={() => {}}
        title="Redo this scene"
        estimate={sampleEstimate()}
        onConfirm={() => {}}
      />,
    );
    expect(screen.getByTestId("cost-preview-dialog-summary")).toHaveTextContent(
      /rerun 3 scenes/i,
    );
    expect(screen.getByTestId("cost-preview-dialog-summary")).toHaveTextContent(
      /about 20 minutes/i,
    );
    expect(screen.getByTestId("cost-preview-dialog-summary")).toHaveTextContent(
      /\$2\.10/,
    );
    expect(screen.getByTestId("cost-preview-dialog-badge-stages")).toHaveTextContent(
      /3 scenes/,
    );
    expect(screen.getByTestId("cost-preview-dialog-badge-minutes")).toHaveTextContent(
      /~20 min/,
    );
    expect(screen.getByTestId("cost-preview-dialog-badge-dollars")).toHaveTextContent(
      /~\$2\.10/,
    );
  });

  test("Continue button fires onConfirm; Cancel closes the dialog", async () => {
    const user = userEvent.setup();
    const onConfirm = jest.fn();
    const onOpenChange = jest.fn();
    render(
      <CostPreviewDialog
        open
        onOpenChange={onOpenChange}
        title="Rewind to narration"
        estimate={sampleEstimate()}
        onConfirm={onConfirm}
      />,
    );
    await user.click(screen.getByTestId("cost-preview-dialog-confirm"));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    await user.click(screen.getByTestId("cost-preview-dialog-cancel"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  test("loading state shows a spinner and disables Continue", () => {
    render(
      <CostPreviewDialog
        open
        onOpenChange={() => {}}
        title="Redo"
        estimate={null}
        loading
        onConfirm={() => {}}
      />,
    );
    expect(
      screen.getByTestId("cost-preview-dialog-loading"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("cost-preview-dialog-confirm")).toBeDisabled();
  });

  test("description-only mode (no estimate, no loading) renders the note and still enables Continue", () => {
    render(
      <CostPreviewDialog
        open
        onOpenChange={() => {}}
        title="Pause production"
        description="This will pause at the next safe checkpoint, nothing lost — resume anytime."
        estimate={null}
        onConfirm={() => {}}
      />,
    );
    expect(
      screen.getByTestId("cost-preview-dialog-description"),
    ).toHaveTextContent(/pause at the next safe checkpoint/i);
    // numeric summary + badges should NOT render -- pause has no cost.
    expect(screen.queryByTestId("cost-preview-dialog-summary")).toBeNull();
    expect(screen.queryByTestId("cost-preview-dialog-badge-stages")).toBeNull();
    expect(screen.getByTestId("cost-preview-dialog-confirm")).not.toBeDisabled();
  });
});

describe("estimateDirectiveLocal fallback", () => {
  test("a scene-scoped slot context collapses to one scene", () => {
    const est = estimateDirectiveLocal({
      slot_context: { scene_num: 3, clip_id: "clip-3" },
    });
    expect(est.stages).toBe(1);
    expect(est.summary).toMatch(/rerun 1 scene,/);
  });

  test("global scope widens to three stages by default", () => {
    // No slot context => pipeline-wide; the summary must describe the
    // unit of work in "stage(s)", per the CostEstimate contract.
    const est = estimateDirectiveLocal({ slot_context: null });
    expect(est.stages).toBe(3);
    expect(est.stage_label).toBe("stage");
    expect(est.summary).toMatch(/rerun 3 stages,/);
  });

  test("stage-specific estimate uses the per-stage cost table", () => {
    // video is dominant cost stage: ~$1.20 per stage × 3 stages ≈ $3.60
    const est = estimateDirectiveLocal({ stage: "video" });
    expect(est.dollars).toBeCloseTo(3.6, 2);
    // scenario is near-free: ~$0.05 per stage × 3 stages ≈ $0.15
    const scenario = estimateDirectiveLocal({ stage: "scenario" });
    expect(scenario.dollars).toBeCloseTo(0.15, 2);
  });
});
