/** @jest-environment jsdom */

/**
 * DESIGN-03 (#255) — snapshot coverage for the RestatedBriefCard render
 * shape. We feed the component a fixture via ``initialData`` so the
 * test does not depend on the shared ``/agui/stream`` singleton or on
 * ``fetch`` being reachable.
 */

import { render } from "@testing-library/react";
import {
  RestatedBriefCard,
  formatDurationPlain,
  averageConfidencePct,
} from "@/components/restated-brief-card";

describe("RestatedBriefCard — render shape", () => {
  test("renders the machine's paraphrase when a brief is present", () => {
    const { container, getByTestId } = render(
      <RestatedBriefCard
        initialData={{
          present: true,
          brief_intent: {
            duration_sec: 420,
            tolerance_sec: 30,
            audience: "adhd-friendly",
            tone: ["cinematic", "warm"],
            required_topics: ["Periaqueductal Gray", "fear response"],
            forbidden_topics: ["clickbait"],
            confidence: {
              duration_sec: 0.95,
              audience: 0.9,
              required_topics: 0.8,
            },
          },
        }}
      />,
    );
    expect(getByTestId("restated-brief-duration").textContent).toBe(
      "7 minutes",
    );
    expect(getByTestId("restated-brief-audience").textContent).toBe(
      "ADHD-friendly adults",
    );
    expect(getByTestId("restated-brief-confidence").textContent).toBe(
      "88%",
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  test("renders an empty state when no brief is submitted yet", () => {
    const { container, getByTestId } = render(
      <RestatedBriefCard
        initialData={{ present: false, brief_intent: null }}
      />,
    );
    expect(getByTestId("restated-brief-empty")).toBeInTheDocument();
    expect(container.firstChild).toMatchSnapshot();
  });
});

describe("RestatedBriefCard — formatting helpers", () => {
  test("formatDurationPlain renders minutes in plain English", () => {
    expect(formatDurationPlain(420)).toBe("7 minutes");
    expect(formatDurationPlain(60)).toBe("1 minute");
    expect(formatDurationPlain(90)).toBe("1 minute 30 seconds");
    expect(formatDurationPlain(45)).toBe("45 seconds");
    expect(formatDurationPlain(1)).toBe("1 second");
    expect(formatDurationPlain(0)).toBe("unknown");
    expect(formatDurationPlain(NaN)).toBe("unknown");
  });

  test("averageConfidencePct averages the known confidences", () => {
    expect(
      averageConfidencePct({ a: 1, b: 0.5, c: 0.0 }),
    ).toBe(50);
    expect(averageConfidencePct({})).toBeNull();
    expect(averageConfidencePct(undefined)).toBeNull();
  });
});
