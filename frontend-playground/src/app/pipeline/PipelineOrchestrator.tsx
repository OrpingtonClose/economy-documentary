"use client";

/**
 * Pipeline orchestration workbench.
 *
 * Three sections, top-to-bottom:
 *
 *   1. **Form** — topic, target duration, language. Submit allocates
 *      a run on the backend (``POST /playground/pipeline/runs``)
 *      and subscribes to its SSE stream.
 *   2. **Stage ribbon** — five segments matching ``PIPELINE_STAGES``
 *      (scenario / audio / visual / production / assembly). Each
 *      segment shows idle / running / done / failed driven by the
 *      ``pipeline.stage.<name>.start`` and
 *      ``pipeline.stage.<name>.end`` event kinds.
 *   3. **Trajectory stream** — every event the run emitted, with a
 *      kind chip and a one-line summary. Approval-gate events open
 *      a transient banner; the final ``run_finished`` event surfaces
 *      the MP4 URL.
 *
 * State is intentionally local to the page — pipeline runs are
 * session-scoped, no persistence, no router state.
 */

import { useCallback, useMemo, useState } from "react";
import Link from "next/link";

import { startPipelineRun } from "@/lib/api";
import type { RunEvent, StartPipelineRunResponse } from "@/lib/types";
import { useRunStream, type RunStreamState } from "@/lib/useRunStream";

/**
 * Stable ordered list of pipeline stages, mirrored from
 * :data:`PIPELINE_STAGES` in
 * ``server/strands_agents/playground/pipeline_adapter.py``.
 *
 * Kept hand-written rather than fetched at runtime so the ribbon
 * renders instantly on first paint — five names changing rarely is
 * cheaper than a network round-trip on every page load.
 */
const PIPELINE_STAGES = [
  "scenario",
  "audio",
  "visual",
  "production",
  "assembly",
] as const;
type StageName = (typeof PIPELINE_STAGES)[number];

type StageStatus = "idle" | "running" | "done" | "failed";

interface StageState {
  readonly name: StageName;
  readonly status: StageStatus;
  readonly elapsedMs: number | null;
  readonly sceneCount: number | null;
}

interface ApprovalState {
  readonly gate: string;
  readonly waitingSeq: number;
  readonly resolved: boolean;
  readonly decision: string | null;
}

const LANGUAGE_OPTIONS = [
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
  { value: "ja", label: "Japanese" },
] as const;

const DEFAULT_TOPIC = "The Federal Reserve";
const DEFAULT_DURATION = 60;
const MIN_DURATION = 30;
const MAX_DURATION = 600;

