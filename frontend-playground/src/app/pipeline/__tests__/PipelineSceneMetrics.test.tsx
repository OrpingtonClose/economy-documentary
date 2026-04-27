/**
 * @jest-environment jsdom
 */

/**
 * Tests for the per-scene QA metrics surface on ``/pipeline``.
 *
 * The directive is "if metrics aren't blatantly obvious from the
 * UI you failed". These tests pin the contract:
 *
 *   1. ``deriveSceneMetrics`` extracts envelope fields verbatim
 *      from ``pipeline.tool.<name>.end`` events into per-scene
 *      rows, in first-seen order.
 *   2. The row renders PASS/FAIL pills + measured values inline,
 *      with ``data-testid`` hooks for every metric the user is
 *      supposed to be able to read at a glance.
 *   3. Master-row at the bottom rolls up scene count, total
 *      duration, and final verdict (any FAIL → fail; all PASS →
 *      pass; otherwise pending).
 *   4. The slice 9j frozen-frame regression (audio 13.0s × video
 *      3.7s → ``qa_duration_align`` FAIL with delta ≈ 9.3s) is
 *      the canonical fixture, rendered with a red ``fail`` pill
 *      and the literal ``Δ = 9.30s`` next to it.
 */

import "@testing-library/jest-dom";

import { render, screen, within } from "@testing-library/react";

import {
  PipelineSceneMetrics,
  deriveSceneMetrics,
} from "../PipelineSceneMetrics";
import type { RunEvent } from "@/lib/types";

function event(
  seq: number,
  kind: string,
  detail: Record<string, unknown>,
  summary = "",
): RunEvent {
  return {
    seq,
    ts: seq,
    kind,
    summary: summary || kind,
    detail,
  };
}

function audioRender(seq: number, sceneId: string, duration: number): RunEvent {
  return event(seq, "pipeline.tool.launch_audio_render.end", {
    tool: "launch_audio_render",
    elapsed_ms: 1000,
    ok: true,
    envelope: {
      scene_id: sceneId,
      duration_s: duration,
      wav_bytes_len: 250000,
      engine: "qwen3-tts-12hz-1.7b-customvoice",
    },
  });
}

function visualProduction(
  seq: number,
  sceneId: string,
  duration: number,
): RunEvent {
  return event(seq, "pipeline.tool.launch_visual_production.end", {
    tool: "launch_visual_production",
    elapsed_ms: 5000,
    ok: true,
    envelope: {
      scene_id: sceneId,
      duration_s: duration,
      mp4_bytes_len: 800000,
      engine: "ltx-video",
    },
  });
}

function audioCompleteness(
  seq: number,
  sceneId: string,
  verdict: "pass" | "fail",
  trailingSilenceS: number,
  tailRmsDb: number,
  reason: string | null = null,
): RunEvent {
  const envelope: Record<string, unknown> = {
    scene_id: sceneId,
    verdict,
    audio_duration_s: 13.0,
    trailing_silence_s: trailingSilenceS,
    tail_rms_db: tailRmsDb,
    min_trailing_silence_s: 0.15,
    max_tail_rms_db: -25.0,
  };
  if (reason) envelope.reason = reason;
  return event(seq, "pipeline.tool.qa_audio_completeness.end", {
    tool: "qa_audio_completeness",
    elapsed_ms: 100,
    ok: true,
    envelope,
  });
}

function durationAlign(
  seq: number,
  sceneId: string,
  audio: number,
  video: number,
  verdict: "pass" | "fail",
  reason: string | null = null,
): RunEvent {
  const envelope: Record<string, unknown> = {
    scene_id: sceneId,
    verdict,
    audio_duration_s: audio,
    video_duration_s: video,
    delta_s: Math.abs(audio - video),
    tolerance_s: 0.5,
  };
  if (reason) envelope.reason = reason;
  return event(seq, "pipeline.tool.qa_duration_align.end", {
    tool: "qa_duration_align",
    elapsed_ms: 50,
    ok: true,
    envelope,
  });
}

function stillsJudge(
  seq: number,
  sceneId: string,
  meanPixelDelta: number,
  verdict: "pass" | "fail",
  reason: string | null = null,
): RunEvent {
  const envelope: Record<string, unknown> = {
    scene_id: sceneId,
    verdict,
    num_samples: 8,
    mean_pixel_delta: meanPixelDelta,
    min_mean_pixel_delta: 1.0,
  };
  if (reason) envelope.reason = reason;
  return event(seq, "pipeline.tool.qa_stills_judge.end", {
    tool: "qa_stills_judge",
    elapsed_ms: 200,
    ok: true,
    envelope,
  });
}

