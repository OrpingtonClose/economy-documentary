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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { listRecentRuns, startPipelineRun } from "@/lib/api";
import { PipelineApprovalCard } from "./PipelineApprovalCard";
import { PipelineSceneMetrics } from "./PipelineSceneMetrics";
import type {
  RunEvent,
  RunSummary,
  StartPipelineRunResponse,
} from "@/lib/types";
import { useRunStream, type RunStreamState } from "@/lib/useRunStream";

//: Component id used by ``POST /playground/pipeline/runs`` —
//: filters the recent-runs sidebar to pipeline runs only.
const PIPELINE_COMPONENT_ID = "pipeline";

//: localStorage key for the last submitted run id. The page reads
//: it on mount when the URL has no ``?run_id`` so a hard-refresh
//: or fresh tab still finds the in-flight run, and writes it on
//: every successful submit so the lookup is always current.
const LAST_RUN_LS_KEY = "economy-documentary:pipeline:last_run_id";

//: Recent-runs sidebar refresh cadence. 5s is fast enough for the
//: sidebar to surface a freshly-submitted run from another tab
//: without flooding ``GET /playground/runs?limit=N`` with polls.
const RECENT_RUNS_POLL_MS = 5_000;

//: Heartbeat thresholds in seconds. Below ``amber`` the badge
//: stays neutral; above ``red`` it escalates to a warning. The
//: pipeline routinely waits 30–90s on Qwen3-TTS or LTX-2.3 between
//: visible events, so neutral has to extend past that.
const HEARTBEAT_AMBER_SEC = 60;
const HEARTBEAT_RED_SEC = 180;

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
  /** Run id surfaced on the waiting event so the UI can post to
   * ``POST /playground/approval/resume/{run_id}/{interrupt_id}``.
   * ``null`` for legacy events that predate slice 9i.
   */
  readonly runId: string | null;
  /** Interrupt id paired with ``runId``. ``null`` when missing. */
  readonly interruptId: string | null;
  /** Tool args the orchestrator interrupted on. Surfaced so the
   * "Edit" panel can render mutable fields. ``null`` when the gate
   * carries no args. */
  readonly args: Record<string, unknown> | null;
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

  //: URL state lives in three places that must stay in sync:
  //:   1. ``?run_id=<id>`` query param — the canonical source of
  //:      truth, shareable / bookmarkable.
  //:   2. ``runId`` React state — drives the SSE subscription.
  //:   3. ``localStorage[LAST_RUN_LS_KEY]`` — fallback for a fresh
  //:      tab opened directly at ``/pipeline``.
  //: The submit handler writes (1) and (3); the mount effect below
  //: reads (1) preferentially and falls back to (3); ``setRunId``
  //: is the only place (2) is mutated.
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  useEffect(() => {
    const fromUrl = searchParams?.get("run_id");
    if (fromUrl && fromUrl !== runId) {
      setRunId(fromUrl);
      return;
    }
    //: No URL run_id and we have not picked one yet — fall back
    //: to localStorage so a fresh tab that lands on bare
    //: ``/pipeline`` still surfaces the operator's last run.
    if (!fromUrl && runId === null && typeof window !== "undefined") {
      const cached = window.localStorage.getItem(LAST_RUN_LS_KEY);
      if (cached) setRunId(cached);
    }
    //: Intentionally only react to ``searchParams`` changes; we do
    //: not want to clobber a freshly-submitted run with a stale URL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  //: Whenever the active run changes, mirror it into the URL and
  //: localStorage. ``router.replace`` keeps the browser history
  //: clean — no ``?run_id=...`` entry per submit, just a single
  //: replaceable slot that follows the active run.
  useEffect(() => {
    if (!pathname || !router) return;
    const fromUrl = searchParams?.get("run_id") ?? null;
    if (runId && runId !== fromUrl) {
      router.replace(`${pathname}?run_id=${encodeURIComponent(runId)}`);
    }
    if (typeof window !== "undefined" && runId) {
      window.localStorage.setItem(LAST_RUN_LS_KEY, runId);
    }
  }, [runId, pathname, router, searchParams]);

  const stream: RunStreamState = useRunStream(runId);

  //: Live recent-runs feed for the sidebar. Refreshes every
  //: ``RECENT_RUNS_POLL_MS`` and on every successful run dispatch
  //: so a freshly-submitted run appears at the top without
  //: waiting for the next poll tick.
  const recentRuns = useRecentRuns(runId);

  //: Auto-scroll the trajectory log so the latest event is always
  //: in view, but only if the operator hasn't scrolled up to read
  //: history. The ``isPinnedToBottomRef`` flag is set on every
  //: scroll event; when ``true`` we scroll to bottom on each new
  //: event, when ``false`` we leave the scroll position alone.
  const trajectoryRef = useRef<HTMLOListElement | null>(null);
  const isPinnedToBottomRef = useRef<boolean>(true);
  useEffect(() => {
    const node = trajectoryRef.current;
    if (!node || !isPinnedToBottomRef.current) return;
    node.scrollTop = node.scrollHeight;
  }, [stream.events.length]);
  const onTrajectoryScroll = useCallback(
    (event: React.UIEvent<HTMLOListElement>) => {
      const node = event.currentTarget;
      const distanceFromBottom =
        node.scrollHeight - node.scrollTop - node.clientHeight;
      isPinnedToBottomRef.current = distanceFromBottom <= 24;
    },
    [],
  );

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
        //: Persist the new run id to localStorage immediately so a
        //: hard-refresh in the same window still re-attaches to it,
        //: even before the URL effect above runs.
        if (typeof window !== "undefined") {
          window.localStorage.setItem(LAST_RUN_LS_KEY, response.run_id);
        }
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
    <main className="mx-auto grid min-h-screen w-full max-w-7xl grid-cols-1 gap-8 px-6 py-10 lg:grid-cols-[260px_1fr]">
      <PipelineSidebar
        runs={recentRuns}
        activeRunId={runId}
        onSelectRun={setRunId}
      />
      <div className="flex min-w-0 flex-col gap-8">
      <header className="flex flex-col gap-3 border-b border-pg-border pb-8">
        <div className="flex items-center justify-between gap-4">
          <p className="text-xs uppercase tracking-widest text-pg-muted">
            documentary-strands-migration · pipeline
          </p>
          <div className="flex items-center gap-3">
            <HeartbeatBadge
              runId={runId}
              ageSec={stream.lastEventAgeSec}
              connection={stream.connection}
              terminal={stream.terminal}
            />
            <Link
              href="/components"
              className="text-xs text-pg-accent hover:underline"
            >
              ← Components
            </Link>
          </div>
        </div>
        <h1 className="text-3xl font-semibold text-pg-text">
          Documentary Pipeline
        </h1>
        <p className="max-w-3xl text-pg-muted">
          Submit a topic and watch the pipeline drive five stages
          end-to-end: scenario → audio → visual → production →
          assembly. Each stage emits structured events that fold into
          the ribbon and the trajectory log below. Every run drives
          the real DeepAgent orchestrator against real workers and
          real LLM-backed QA gates — there is no scripted-replay or
          simulator path.
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
              <PipelineApprovalCard
                key={`${approval.gate}-${approval.waitingSeq}`}
                approval={approval}
              />
            ))}
          </ul>
          <p className="text-xs text-pg-muted">
            Pending gates wait for an operator decision via
            ``POST /playground/approval/resume/{`{run_id}/{interrupt_id}`}``.
            Unattended runs auto-resume; runs with
            ``ENABLE_PIPELINE_HITL`` set bind on operator input.
          </p>
        </section>
      ) : null}

      <PipelineSceneMetrics events={stream.events} />

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
          {finalMp4Url.startsWith("http") || finalMp4Url.startsWith("/") ? (
            <video
              data-testid="pipeline-final-video"
              src={finalMp4Url}
              controls
              className="w-full max-w-3xl rounded border border-pg-border bg-black"
            >
              Your browser does not support inline video playback.
            </video>
          ) : null}
          <p className="break-all font-mono text-sm text-pg-text">
            {finalMp4Url}
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
            ref={trajectoryRef}
            onScroll={onTrajectoryScroll}
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
      </div>
    </main>
  );
}

