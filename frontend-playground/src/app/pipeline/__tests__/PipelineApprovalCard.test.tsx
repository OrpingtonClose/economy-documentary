/**
 * @jest-environment jsdom
 */

/**
 * Component tests for the pipeline approval-gate card.
 *
 * The card is the only UI surface that turns a pending gate event
 * into an HTTP decision. These tests pin down:
 *
 *  1. Approve dispatches ``{ type: "accept" }`` to
 *     :func:`resolvePipelineApproval` with the run/interrupt ids
 *     from the parent. Field names mirror the backend
 *     ``ApprovalDecision`` TypedDict exactly — the SDK term is
 *     ``accept``, not ``approve``.
 *  2. Reject dispatches ``{ type: "reject", reason: ... }``,
 *     defaulting the reason string when none is typed.
 *  3. Edit toggles the inline JSON editor, validates the textarea
 *     contents, and dispatches ``{ type: "edit", args, reason? }``
 *     on submit (``args`` is the backend field name; ``reason`` is
 *     omitted when blank).
 *  4. Invalid JSON in the edits textarea surfaces an error and
 *     does NOT dispatch.
 *  5. A gate received without resume coordinates renders disabled
 *     buttons and a fallback hint instead of dispatching.
 *  6. Resolved gates collapse to a "resumed: …" pill — no buttons.
 */

import "@testing-library/jest-dom";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import {
  PipelineApprovalCard,
  type ApprovalCardApproval,
} from "../PipelineApprovalCard";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  resolvePipelineApproval: jest.fn(),
}));

import { resolvePipelineApproval } from "@/lib/api";

const mockResolve = resolvePipelineApproval as jest.MockedFunction<
  typeof resolvePipelineApproval
>;

function pendingApproval(
  overrides: Partial<ApprovalCardApproval> = {},
): ApprovalCardApproval {
  return {
    gate: "launch_visual_production",
    waitingSeq: 7,
    resolved: false,
    decision: null,
    runId: "run-abc",
    interruptId: "int-789",
    args: { scene_id: "s1", prompt: "wide shot" },
    ...overrides,
  };
}

function renderCard(
  approval: ApprovalCardApproval,
  onResolved: jest.Mock | null = null,
): void {
  render(
    <ul>
      <PipelineApprovalCard approval={approval} onResolved={onResolved} />
    </ul>,
  );
}

afterEach(() => {
  mockResolve.mockReset();
});