describe("deriveSceneMetrics", () => {
  it("returns an empty list for an empty event stream", () => {
    expect(deriveSceneMetrics([])).toHaveLength(0);
  });

  it("ignores non-tool-end events", () => {
    expect(
      deriveSceneMetrics([
        event(1, "pipeline.run_started", {}),
        event(2, "pipeline.stage.scenario.start", {}),
      ]),
    ).toHaveLength(0);
  });

  it("extracts audio + video durations + bytes per scene", () => {
    const rows = deriveSceneMetrics([
      audioRender(1, "scene-1", 12.5),
      visualProduction(2, "scene-1", 5.0),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].sceneId).toBe("scene-1");
    expect(rows[0].audioDurationS).toBe(12.5);
    expect(rows[0].videoDurationS).toBe(5.0);
    expect(rows[0].audioBytes).toBe(250000);
    expect(rows[0].videoBytes).toBe(800000);
  });

  it("extracts qa_audio_completeness verdict + trailing_silence + tail_rms", () => {
    const [row] = deriveSceneMetrics([
      audioRender(1, "scene-1", 12.5),
      audioCompleteness(2, "scene-1", "pass", 0.18, -44.0),
    ]);
    expect(row.audioCompleteness).toEqual({
      verdict: "pass",
      tailRmsDb: -44.0,
      trailingSilenceS: 0.18,
      minTrailingSilenceS: 0.15,
      maxTailRmsDb: -25.0,
      reason: null,
    });
  });

  it("extracts qa_duration_align verdict + delta_s + tolerance_s", () => {
    const [row] = deriveSceneMetrics([
      durationAlign(1, "scene-1", 13.0, 3.7, "fail"),
    ]);
    expect(row.durationAlign?.verdict).toBe("fail");
    expect(row.durationAlign?.deltaS).toBeCloseTo(9.3, 1);
    expect(row.durationAlign?.toleranceS).toBe(0.5);
  });

  it("extracts qa_stills_judge mean_pixel_delta + verdict", () => {
    const [row] = deriveSceneMetrics([
      stillsJudge(1, "scene-1", 8.7, "pass"),
    ]);
    expect(row.stillsJudge?.verdict).toBe("pass");
    expect(row.stillsJudge?.meanPixelDelta).toBeCloseTo(8.7);
    expect(row.stillsJudge?.minMeanPixelDelta).toBe(1.0);
    expect(row.stillsJudge?.numSamples).toBe(8);
  });

  it("preserves first-seen scene order across out-of-order events", () => {
    const rows = deriveSceneMetrics([
      audioRender(1, "scene-2", 10),
      audioRender(2, "scene-1", 11),
      visualProduction(3, "scene-1", 5),
      visualProduction(4, "scene-2", 5),
    ]);
    expect(rows.map((r) => r.sceneId)).toEqual(["scene-2", "scene-1"]);
  });

  it("merges multiple QA gate events for the same scene without dropping fields", () => {
    const rows = deriveSceneMetrics([
      audioRender(1, "scene-1", 12.5),
      visualProduction(2, "scene-1", 5.0),
      audioCompleteness(3, "scene-1", "pass", 0.2, -42),
      durationAlign(4, "scene-1", 12.5, 5.0, "fail"),
      stillsJudge(5, "scene-1", 9.0, "pass"),
    ]);
    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row.audioCompleteness?.verdict).toBe("pass");
    expect(row.durationAlign?.verdict).toBe("fail");
    expect(row.stillsJudge?.verdict).toBe("pass");
    // Earlier render values are preserved when later QA events
    // also report the same field.
    expect(row.audioBytes).toBe(250000);
    expect(row.videoBytes).toBe(800000);
  });

  it("ignores tool-end events with no scene_id in the envelope", () => {
    expect(
      deriveSceneMetrics([
        event(1, "pipeline.tool.launch_audio_render.end", {
          tool: "launch_audio_render",
          envelope: { duration_s: 5.0 },
        }),
      ]),
    ).toHaveLength(0);
  });
});

