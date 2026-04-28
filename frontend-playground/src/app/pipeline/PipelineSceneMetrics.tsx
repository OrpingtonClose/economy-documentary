"use client";

/**
 * Per-scene QA metrics card grid for the ``/pipeline`` page.
 *
 * Every QA gate verdict and measurement the orchestrator emits in
 * the trajectory is lifted into a per-scene row here. If a metric
 * isn't visible on this surface, the pipeline failed — that's the
 * directive in AGENTS.md hard invariant §5 ("QA immediately after
 * each artifact, never batch QA at the end") rendered as a UI
 * contract.
 *
 * The data sources are the ``pipeline.tool.<name>.end`` events
 * emitted by ``server/strands_agents/playground/pipeline_adapter.py``
 * (``_tool_kind(tool, "end")``). Each carries an ``envelope`` dict
 * lifted verbatim from the underlying tool's return value, which
 * for the QA gates is shaped by
 * ``server/strands_agents/qa_gates.py``:
 *
 *   - ``launch_audio_render``  → ``{scene_id, duration_s, wav_bytes_len, ...}``
 *   - ``qa_audio_completeness``→ ``{scene_id, verdict, audio_duration_s,
 *                                  trailing_silence_s, tail_rms_db,
 *                                  min_trailing_silence_s, max_tail_rms_db,
 *                                  reason?}``
 *   - ``launch_visual_production`` → ``{scene_id, duration_s, mp4_bytes_len, ...}``
 *   - ``qa_duration_align``   → ``{scene_id, verdict, audio_duration_s,
 *                                  video_duration_s, delta_s,
 *                                  tolerance_s, reason?}``
 *   - ``qa_stills_judge``     → ``{scene_id, verdict, num_samples,
 *                                  mean_pixel_delta, min_mean_pixel_delta,
 *                                  reason?}``
 *
 * Failure values are rendered in red with the exact failing
 * measurement inline so the regression mode is obvious at a glance,
 * not buried in an event-detail expandable.
 */

import type { RunEvent } from "@/lib/types";

export interface SceneAudioCompletenessMetric {
  readonly verdict: string;
  readonly tailRmsDb: number | null;
  readonly trailingSilenceS: number | null;
  readonly minTrailingSilenceS: number | null;
  readonly maxTailRmsDb: number | null;
  readonly reason: string | null;
}

export interface SceneDurationAlignMetric {
  readonly verdict: string;
  readonly audioDurationS: number | null;
  readonly videoDurationS: number | null;
  readonly deltaS: number | null;
  readonly toleranceS: number | null;
  readonly reason: string | null;
}

export interface SceneStillsJudgeMetric {
  readonly verdict: string;
  readonly meanPixelDelta: number | null;
  readonly minMeanPixelDelta: number | null;
  readonly numSamples: number | null;
  readonly reason: string | null;
}

export interface SceneMetrics {
  readonly sceneId: string;
  readonly orderSeq: number;
  readonly audioDurationS: number | null;
  readonly videoDurationS: number | null;
  readonly audioBytes: number | null;
  readonly videoBytes: number | null;
  readonly audioCompleteness: SceneAudioCompletenessMetric | null;
  readonly durationAlign: SceneDurationAlignMetric | null;
  readonly stillsJudge: SceneStillsJudgeMetric | null;
}

const TOOL_KIND_PREFIX = "pipeline.tool.";
const END_SUFFIX = ".end";

function readEnvelope(event: RunEvent): Record<string, unknown> | null {
  const detail = event.detail;
  if (!detail || typeof detail !== "object") return null;
  const envelope = (detail as Record<string, unknown>).envelope;
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
    return null;
  }
  return envelope as Record<string, unknown>;
}

function toolNameFromKind(kind: string): string | null {
  if (!kind.startsWith(TOOL_KIND_PREFIX) || !kind.endsWith(END_SUFFIX)) {
    return null;
  }
  return kind.slice(TOOL_KIND_PREFIX.length, kind.length - END_SUFFIX.length);
}