export function PipelineOrchestrator() {
  const [topic, setTopic] = useState<string>(DEFAULT_TOPIC);
  const [durationSec, setDurationSec] = useState<number>(DEFAULT_DURATION);
  const [language, setLanguage] = useState<string>("en");

  const [runMeta, setRunMeta] = useState<StartPipelineRunResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const stream: RunStreamState = useRunStream(runId);

  const stages = useMemo(() => deriveStageStates(stream.events), [
    stream.events,
  ]);
  const approvals = useMemo(() => deriveApprovals(stream.events), [
    stream.events,
  ]);
  const finalMp4Url = useMemo(() => deriveFinalMp4Url(stream), [stream]);
  const totalElapsedMs = stream.terminal?.output
    ? extractElapsedMs(stream.terminal.output)
    : null;

  const onSubmit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSubmitError(null);
      setRunMeta(null);
      setRunId(null);
      const trimmedTopic = topic.trim();
      if (!trimmedTopic) {
        setSubmitError("Topic is required.");
        return;
      }
      if (
        !Number.isFinite(durationSec) ||
        durationSec < MIN_DURATION ||
        durationSec > MAX_DURATION
      ) {
        setSubmitError(
          `Duration must be between ${MIN_DURATION} and ${MAX_DURATION} seconds.`,
        );
        return;
      }
      setIsSubmitting(true);
      try {
        const response = await startPipelineRun({
          topic: trimmedTopic,
          target_duration_sec: Math.floor(durationSec),
          language: language || "en",
        });
        setRunMeta(response);
        setRunId(response.run_id);
      } catch (err) {
        setSubmitError(err instanceof Error ? err.message : String(err));
      } finally {
        setIsSubmitting(false);
      }
    },
    [topic, durationSec, language],
  );

  const isRunning = runId !== null && !stream.terminal;
  const runOk = stream.terminal?.status === "OK";
  const runFailed =
    stream.terminal != null &&
    stream.terminal.status !== "OK" &&
    stream.terminal.status !== "CANCELLED";

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col gap-8 px-8 py-12">
      <header className="flex flex-col gap-3 border-b border-pg-border pb-8">
        <div className="flex items-center justify-between gap-4">
          <p className="text-xs uppercase tracking-widest text-pg-muted">
            documentary-strands-migration · pipeline
          </p>
          <Link
            href="/components"
            className="text-xs text-pg-accent hover:underline"
          >
            ← Components
          </Link>
        </div>
        <h1 className="text-3xl font-semibold text-pg-text">
          Documentary Pipeline
        </h1>
        <p className="max-w-3xl text-pg-muted">
          Submit a topic and watch the pipeline drive five stages
          end-to-end: scenario → audio → visual → production →
          assembly. Each stage emits structured events that fold into
          the ribbon and the trajectory log below. The simulator runs
          deterministically until slice 9 attaches the real
          orchestrator — the wire shape is the same in both modes.
        </p>
      </header>

      <section
        aria-labelledby="pipeline-form-heading"
        className="flex flex-col gap-4 rounded border border-pg-border bg-pg-surface p-6"
      >
        <h2
          id="pipeline-form-heading"
          className="text-lg font-semibold text-pg-text"
        >
          Run inputs
        </h2>
        <form
          onSubmit={onSubmit}
          className="grid grid-cols-1 gap-4 md:grid-cols-[2fr_1fr_1fr_auto]"
        >
          <label className="flex flex-col gap-1 text-sm text-pg-muted">
            <span>Topic</span>
            <input
              type="text"
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              maxLength={200}
              data-testid="pipeline-topic-input"
              placeholder="e.g. The Federal Reserve"
              className="rounded border border-pg-border bg-pg-bg px-3 py-2 text-sm text-pg-text outline-none focus:border-pg-accent"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-pg-muted">
            <span>Duration (sec)</span>
            <input
              type="number"
              min={MIN_DURATION}
              max={MAX_DURATION}
              step={5}
              value={durationSec}
              onChange={(event) =>
                setDurationSec(Number(event.target.value) || 0)
              }
              data-testid="pipeline-duration-input"
              className="rounded border border-pg-border bg-pg-bg px-3 py-2 text-sm text-pg-text outline-none focus:border-pg-accent"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-pg-muted">
            <span>Language</span>
            <select
              value={language}
              onChange={(event) => setLanguage(event.target.value)}
              data-testid="pipeline-language-input"
              className="rounded border border-pg-border bg-pg-bg px-3 py-2 text-sm text-pg-text outline-none focus:border-pg-accent"
            >
              {LANGUAGE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={isSubmitting || isRunning}
              data-testid="pipeline-run-button"
              className="inline-flex w-full items-center justify-center gap-2 rounded bg-pg-accent px-4 py-2 text-sm font-semibold text-pg-bg transition hover:bg-pg-accent/80 disabled:cursor-not-allowed disabled:opacity-50 md:w-auto"
            >
              {isRunning
                ? "Running…"
                : isSubmitting
                  ? "Dispatching…"
                  : "Run pipeline"}
            </button>
          </div>
        </form>
        {submitError ? (
          <p
            role="alert"
            className="text-sm text-pg-red"
            data-testid="pipeline-submit-error"
          >
            {submitError}
          </p>
        ) : null}
      </section>

      <section
        aria-labelledby="pipeline-stages-heading"
        className="flex flex-col gap-4 rounded border border-pg-border bg-pg-surface p-6"
      >
        <div className="flex items-center justify-between">
          <h2
            id="pipeline-stages-heading"
            className="text-lg font-semibold text-pg-text"
          >
            Stage progress
          </h2>
          <RunStatusPill
            connection={stream.connection}
            runOk={runOk}
            runFailed={runFailed}
            isRunning={isRunning}
            hasRun={runId !== null}
          />
        </div>
        <ol
          className="grid grid-cols-1 gap-2 sm:grid-cols-5"
          data-testid="pipeline-stage-ribbon"
        >
          {stages.map((stage) => (
            <StageCell key={stage.name} stage={stage} />
          ))}
        </ol>
        {runMeta ? (
          <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-pg-muted sm:grid-cols-3">
            <div className="flex gap-2">
              <dt className="text-pg-muted/70">run_id</dt>
              <dd className="font-mono text-pg-text">{runMeta.run_id}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-pg-muted/70">topic</dt>
              <dd className="text-pg-text">{runMeta.topic}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-pg-muted/70">duration</dt>
              <dd className="text-pg-text">
                {runMeta.target_duration_sec}s · {runMeta.language}
              </dd>
            </div>
            {totalElapsedMs !== null ? (
              <div className="flex gap-2">
                <dt className="text-pg-muted/70">elapsed</dt>
                <dd className="text-pg-text">{totalElapsedMs}ms</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </section>

      {approvals.length > 0 ? (
        <section
          aria-labelledby="pipeline-approvals-heading"
          className="flex flex-col gap-3 rounded border border-pg-amber/40 bg-pg-amber/5 p-6"
          data-testid="pipeline-approvals"
        >
          <h2
            id="pipeline-approvals-heading"
            className="text-lg font-semibold text-pg-amber"
          >
            Approval gates
          </h2>
          <ul className="flex flex-col gap-2">
            {approvals.map((approval) => (
              <li
                key={`${approval.gate}-${approval.waitingSeq}`}
                className="flex items-center justify-between rounded border border-pg-border bg-pg-surface px-3 py-2 text-sm"
              >
                <span className="font-mono text-pg-text">{approval.gate}</span>
                {approval.resolved ? (
                  <span className="rounded bg-pg-green/20 px-2 py-0.5 text-xs text-pg-green">
                    resumed: {approval.decision ?? "accept"}
                  </span>
                ) : (
                  <span className="rounded bg-pg-amber/20 px-2 py-0.5 text-xs text-pg-amber">
                    waiting…
                  </span>
                )}
              </li>
            ))}
          </ul>
          <p className="text-xs text-pg-muted">
            The simulator auto-resumes every gate. Slice 9 wires real
            human-in-the-loop responses through the same envelope.
          </p>
        </section>
      ) : null}

      {finalMp4Url ? (
        <section
          aria-labelledby="pipeline-final-heading"
          className="flex flex-col gap-3 rounded border border-pg-green/40 bg-pg-green/5 p-6"
          data-testid="pipeline-final"
        >
          <h2
            id="pipeline-final-heading"
            className="text-lg font-semibold text-pg-green"
          >
            Final master MP4
          </h2>
          <p className="break-all font-mono text-sm text-pg-text">
            {finalMp4Url}
          </p>
          <p className="text-xs text-pg-muted">
            B2 URLs aren’t directly playable in the browser. Slice 9
            will swap this for an HTML5 <code>&lt;video&gt;</code>{" "}
            once the assembly leaf publishes a CDN-fronted master.
          </p>
        </section>
      ) : null}

      <section
        aria-labelledby="pipeline-trajectory-heading"
        className="flex flex-col gap-3 rounded border border-pg-border bg-pg-surface p-6"
      >
        <h2
          id="pipeline-trajectory-heading"
          className="text-lg font-semibold text-pg-text"
        >
          Trajectory ({stream.events.length})
        </h2>
        {runId === null ? (
          <p className="text-sm text-pg-muted">
            Submit the form above to start a run. Events will stream
            here as the pipeline advances through each stage.
          </p>
        ) : stream.events.length === 0 ? (
          <p className="text-sm text-pg-muted">
            Subscribed. Waiting for the first event…
          </p>
        ) : (
          <ol
            className="flex max-h-[420px] flex-col gap-1 overflow-y-auto font-mono text-xs"
            data-testid="pipeline-trajectory"
          >
            {stream.events.map((event) => (
              <TrajectoryRow key={event.seq} event={event} />
            ))}
          </ol>
        )}
        {stream.error ? (
          <p
            role="alert"
            className="text-sm text-pg-red"
            data-testid="pipeline-stream-error"
          >
            stream error: {stream.error}
          </p>
        ) : null}
      </section>
    </main>
  );
}

interface RunStatusPillProps {
  readonly connection: RunStreamState["connection"];
  readonly runOk: boolean;
  readonly runFailed: boolean;
  readonly isRunning: boolean;
  readonly hasRun: boolean;
}

function RunStatusPill({
  connection,
  runOk,
  runFailed,
  isRunning,
  hasRun,
}: RunStatusPillProps) {
  if (!hasRun) {
    return (
      <span className="rounded bg-pg-surface px-2 py-1 text-xs text-pg-muted">
        idle
      </span>
    );
  }
  if (runOk) {
    return (
      <span
        data-testid="pipeline-status-pill"
        className="rounded bg-pg-green/20 px-2 py-1 text-xs text-pg-green"
      >
        run.ok
      </span>
    );
  }
  if (runFailed) {
    return (
      <span
        data-testid="pipeline-status-pill"
        className="rounded bg-pg-red/20 px-2 py-1 text-xs text-pg-red"
      >
        run.error
      </span>
    );
  }
  if (isRunning) {
    return (
      <span
        data-testid="pipeline-status-pill"
        className="rounded bg-pg-accent/20 px-2 py-1 text-xs text-pg-accent"
      >
        running…
      </span>
    );
  }
  if (connection === "lost") {
    return (
      <span className="rounded bg-pg-red/20 px-2 py-1 text-xs text-pg-red">
        connection lost
      </span>
    );
  }
  return (
    <span className="rounded bg-pg-surface px-2 py-1 text-xs text-pg-muted">
      {connection}
    </span>
  );
}

function StageCell({ stage }: { readonly stage: StageState }) {
  return (
    <li
      data-testid={`pipeline-stage-${stage.name}`}
      data-status={stage.status}
      className={`flex flex-col gap-1 rounded border px-3 py-2 ${stageCellClass(stage.status)}`}
    >
      <span className="text-xs uppercase tracking-wider">{stage.name}</span>
      <span className="text-sm font-semibold">
        {stageStatusLabel(stage.status)}
      </span>
      {stage.elapsedMs !== null ? (
        <span className="text-xs text-pg-muted">{stage.elapsedMs}ms</span>
      ) : null}
      {stage.sceneCount !== null && stage.status !== "idle" ? (
        <span className="text-xs text-pg-muted">{stage.sceneCount} scenes</span>
      ) : null}
    </li>
  );
}

function stageCellClass(status: StageStatus): string {
  switch (status) {
    case "running":
      return "border-pg-accent/40 bg-pg-accent/10 text-pg-accent";
    case "done":
      return "border-pg-green/40 bg-pg-green/10 text-pg-green";
    case "failed":
      return "border-pg-red/40 bg-pg-red/10 text-pg-red";
    case "idle":
    default:
      return "border-pg-border bg-pg-bg text-pg-muted";
  }
}

function stageStatusLabel(status: StageStatus): string {
  switch (status) {
    case "running":
      return "running";
    case "done":
      return "done";
    case "failed":
      return "failed";
    case "idle":
    default:
      return "—";
  }
}

function TrajectoryRow({ event }: { readonly event: RunEvent }) {
  return (
    <li className="flex items-start gap-2 rounded px-2 py-1 hover:bg-pg-bg">
      <span className="w-10 shrink-0 text-pg-muted">#{event.seq}</span>
      <span
        className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${trajectoryKindClass(event.kind)}`}
      >
        {event.kind}
      </span>
      <span className="text-pg-text">{event.summary}</span>
    </li>
  );
}

function trajectoryKindClass(kind: string): string {
  if (kind === "run.ok") return "bg-pg-green/20 text-pg-green";
  if (kind === "run.error" || kind === "run.cancelled")
    return "bg-pg-red/20 text-pg-red";
  if (kind.startsWith("pipeline.stage.")) {
    if (kind.endsWith(".end")) return "bg-pg-green/20 text-pg-green";
    return "bg-pg-accent/20 text-pg-accent";
  }
  if (kind.startsWith("pipeline.approval.")) return "bg-pg-amber/20 text-pg-amber";
  if (kind.startsWith("pipeline.tool."))
    return "bg-pg-infra/20 text-pg-infra";
  if (kind === "pipeline.artifact") return "bg-pg-green/10 text-pg-green";
  if (kind === "pipeline.run_started" || kind === "pipeline.run_finished")
    return "bg-pg-accent/20 text-pg-accent";
  if (kind === "pipeline.unknown") return "bg-pg-amber/20 text-pg-amber";
  return "bg-pg-surface text-pg-muted";
}

// --------------------------------------------------------------------------
// Pure event-folding helpers (UI state derivations).
// --------------------------------------------------------------------------

function initialStages(): readonly StageState[] {
  return PIPELINE_STAGES.map((name) => ({
    name,
    status: "idle" as StageStatus,
    elapsedMs: null,
    sceneCount: null,
  }));
}

/**
 * Fold the event stream into per-stage status cells. Two event kinds
 * matter: ``pipeline.stage.<name>.start`` flips the cell to running
 * and records ``scene_count``; ``pipeline.stage.<name>.end`` flips
 * it to done and records ``elapsed_ms``. A terminal ``run.error``
 * marks any in-flight stage as failed.
 */
export function deriveStageStates(
  events: readonly RunEvent[],
): readonly StageState[] {
  const byName = new Map<StageName, StageState>();
  for (const stage of initialStages()) {
    byName.set(stage.name, stage);
  }
  for (const event of events) {
    const start = matchStageEvent(event.kind, ".start");
    const end = matchStageEvent(event.kind, ".end");
    if (start) {
      const detail = event.detail ?? {};
      const sceneCount =
        typeof detail.scene_count === "number" ? detail.scene_count : null;
      byName.set(start, {
        name: start,
        status: "running",
        elapsedMs: null,
        sceneCount,
      });
    } else if (end) {
      const prev = byName.get(end);
      const detail = event.detail ?? {};
      const elapsedMs =
        typeof detail.elapsed_ms === "number" ? detail.elapsed_ms : null;
      byName.set(end, {
        name: end,
        status: "done",
        elapsedMs,
        sceneCount: prev?.sceneCount ?? null,
      });
    } else if (event.kind === "pipeline.stage_failed") {
      const detail = event.detail ?? {};
      const stage = typeof detail.stage === "string" ? detail.stage : null;
      if (stage && isPipelineStageName(stage)) {
        const prev = byName.get(stage);
        byName.set(stage, {
          name: stage,
          status: "failed",
          elapsedMs: prev?.elapsedMs ?? null,
          sceneCount: prev?.sceneCount ?? null,
        });
      }
    } else if (event.kind === "run.error") {
      for (const [name, state] of byName.entries()) {
        if (state.status === "running") {
          byName.set(name, { ...state, status: "failed" });
        }
      }
    }
  }
  return PIPELINE_STAGES.map(
    (name) => byName.get(name) ?? { name, status: "idle", elapsedMs: null, sceneCount: null },
  );
}

function matchStageEvent(kind: string, suffix: string): StageName | null {
  const prefix = "pipeline.stage.";
  if (!kind.startsWith(prefix) || !kind.endsWith(suffix)) {
    return null;
  }
  const name = kind.slice(prefix.length, kind.length - suffix.length);
  return isPipelineStageName(name) ? name : null;
}

function isPipelineStageName(name: string): name is StageName {
  return (PIPELINE_STAGES as readonly string[]).includes(name);
}

/**
 * Pair ``pipeline.approval.waiting`` events with their matching
 * ``pipeline.approval.resumed`` events. Returns a list ordered by
 * the waiting event's seq, so the UI can show oldest gates first.
 */
export function deriveApprovals(
  events: readonly RunEvent[],
): readonly ApprovalState[] {
  const waiting: ApprovalState[] = [];
  for (const event of events) {
    if (event.kind === "pipeline.approval.waiting") {
      const detail = event.detail ?? {};
      const gate = typeof detail.gate_name === "string" ? detail.gate_name : "";
      waiting.push({
        gate,
        waitingSeq: event.seq,
        resolved: false,
        decision: null,
      });
    } else if (event.kind === "pipeline.approval.resumed") {
      const detail = event.detail ?? {};
      const gate = typeof detail.gate_name === "string" ? detail.gate_name : "";
      const decision =
        typeof detail.decision === "string" ? detail.decision : null;
      // Resolve the latest unresolved entry for this gate. New gates
      // (no prior waiting event) shouldn't happen in the simulator,
      // but if they do we surface a synthetic resolved entry.
      let resolved = false;
      for (let i = waiting.length - 1; i >= 0; i -= 1) {
        if (waiting[i].gate === gate && !waiting[i].resolved) {
          waiting[i] = { ...waiting[i], resolved: true, decision };
          resolved = true;
          break;
        }
      }
      if (!resolved) {
        waiting.push({
          gate,
          waitingSeq: event.seq,
          resolved: true,
          decision,
        });
      }
    }
  }
  return waiting;
}

function deriveFinalMp4Url(stream: RunStreamState): string | null {
  // Prefer the terminal payload — slice 7 puts the final URL there
  // explicitly. Fall back to scanning for the pipeline.run_finished
  // event in case a UI reload races the terminal write.
  const fromTerminal =
    stream.terminal?.output && typeof stream.terminal.output === "object"
      ? (stream.terminal.output as Record<string, unknown>).final_mp4_b2_url
      : undefined;
  if (typeof fromTerminal === "string" && fromTerminal.length > 0) {
    return fromTerminal;
  }
  for (let i = stream.events.length - 1; i >= 0; i -= 1) {
    const event = stream.events[i];
    if (event.kind === "pipeline.run_finished") {
      const detail = event.detail ?? {};
      const url = detail.final_mp4_b2_url;
      if (typeof url === "string" && url.length > 0) {
        return url;
      }
    }
  }
  return null;
}

function extractElapsedMs(output: unknown): number | null {
  if (output && typeof output === "object" && !Array.isArray(output)) {
    const elapsed = (output as Record<string, unknown>).elapsed_ms;
    if (typeof elapsed === "number") {
      return elapsed;
    }
  }
  return null;
}