// --------------------------------------------------------------------------
// Sidebar — recent runs list. Polls ``GET /playground/runs?limit=N``.
// --------------------------------------------------------------------------

function useRecentRuns(activeRunId: string | null): readonly RunSummary[] {
  const [runs, setRuns] = useState<readonly RunSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const fetchOnce = async () => {
      try {
        const list = await listRecentRuns({
          limit: 20,
          componentId: PIPELINE_COMPONENT_ID,
        });
        if (!cancelled) setRuns(list);
      } catch {
        //: Sidebar is observability — a missed poll is not
        //: actionable. Swallow and try again on the next tick.
      } finally {
        if (!cancelled) {
          timer = setTimeout(fetchOnce, RECENT_RUNS_POLL_MS);
        }
      }
    };
    void fetchOnce();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeRunId]);
  return runs;
}

interface PipelineSidebarProps {
  readonly runs: readonly RunSummary[];
  readonly activeRunId: string | null;
  readonly onSelectRun: (runId: string) => void;
}

function PipelineSidebar({
  runs,
  activeRunId,
  onSelectRun,
}: PipelineSidebarProps) {
  return (
    <aside
      data-testid="pipeline-sidebar"
      className="flex flex-col gap-3 lg:sticky lg:top-6 lg:max-h-[calc(100vh-3rem)] lg:overflow-y-auto"
    >
      <div className="flex flex-col gap-1 border-b border-pg-border pb-3">
        <p className="text-xs uppercase tracking-widest text-pg-muted">
          recent runs
        </p>
        <p className="text-xs text-pg-muted/70">
          last {runs.length} pipeline runs on this server
        </p>
      </div>
      {runs.length === 0 ? (
        <p className="text-xs text-pg-muted">
          No runs yet. Submit the form to start one.
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {runs.map((run) => (
            <li key={run.run_id}>
              <button
                type="button"
                data-testid={`pipeline-sidebar-run-${run.run_id}`}
                onClick={() => onSelectRun(run.run_id)}
                aria-pressed={run.run_id === activeRunId}
                className={`flex w-full flex-col items-start gap-1 rounded border px-2 py-2 text-left text-xs transition hover:border-pg-accent/60 ${
                  run.run_id === activeRunId
                    ? "border-pg-accent bg-pg-accent/10 text-pg-text"
                    : "border-pg-border bg-pg-surface text-pg-muted"
                }`}
              >
                <span className="flex w-full items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-pg-text">
                    {run.run_id.slice(0, 14)}
                  </span>
                  <RunRowStatusDot run={run} />
                </span>
                <span className="line-clamp-2 text-[11px] text-pg-text">
                  {run.topic ?? run.case_name ?? "(no topic)"}
                </span>
                <span className="text-[10px] text-pg-muted/80">
                  {run.target_duration_sec
                    ? `${run.target_duration_sec}s`
                    : "—"}
                  {run.language ? ` · ${run.language}` : ""}
                  {run.event_count > 0
                    ? ` · ${run.event_count} ev`
                    : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}

function RunRowStatusDot({ run }: { readonly run: RunSummary }) {
  let color = "bg-pg-muted";
  let label = "idle";
  if (run.terminal_status === "OK") {
    color = "bg-pg-green";
    label = "ok";
  } else if (
    run.terminal_status &&
    run.terminal_status !== "OK" &&
    run.terminal_status !== "CANCELLED"
  ) {
    color = "bg-pg-red";
    label = "error";
  } else if (run.terminal_status === "CANCELLED") {
    color = "bg-pg-amber";
    label = "cancelled";
  } else if (!run.closed) {
    color = "bg-pg-accent";
    label = "running";
  }
  return (
    <span
      title={label}
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`}
    />
  );
}

// --------------------------------------------------------------------------
// Heartbeat — "last event Ns ago" with color escalation.
// --------------------------------------------------------------------------

interface HeartbeatBadgeProps {
  readonly runId: string | null;
  readonly ageSec: number | null;
  readonly connection: RunStreamState["connection"];
  readonly terminal: RunStreamState["terminal"];
}

function HeartbeatBadge({
  runId,
  ageSec,
  connection,
  terminal,
}: HeartbeatBadgeProps) {
  if (runId === null) return null;
  if (terminal !== null) {
    return (
      <span
        data-testid="pipeline-heartbeat"
        data-state="terminal"
        className="rounded bg-pg-surface px-2 py-1 text-[11px] text-pg-muted"
      >
        run finished
      </span>
    );
  }
  if (connection === "lost") {
    return (
      <span
        data-testid="pipeline-heartbeat"
        data-state="lost"
        className="rounded bg-pg-red/20 px-2 py-1 text-[11px] text-pg-red"
      >
        connection lost
      </span>
    );
  }
  if (ageSec === null) {
    return (
      <span
        data-testid="pipeline-heartbeat"
        data-state="waiting"
        className="rounded bg-pg-surface px-2 py-1 text-[11px] text-pg-muted"
      >
        waiting for first event…
      </span>
    );
  }
  const rounded = Math.max(0, Math.round(ageSec));
  let className =
    "rounded bg-pg-accent/20 px-2 py-1 text-[11px] text-pg-accent";
  let state: "fresh" | "amber" | "red" = "fresh";
  if (ageSec >= HEARTBEAT_RED_SEC) {
    className = "rounded bg-pg-red/20 px-2 py-1 text-[11px] text-pg-red";
    state = "red";
  } else if (ageSec >= HEARTBEAT_AMBER_SEC) {
    className =
      "rounded bg-pg-amber/20 px-2 py-1 text-[11px] text-pg-amber";
    state = "amber";
  }
  return (
    <span
      data-testid="pipeline-heartbeat"
      data-state={state}
      data-age-sec={rounded}
      title="Seconds since the last structured event landed."
      className={className}
    >
      last event {rounded}s ago
    </span>
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
      const runId =
        typeof detail.run_id === "string" && detail.run_id.length > 0
          ? detail.run_id
          : null;
      const interruptId =
        typeof detail.interrupt_id === "string" && detail.interrupt_id.length > 0
          ? detail.interrupt_id
          : null;
      const rawArgs = detail.args;
      const args =
        rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)
          ? (rawArgs as Record<string, unknown>)
          : null;
      waiting.push({
        gate,
        waitingSeq: event.seq,
        resolved: false,
        decision: null,
        runId,
        interruptId,
        args,
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
          runId: null,
          interruptId: null,
          args: null,
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