describe("PipelineApprovalCard — pending gate", () => {
  it("renders gate name and waiting pill", () => {
    renderCard(pendingApproval());

    expect(screen.getByText("launch_visual_production")).toBeInTheDocument();
    expect(
      screen.getByTestId("pipeline-approval-status-waiting"),
    ).toHaveTextContent("waiting");
  });

  it("Approve button posts { type: 'accept' } with run/interrupt ids", async () => {
    mockResolve.mockResolvedValueOnce({
      status: "ok",
      decision_type: "accept",
    });
    const onResolved = jest.fn();
    renderCard(pendingApproval(), onResolved);

    fireEvent.click(screen.getByTestId("pipeline-approval-approve"));

    await waitFor(() => {
      expect(mockResolve).toHaveBeenCalledTimes(1);
    });
    expect(mockResolve).toHaveBeenCalledWith("run-abc", "int-789", {
      type: "accept",
    });
    expect(onResolved).toHaveBeenCalledWith("accept");
  });

  it("Reject button posts { type: 'reject', reason } with default reason", async () => {
    mockResolve.mockResolvedValueOnce({
      status: "ok",
      decision_type: "reject",
    });
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-reject"));

    await waitFor(() => {
      expect(mockResolve).toHaveBeenCalledTimes(1);
    });
    expect(mockResolve).toHaveBeenCalledWith("run-abc", "int-789", {
      type: "reject",
      reason: "Rejected by operator",
    });
  });

  it("Edit toggle reveals the inline editor pre-populated with args", () => {
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-edit-toggle"));

    const textarea = screen.getByTestId(
      "pipeline-approval-edits-textarea",
    ) as HTMLTextAreaElement;
    const parsed = JSON.parse(textarea.value);
    expect(parsed).toEqual({ scene_id: "s1", prompt: "wide shot" });
  });

  it("Edit submit posts { type: 'edit', args, reason } with mutated JSON", async () => {
    mockResolve.mockResolvedValueOnce({
      status: "ok",
      decision_type: "edit",
    });
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-edit-toggle"));
    fireEvent.change(screen.getByTestId("pipeline-approval-edits-textarea"), {
      target: {
        value: JSON.stringify({ scene_id: "s1", prompt: "tighter framing" }),
      },
    });
    fireEvent.change(screen.getByTestId("pipeline-approval-feedback-input"), {
      target: { value: "trim by 5s" },
    });
    fireEvent.click(screen.getByTestId("pipeline-approval-edit-submit"));

    await waitFor(() => {
      expect(mockResolve).toHaveBeenCalledTimes(1);
    });
    expect(mockResolve).toHaveBeenCalledWith("run-abc", "int-789", {
      type: "edit",
      args: { scene_id: "s1", prompt: "tighter framing" },
      reason: "trim by 5s",
    });
  });

  it("Edit submit omits 'reason' when feedback input is blank", async () => {
    mockResolve.mockResolvedValueOnce({
      status: "ok",
      decision_type: "edit",
    });
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-edit-toggle"));
    fireEvent.change(screen.getByTestId("pipeline-approval-edits-textarea"), {
      target: {
        value: JSON.stringify({ scene_id: "s1", prompt: "tighter framing" }),
      },
    });
    fireEvent.click(screen.getByTestId("pipeline-approval-edit-submit"));

    await waitFor(() => {
      expect(mockResolve).toHaveBeenCalledTimes(1);
    });
    expect(mockResolve).toHaveBeenCalledWith("run-abc", "int-789", {
      type: "edit",
      args: { scene_id: "s1", prompt: "tighter framing" },
    });
  });

  it("Invalid JSON in edits surfaces error and does NOT dispatch", async () => {
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-edit-toggle"));
    fireEvent.change(screen.getByTestId("pipeline-approval-edits-textarea"), {
      target: { value: "{not json" },
    });
    fireEvent.click(screen.getByTestId("pipeline-approval-edit-submit"));

    expect(
      await screen.findByTestId("pipeline-approval-error"),
    ).toHaveTextContent(/Invalid JSON/);
    expect(mockResolve).not.toHaveBeenCalled();
  });

  it("Non-object JSON in edits (e.g. array) is rejected", async () => {
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-edit-toggle"));
    fireEvent.change(screen.getByTestId("pipeline-approval-edits-textarea"), {
      target: { value: "[1, 2, 3]" },
    });
    fireEvent.click(screen.getByTestId("pipeline-approval-edit-submit"));

    expect(
      await screen.findByTestId("pipeline-approval-error"),
    ).toHaveTextContent(/JSON object/);
    expect(mockResolve).not.toHaveBeenCalled();
  });

  it("Surfaces backend error message when resolve rejects", async () => {
    mockResolve.mockRejectedValueOnce(new Error("404 not pending"));
    renderCard(pendingApproval());

    fireEvent.click(screen.getByTestId("pipeline-approval-approve"));

    expect(
      await screen.findByTestId("pipeline-approval-error"),
    ).toHaveTextContent(/404 not pending/);
  });

  it("Disables buttons + shows hint when run_id missing", () => {
    renderCard(pendingApproval({ runId: null }));

    expect(screen.getByTestId("pipeline-approval-approve")).toBeDisabled();
    expect(screen.getByTestId("pipeline-approval-edit-toggle")).toBeDisabled();
    expect(screen.getByTestId("pipeline-approval-reject")).toBeDisabled();
    expect(
      screen.getByText(/Gate received without resume coordinates/i),
    ).toBeInTheDocument();
  });

  it("Disables buttons when interrupt_id missing", () => {
    renderCard(pendingApproval({ interruptId: null }));

    expect(screen.getByTestId("pipeline-approval-approve")).toBeDisabled();
    expect(screen.getByTestId("pipeline-approval-edit-toggle")).toBeDisabled();
    expect(screen.getByTestId("pipeline-approval-reject")).toBeDisabled();
  });
});

describe("PipelineApprovalCard — resolved gate", () => {
  it("collapses to a 'resumed: …' pill with no buttons", () => {
    renderCard({
      gate: "launch_assembly",
      waitingSeq: 11,
      resolved: true,
      decision: "accept",
      runId: "run-abc",
      interruptId: "int-555",
      args: null,
    });

    expect(
      screen.getByTestId("pipeline-approval-status-resolved"),
    ).toHaveTextContent(/resumed: accept/);
    expect(
      screen.queryByTestId("pipeline-approval-approve"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("pipeline-approval-edit-toggle"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("pipeline-approval-reject"),
    ).not.toBeInTheDocument();
  });

  it("falls back to 'accept' label when decision is null", () => {
    renderCard({
      gate: "launch_assembly",
      waitingSeq: 12,
      resolved: true,
      decision: null,
      runId: null,
      interruptId: null,
      args: null,
    });

    expect(
      screen.getByTestId("pipeline-approval-status-resolved"),
    ).toHaveTextContent(/resumed: accept/);
  });
});