describe("PipelineSceneMetrics rendering", () => {
  it("renders the empty state when no QA events have arrived", () => {
    render(<PipelineSceneMetrics events={[]} />);
    expect(
      screen.getByTestId("pipeline-scene-metrics-empty"),
    ).toBeInTheDocument();
  });

  it("renders one row per scene with PASS/FAIL pills + measured values", () => {
    render(
      <PipelineSceneMetrics
        events={[
          audioRender(1, "scene-1", 12.5),
          audioCompleteness(2, "scene-1", "pass", 0.18, -44.0),
          visualProduction(3, "scene-1", 5.0),
          durationAlign(4, "scene-1", 12.5, 5.0, "fail"),
          stillsJudge(5, "scene-1", 0.4, "fail", "below floor"),
        ]}
      />,
    );

    const row = screen.getByTestId("pipeline-metrics-row-scene-1");
    expect(within(row).getByText("scene-1")).toBeInTheDocument();

    expect(
      screen.getByTestId("pipeline-metrics-scene-1-audio-completeness-verdict"),
    ).toHaveAttribute("data-verdict", "pass");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-duration-align-verdict"),
    ).toHaveAttribute("data-verdict", "fail");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-stills-judge-verdict"),
    ).toHaveAttribute("data-verdict", "fail");

    expect(
      screen.getByTestId("pipeline-metrics-scene-1-audio-duration"),
    ).toHaveTextContent("12.50s");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-video-duration"),
    ).toHaveTextContent("5.00s");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-tail-rms-db"),
    ).toHaveTextContent("-44.0 dBFS");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-trailing-silence-s"),
    ).toHaveTextContent("0.18s");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-delta-s"),
    ).toHaveTextContent("7.50s");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-mean-pixel-delta"),
    ).toHaveTextContent("0.400");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-stills-judge-reason"),
    ).toHaveTextContent("below floor");
  });

  it("rolls up the master verdict to FAIL when any scene gate fails", () => {
    render(
      <PipelineSceneMetrics
        events={[
          audioRender(1, "scene-1", 12.5),
          audioCompleteness(2, "scene-1", "pass", 0.2, -44),
          visualProduction(3, "scene-1", 5.0),
          durationAlign(4, "scene-1", 12.5, 5.0, "fail"),
          stillsJudge(5, "scene-1", 9.0, "pass"),
        ]}
      />,
    );
    expect(
      screen.getByTestId("pipeline-metrics-master-verdict"),
    ).toHaveAttribute("data-verdict", "fail");
  });

  it("rolls up the master verdict to PASS when every scene gate passes", () => {
    render(
      <PipelineSceneMetrics
        events={[
          audioRender(1, "scene-1", 12.5),
          audioCompleteness(2, "scene-1", "pass", 0.2, -44),
          visualProduction(3, "scene-1", 12.5),
          durationAlign(4, "scene-1", 12.5, 12.5, "pass"),
          stillsJudge(5, "scene-1", 9.0, "pass"),
          audioRender(6, "scene-2", 10.0),
          audioCompleteness(7, "scene-2", "pass", 0.2, -44),
          visualProduction(8, "scene-2", 10.0),
          durationAlign(9, "scene-2", 10.0, 10.0, "pass"),
          stillsJudge(10, "scene-2", 7.5, "pass"),
        ]}
      />,
    );
    expect(
      screen.getByTestId("pipeline-metrics-master-verdict"),
    ).toHaveAttribute("data-verdict", "pass");
    expect(
      screen.getByTestId("pipeline-metrics-master-audio"),
    ).toHaveTextContent("22.50s");
    expect(
      screen.getByTestId("pipeline-metrics-master-video"),
    ).toHaveTextContent("22.50s");
  });

  it("renders the slice-9j frozen-frame regression with delta ≈ 9.3s and a red FAIL pill", () => {
    render(
      <PipelineSceneMetrics
        events={[
          audioRender(1, "scene-1", 13.0),
          visualProduction(2, "scene-1", 3.7),
          durationAlign(3, "scene-1", 13.0, 3.7, "fail"),
        ]}
      />,
    );
    const verdict = screen.getByTestId(
      "pipeline-metrics-scene-1-duration-align-verdict",
    );
    expect(verdict).toHaveAttribute("data-verdict", "fail");
    expect(verdict).toHaveTextContent("fail");
    expect(
      screen.getByTestId("pipeline-metrics-scene-1-delta-s"),
    ).toHaveTextContent("9.30s");
  });
});
