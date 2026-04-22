/** @jest-environment jsdom */

/**
 * DESIGN-09 (#261): HaltPauseButton tests.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HaltPauseButton } from "@/components/halt-pause-button";

describe("HaltPauseButton", () => {
  test("hides when the pipeline is idle", () => {
    render(
      <HaltPauseButton
        running={false}
        halted={false}
        onConfirmPause={() => {}}
      />,
    );
    expect(screen.queryByTestId("halt-pause-button")).toBeNull();
  });

  test("hides when the halt flag is already engaged", () => {
    render(
      <HaltPauseButton
        running
        halted
        onConfirmPause={() => {}}
      />,
    );
    expect(screen.queryByTestId("halt-pause-button")).toBeNull();
  });

  test("renders amber 'Pause production' copy when running and not halted", () => {
    render(
      <HaltPauseButton
        running
        halted={false}
        onConfirmPause={() => {}}
      />,
    );
    const btn = screen.getByTestId("halt-pause-button");
    expect(btn).toHaveTextContent("Pause production");
    // amber styling (not red / destructive).
    expect(btn.className).toMatch(/amber/);
    expect(btn.className).not.toMatch(/(^|\s)bg-red-/);
    expect(btn.className).not.toMatch(/(^|\s)bg-destructive/);
  });

  test("shows 'Pausing…' while submitting", () => {
    render(
      <HaltPauseButton
        running
        halted={false}
        submitting
        onConfirmPause={() => {}}
      />,
    );
    expect(screen.getByTestId("halt-pause-button")).toHaveTextContent(
      /Pausing/,
    );
    expect(screen.getByTestId("halt-pause-button")).toBeDisabled();
  });

  test("click opens the cost-preview dialog with the plain-English pause copy, not a numeric estimate", async () => {
    const user = userEvent.setup();
    render(
      <HaltPauseButton
        running
        halted={false}
        onConfirmPause={() => {}}
      />,
    );
    await user.click(screen.getByTestId("halt-pause-button"));
    await waitFor(() => {
      expect(screen.getByTestId("halt-pause-dialog")).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("halt-pause-dialog-description"),
    ).toHaveTextContent(
      /pause at the next safe checkpoint, nothing lost . resume anytime/i,
    );
    // No numeric cost is shown -- pausing is free.
    expect(screen.queryByTestId("halt-pause-dialog-summary")).toBeNull();
    expect(screen.queryByTestId("halt-pause-dialog-badge-stages")).toBeNull();
  });

  test("confirming the dialog calls onConfirmPause once", async () => {
    const user = userEvent.setup();
    const onConfirmPause = jest.fn().mockResolvedValue(undefined);
    render(
      <HaltPauseButton
        running
        halted={false}
        onConfirmPause={onConfirmPause}
      />,
    );
    await user.click(screen.getByTestId("halt-pause-button"));
    await waitFor(() => {
      expect(screen.getByTestId("halt-pause-dialog")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("halt-pause-dialog-confirm"));
    await waitFor(() => {
      expect(onConfirmPause).toHaveBeenCalledTimes(1);
    });
  });

  test("cancelling the dialog does not call onConfirmPause", async () => {
    const user = userEvent.setup();
    const onConfirmPause = jest.fn();
    render(
      <HaltPauseButton
        running
        halted={false}
        onConfirmPause={onConfirmPause}
      />,
    );
    await user.click(screen.getByTestId("halt-pause-button"));
    await waitFor(() => {
      expect(screen.getByTestId("halt-pause-dialog")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("halt-pause-dialog-cancel"));
    expect(onConfirmPause).not.toHaveBeenCalled();
  });
});