function readNumber(envelope: Record<string, unknown>, key: string): number | null {
  const v = envelope[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function readString(envelope: Record<string, unknown>, key: string): string | null {
  const v = envelope[key];
  return typeof v === "string" && v.length > 0 ? v : null;
}

/**
 * Fold the trajectory into a per-scene metrics map. Pure function;
 * the React component just renders what this returns.
 */
export function deriveSceneMetrics(
  events: readonly RunEvent[],
): readonly SceneMetrics[] {
  const byScene = new Map<string, SceneMetrics>();
  const orderBySeq = new Map<string, number>();

  function getOrCreate(sceneId: string, seq: number): SceneMetrics {
    if (!orderBySeq.has(sceneId)) orderBySeq.set(sceneId, seq);
    const prior = byScene.get(sceneId);
    if (prior) return prior;
    const seed: SceneMetrics = {
      sceneId,
      orderSeq: orderBySeq.get(sceneId) ?? seq,
      audioDurationS: null,
      videoDurationS: null,
      audioBytes: null,
      videoBytes: null,
      audioCompleteness: null,
      durationAlign: null,
      stillsJudge: null,
    };
    byScene.set(sceneId, seed);
    return seed;
  }

  for (const event of events) {
    const tool = toolNameFromKind(event.kind);
    if (!tool) continue;
    const envelope = readEnvelope(event);
    if (!envelope) continue;
    const sceneId = readString(envelope, "scene_id");
    if (!sceneId) continue;

    const prev = getOrCreate(sceneId, event.seq);

    if (tool === "launch_audio_render") {
      byScene.set(sceneId, {
        ...prev,
        audioDurationS: readNumber(envelope, "duration_s") ?? prev.audioDurationS,
        audioBytes: readNumber(envelope, "wav_bytes_len") ?? prev.audioBytes,
      });
    } else if (tool === "launch_visual_production") {
      byScene.set(sceneId, {
        ...prev,
        videoDurationS: readNumber(envelope, "duration_s") ?? prev.videoDurationS,
        videoBytes: readNumber(envelope, "mp4_bytes_len") ?? prev.videoBytes,
      });
    } else if (tool === "qa_audio_completeness") {
      byScene.set(sceneId, {
        ...prev,
        audioDurationS:
          readNumber(envelope, "audio_duration_s") ?? prev.audioDurationS,
        audioCompleteness: {
          verdict: readString(envelope, "verdict") ?? "unknown",
          tailRmsDb: readNumber(envelope, "tail_rms_db"),
          trailingSilenceS: readNumber(envelope, "trailing_silence_s"),
          minTrailingSilenceS: readNumber(envelope, "min_trailing_silence_s"),
          maxTailRmsDb: readNumber(envelope, "max_tail_rms_db"),
          reason: readString(envelope, "reason"),
        },
      });
    } else if (tool === "qa_duration_align") {
      byScene.set(sceneId, {
        ...prev,
        audioDurationS:
          readNumber(envelope, "audio_duration_s") ?? prev.audioDurationS,
        videoDurationS:
          readNumber(envelope, "video_duration_s") ?? prev.videoDurationS,
        durationAlign: {
          verdict: readString(envelope, "verdict") ?? "unknown",
          audioDurationS: readNumber(envelope, "audio_duration_s"),
          videoDurationS: readNumber(envelope, "video_duration_s"),
          deltaS: readNumber(envelope, "delta_s"),
          toleranceS: readNumber(envelope, "tolerance_s"),
          reason: readString(envelope, "reason"),
        },
      });
    } else if (tool === "qa_stills_judge") {
      byScene.set(sceneId, {
        ...prev,
        stillsJudge: {
          verdict: readString(envelope, "verdict") ?? "unknown",
          meanPixelDelta: readNumber(envelope, "mean_pixel_delta"),
          minMeanPixelDelta: readNumber(envelope, "min_mean_pixel_delta"),
          numSamples: readNumber(envelope, "num_samples"),
          reason: readString(envelope, "reason"),
        },
      });
    }
  }

  return Array.from(byScene.values()).sort((a, b) => a.orderSeq - b.orderSeq);
}

// --------------------------------------------------------------------------
// Render
// --------------------------------------------------------------------------

function VerdictPill({
  verdict,
  testid,
}: {
  readonly verdict: string | null | undefined;
  readonly testid?: string;
}) {
  if (!verdict) {
    return (
      <span
        data-testid={testid}
        data-verdict="pending"
        className="inline-flex items-center rounded bg-pg-surface px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pg-muted"
      >
        pending
      </span>
    );
  }
  const lower = verdict.toLowerCase();
  if (lower === "pass") {
    return (
      <span
        data-testid={testid}
        data-verdict="pass"
        className="inline-flex items-center rounded bg-pg-green/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pg-green"
      >
        pass
      </span>
    );
  }
  if (lower === "fail") {
    return (
      <span
        data-testid={testid}
        data-verdict="fail"
        className="inline-flex items-center rounded bg-pg-red/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pg-red"
      >
        fail
      </span>
    );
  }
  return (
    <span
      data-testid={testid}
      data-verdict={lower}
      className="inline-flex items-center rounded bg-pg-amber/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pg-amber"
    >
      {lower}
    </span>
  );
}

function fmtSeconds(value: number | null): string {
  if (value === null) return "—";
  return `${value.toFixed(2)}s`;
}

function fmtDb(value: number | null): string {
  if (value === null) return "—";
  return `${value.toFixed(1)} dBFS`;
}

function fmtBytes(value: number | null): string {
  if (value === null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}

function fmtNumber(value: number | null, digits = 2): string {
  if (value === null) return "—";
  return value.toFixed(digits);
}

function failingClass(failing: boolean): string {
  return failing ? "text-pg-red font-semibold" : "text-pg-text";
}

interface PipelineSceneMetricsProps {
  readonly events: readonly RunEvent[];
}

export function PipelineSceneMetrics({ events }: PipelineSceneMetricsProps) {
  const scenes = deriveSceneMetrics(events);

  if (scenes.length === 0) {
    return (
      <section
        aria-labelledby="pipeline-metrics-heading"
        className="flex flex-col gap-3 rounded border border-pg-border bg-pg-surface p-6"
        data-testid="pipeline-scene-metrics-empty"
      >
        <h2
          id="pipeline-metrics-heading"
          className="text-lg font-semibold text-pg-text"
        >
          Per-scene quality checks
        </h2>
        <p className="text-sm text-pg-muted">
          Once each scene has been rendered we run a sound check, a
          timing check, and a visual check on it. Results will appear
          here as scenes finish.
        </p>
      </section>
    );
  }

  const masterAudio = scenes.reduce(
    (acc, s) => acc + (s.audioDurationS ?? 0),
    0,
  );
  const masterVideo = scenes.reduce(
    (acc, s) => acc + (s.videoDurationS ?? 0),
    0,
  );
  const allPass =
    scenes.length > 0 &&
    scenes.every(
      (s) =>
        s.audioCompleteness?.verdict?.toLowerCase() === "pass" &&
        s.durationAlign?.verdict?.toLowerCase() === "pass" &&
        s.stillsJudge?.verdict?.toLowerCase() === "pass",
    );
  const anyFail = scenes.some(
    (s) =>
      s.audioCompleteness?.verdict?.toLowerCase() === "fail" ||
      s.durationAlign?.verdict?.toLowerCase() === "fail" ||
      s.stillsJudge?.verdict?.toLowerCase() === "fail",
  );

  const masterVerdict: "pass" | "fail" | "pending" = anyFail
    ? "fail"
    : allPass
      ? "pass"
      : "pending";

  return (
    <section
      aria-labelledby="pipeline-metrics-heading"
      className="flex flex-col gap-4 rounded border border-pg-border bg-pg-surface p-6"
      data-testid="pipeline-scene-metrics"
    >
      <div className="flex items-center justify-between">
        <h2
          id="pipeline-metrics-heading"
          className="text-lg font-semibold text-pg-text"
        >
          Per-scene quality checks ({scenes.length})
        </h2>
        <VerdictPill
          verdict={masterVerdict}
          testid="pipeline-metrics-master-verdict"
        />
      </div>
      <p className="text-xs text-pg-muted">
        Each scene goes through three quick checks before it lands in
        the final video. Green means the scene is good to ship; red
        means we need to fix that scene before assembling the master.
      </p>
      <div className="overflow-x-auto">
        <table
          className="min-w-full table-auto border-collapse text-xs"
          data-testid="pipeline-metrics-table"
        >
          <thead>
            <tr className="border-b border-pg-border text-left text-[10px] uppercase tracking-wider text-pg-muted">
              <th className="px-2 py-2">Scene</th>
              <th className="px-2 py-2">Narration</th>
              <th className="px-2 py-2">Visuals</th>
              <th
                className="px-2 py-2"
                title="Sound check — confirms the narration is clean and ends naturally."
              >
                Sound check
              </th>
              <th
                className="px-2 py-2"
                title="Timing check — confirms the visuals match the narration length."
              >
                Timing check
              </th>
              <th
                className="px-2 py-2"
                title="Visual check — confirms the video has motion (not a frozen still)."
              >
                Visual check
              </th>
            </tr>
          </thead>
          <tbody>
            {scenes.map((scene) => (
              <SceneRow key={scene.sceneId} scene={scene} />
            ))}
          </tbody>
          <tfoot>
            <tr
              className="border-t-2 border-pg-border bg-pg-bg/40"
              data-testid="pipeline-metrics-master-row"
            >
              <td className="px-2 py-2 font-semibold text-pg-text">
                MASTER
              </td>
              <td
                className="px-2 py-2 font-mono text-pg-text"
                data-testid="pipeline-metrics-master-audio"
              >
                {fmtSeconds(masterAudio)}
              </td>
              <td
                className="px-2 py-2 font-mono text-pg-text"
                data-testid="pipeline-metrics-master-video"
              >
                {fmtSeconds(masterVideo)}
              </td>
              <td className="px-2 py-2 text-pg-muted" colSpan={3}>
                {scenes.length} scene{scenes.length === 1 ? "" : "s"} ·{" "}
                final verdict:{" "}
                <VerdictPill verdict={masterVerdict} />
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}

function SceneRow({ scene }: { readonly scene: SceneMetrics }) {
  const audioFail = scene.audioCompleteness?.verdict?.toLowerCase() === "fail";
  const durationFail = scene.durationAlign?.verdict?.toLowerCase() === "fail";
  const stillsFail = scene.stillsJudge?.verdict?.toLowerCase() === "fail";

  return (
    <tr
      className="border-b border-pg-border align-top"
      data-testid={`pipeline-metrics-row-${scene.sceneId}`}
      data-scene-id={scene.sceneId}
    >
      <td className="px-2 py-2 font-mono font-semibold text-pg-text">
        {scene.sceneId}
      </td>
      <td className="px-2 py-2 font-mono text-pg-text">
        <div data-testid={`pipeline-metrics-${scene.sceneId}-audio-duration`}>
          {fmtSeconds(scene.audioDurationS)}
        </div>
        <div className="text-[10px] text-pg-muted">
          {fmtBytes(scene.audioBytes)}
        </div>
      </td>
      <td className="px-2 py-2 font-mono text-pg-text">
        <div data-testid={`pipeline-metrics-${scene.sceneId}-video-duration`}>
          {fmtSeconds(scene.videoDurationS)}
        </div>
        <div className="text-[10px] text-pg-muted">
          {fmtBytes(scene.videoBytes)}
        </div>
      </td>
      <td
        className="px-2 py-2"
        data-testid={`pipeline-metrics-${scene.sceneId}-audio-completeness`}
      >
        <div className="flex items-center gap-2">
          <VerdictPill
            verdict={scene.audioCompleteness?.verdict ?? null}
            testid={`pipeline-metrics-${scene.sceneId}-audio-completeness-verdict`}
          />
          <span className="text-[11px] text-pg-text">
            {audioCheckSummary(scene.audioCompleteness)}
          </span>
        </div>
        <details className="mt-1 text-[10px]">
          <summary className="cursor-pointer text-pg-muted">
            details
          </summary>
          <div className="mt-1 flex flex-col gap-0.5 font-mono">
            <span className={failingClass(audioFail)}>
              tail_rms ={" "}
              <span
                data-testid={`pipeline-metrics-${scene.sceneId}-tail-rms-db`}
              >
                {fmtDb(scene.audioCompleteness?.tailRmsDb ?? null)}
              </span>
            </span>
            <span className={failingClass(audioFail)}>
              silence ={" "}
              <span
                data-testid={`pipeline-metrics-${scene.sceneId}-trailing-silence-s`}
              >
                {fmtSeconds(scene.audioCompleteness?.trailingSilenceS ?? null)}
              </span>
            </span>
            {scene.audioCompleteness?.reason ? (
              <span
                className="text-pg-red"
                data-testid={`pipeline-metrics-${scene.sceneId}-audio-completeness-reason`}
              >
                {scene.audioCompleteness.reason}
              </span>
            ) : null}
          </div>
        </details>
      </td>
      <td
        className="px-2 py-2"
        data-testid={`pipeline-metrics-${scene.sceneId}-duration-align`}
      >
        <div className="flex items-center gap-2">
          <VerdictPill
            verdict={scene.durationAlign?.verdict ?? null}
            testid={`pipeline-metrics-${scene.sceneId}-duration-align-verdict`}
          />
          <span className="text-[11px] text-pg-text">
            {durationCheckSummary(scene.durationAlign)}
          </span>
        </div>
        <details className="mt-1 text-[10px]">
          <summary className="cursor-pointer text-pg-muted">
            details
          </summary>
          <div className="mt-1 flex flex-col gap-0.5 font-mono">
            <span className={failingClass(durationFail)}>
              Δ ={" "}
              <span
                data-testid={`pipeline-metrics-${scene.sceneId}-delta-s`}
              >
                {fmtSeconds(scene.durationAlign?.deltaS ?? null)}
              </span>
            </span>
            <span className="text-pg-muted">
              tol = {fmtSeconds(scene.durationAlign?.toleranceS ?? null)}
            </span>
            {scene.durationAlign?.reason ? (
              <span
                className="text-pg-red"
                data-testid={`pipeline-metrics-${scene.sceneId}-duration-align-reason`}
              >
                {scene.durationAlign.reason}
              </span>
            ) : null}
          </div>
        </details>
      </td>
      <td
        className="px-2 py-2"
        data-testid={`pipeline-metrics-${scene.sceneId}-stills-judge`}
      >
        <div className="flex items-center gap-2">
          <VerdictPill
            verdict={scene.stillsJudge?.verdict ?? null}
            testid={`pipeline-metrics-${scene.sceneId}-stills-judge-verdict`}
          />
          <span className="text-[11px] text-pg-text">
            {stillsCheckSummary(scene.stillsJudge)}
          </span>
        </div>
        <details className="mt-1 text-[10px]">
          <summary className="cursor-pointer text-pg-muted">
            details
          </summary>
          <div className="mt-1 flex flex-col gap-0.5 font-mono">
            <span className={failingClass(stillsFail)}>
              mean_delta ={" "}
              <span
                data-testid={`pipeline-metrics-${scene.sceneId}-mean-pixel-delta`}
              >
                {fmtNumber(scene.stillsJudge?.meanPixelDelta ?? null, 3)}
              </span>
            </span>
            <span className="text-pg-muted">
              floor ={" "}
              {fmtNumber(scene.stillsJudge?.minMeanPixelDelta ?? null, 3)}
            </span>
            {scene.stillsJudge?.reason ? (
              <span
                className="text-pg-red"
                data-testid={`pipeline-metrics-${scene.sceneId}-stills-judge-reason`}
              >
                {scene.stillsJudge.reason}
              </span>
            ) : null}
          </div>
        </details>
      </td>
    </tr>
  );
}


/**
 * Plain-English summary of the audio completeness check.
 *
 * The persona-test feedback was that ``tail_rms = -44 dBFS`` /
 * ``silence = 180ms`` is meaningless to a non-technical operator
 * — the same data needs a one-line human label so they know
 * whether to be calm or worried even before they expand the
 * details.
 */
function audioCheckSummary(
  ac: SceneMetrics["audioCompleteness"] | undefined,
): string {
  if (!ac || !ac.verdict) {
    return "checking…";
  }
  const verdict = ac.verdict.toLowerCase();
  if (verdict === "pass") {
    return "narration sounds clean";
  }
  if (verdict === "fail") {
    return "narration cut off or noisy";
  }
  return "checking…";
}

/** Plain-English summary of the duration-align check. */
function durationCheckSummary(
  da: SceneMetrics["durationAlign"] | undefined,
): string {
  if (!da || !da.verdict) {
    return "checking…";
  }
  const verdict = da.verdict.toLowerCase();
  if (verdict === "pass") {
    return "visuals match narration length";
  }
  if (verdict === "fail") {
    return "visuals are off-length vs narration";
  }
  return "checking…";
}

/** Plain-English summary of the stills-judge check. */
function stillsCheckSummary(
  sj: SceneMetrics["stillsJudge"] | undefined,
): string {
  if (!sj || !sj.verdict) {
    return "checking…";
  }
  const verdict = sj.verdict.toLowerCase();
  if (verdict === "pass") {
    return "video has motion";
  }
  if (verdict === "fail") {
    return "video looks frozen";
  }
  return "checking…";
}
