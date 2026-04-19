"use client";

/**
 * DESIGN-03 (#255) — "What I heard you ask for" card.
 *
 * A plain-English paraphrase of the user's brief that sits above the
 * film timeline so drift between what the user said and what the
 * machine understood is always one glance away.
 *
 * Data source: ``GET /agui/restated_brief`` (INTENT-03, #267). The
 * endpoint returns ``{brief_intent, present}``; when ``present`` is
 * false we render the empty state ("Submit a brief to see what I
 * understood.") instead of a fake card.
 *
 * The card re-fetches on mount and whenever the shared ``/agui/stream``
 * emits a ``run_started`` event so a fresh run replaces a stale
 * paraphrase without the user having to reload.
 *
 * Copy is deliberately plain English — no ``RUN_STARTED``, ``SSE``,
 * ``slot``, ``directive`` or any other internal vocabulary (DESIGN-01
 * acceptance criterion, mirrored here for the card).
 */

import * as React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { subscribeAguiStream } from "@/lib/agui-stream";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/**
 * Shape of the ``brief_intent`` payload returned by
 * ``/agui/restated_brief`` — mirrors
 * :class:`server.agents.intent_extractor.BriefIntent`.
 */
export interface BriefIntentPayload {
  duration_sec: number;
  tolerance_sec?: number;
  audience?: string;
  tone?: string[];
  corpus_paths?: string[];
  required_topics?: string[];
  forbidden_topics?: string[];
  format_hints?: Record<string, unknown>;
  confidence?: Record<string, number>;
}

export interface RestatedBriefResponse {
  brief_intent: BriefIntentPayload | null;
  present: boolean;
  error?: string;
}

export interface RestatedBriefCardProps {
  /**
   * Optional fixture override — when supplied we skip the fetch and
   * render directly. Used by jest snapshot tests so the component's
   * render shape can be exercised without a live backend.
   */
  initialData?: RestatedBriefResponse;
  className?: string;
}

type FetchState =
  | { status: "loading" }
  | { status: "ready"; data: RestatedBriefResponse }
  | { status: "error"; message: string };

/**
 * Format a duration given in seconds as plain-English minutes.
 *
 *   420  -> "7 minutes"
 *   90   -> "1 minute 30 seconds"
 *   45   -> "45 seconds"
 */
export function formatDurationPlain(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return "unknown";
  const total = Math.round(sec);
  if (total < 60) return `${total} ${total === 1 ? "second" : "seconds"}`;
  const mins = Math.floor(total / 60);
  const rem = total - mins * 60;
  const minsLabel = `${mins} ${mins === 1 ? "minute" : "minutes"}`;
  if (rem === 0) return minsLabel;
  return `${minsLabel} ${rem} ${rem === 1 ? "second" : "seconds"}`;
}

/** Average of known confidence values, expressed as a whole percentage. */
export function averageConfidencePct(
  confidence: Record<string, number> | undefined,
): number | null {
  if (!confidence) return null;
  const values = Object.values(confidence).filter(
    (v) => typeof v === "number" && Number.isFinite(v),
  );
  if (values.length === 0) return null;
  const mean = values.reduce((acc, v) => acc + v, 0) / values.length;
  return Math.round(mean * 100);
}

/** Human-readable audience label. */
function humaniseAudience(value: string | undefined): string {
  if (!value) return "Anyone";
  const v = value.trim().toLowerCase();
  if (v === "adhd-friendly" || v === "adhd_friendly" || v === "adhd") {
    return "ADHD-friendly adults";
  }
  if (v === "general") return "General audience";
  if (v === "expert") return "Subject-matter experts";
  // Capitalise first letter as a safe default.
  return value.charAt(0).toUpperCase() + value.slice(1);
}

async function fetchRestatedBrief(): Promise<RestatedBriefResponse> {
  const res = await fetch(`${BACKEND_URL}/agui/restated_brief`);
  if (!res.ok) {
    throw new Error(`restated_brief HTTP ${res.status}`);
  }
  return (await res.json()) as RestatedBriefResponse;
}

export function RestatedBriefCard({
  initialData,
  className,
}: RestatedBriefCardProps = {}): React.ReactElement {
  const [state, setState] = React.useState<FetchState>(
    initialData
      ? { status: "ready", data: initialData }
      : { status: "loading" },
  );
  const [open, setOpen] = React.useState<boolean>(true);

  const reload = React.useCallback(async () => {
    try {
      const data = await fetchRestatedBrief();
      setState({ status: "ready", data });
    } catch (err) {
      setState({
        status: "error",
        message: err instanceof Error ? err.message : "unknown error",
      });
    }
  }, []);

  // Initial fetch on mount — skipped if the caller provided a fixture
  // (snapshot tests pass ``initialData`` directly).
  React.useEffect(() => {
    if (initialData) return;
    void reload();
  }, [initialData, reload]);

  // Re-fetch whenever a new pipeline run starts so the card cannot go
  // stale across runs. We hook both the CustomEvent-style
  // ``run_started`` and the raw ``message`` channel in case an upstream
  // emits a ``RUN_STARTED`` typed event without a named dispatch.
  React.useEffect(() => {
    if (initialData) return;
    const unsubscribe = subscribeAguiStream({
      events: ["run_started"],
      onEvent: () => {
        void reload();
      },
    });
    return unsubscribe;
  }, [initialData, reload]);

  return (
    <Card
      data-testid="restated-brief-card"
      className={cn("border bg-card", className)}
    >
      <Collapsible open={open} onOpenChange={setOpen}>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1 min-w-0">
              <CardTitle className="text-base">
                Here&apos;s what I heard you ask for.
              </CardTitle>
              <CardDescription>
                {state.status === "ready" && state.data.present
                  ? "If any of this looks wrong, tell me in chat and I'll update it."
                  : "Submit a brief to see what I understood."}
              </CardDescription>
            </div>
            <CollapsibleTrigger
              aria-label={open ? "Collapse summary" : "Expand summary"}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              data-testid="restated-brief-toggle"
            >
              {open ? (
                <ChevronDown className="h-4 w-4" aria-hidden />
              ) : (
                <ChevronRight className="h-4 w-4" aria-hidden />
              )}
            </CollapsibleTrigger>
          </div>
        </CardHeader>
        <CollapsibleContent>
          <CardContent className="pt-0">
            <RestatedBriefBody state={state} />
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

function RestatedBriefBody({
  state,
}: {
  state: FetchState;
}): React.ReactElement {
  if (state.status === "loading") {
    return (
      <p
        className="text-sm text-muted-foreground"
        data-testid="restated-brief-loading"
      >
        Reading your brief&hellip;
      </p>
    );
  }
  if (state.status === "error") {
    // Per DESIGN-01 rule #3: don't show red on the primary surface
    // unless the user needs to act. A transient read error while the
    // run is warming up isn't actionable — render it muted. We only
    // refresh on mount or on a ``run_started`` event, so the copy is
    // deliberately honest about that cadence rather than promising a
    // retry loop the component does not implement.
    return (
      <p
        className="text-sm text-muted-foreground"
        data-testid="restated-brief-error"
      >
        I couldn&apos;t read the brief just now. It&apos;ll update the next
        time a run starts.
      </p>
    );
  }
  const { brief_intent, present } = state.data;
  if (!present || !brief_intent) {
    return (
      <p
        className="text-sm text-muted-foreground"
        data-testid="restated-brief-empty"
      >
        Nothing to show yet. Type a topic into the chat on the left and
        I&apos;ll paraphrase it back here so you can check I got it right.
      </p>
    );
  }

  const {
    duration_sec,
    audience,
    tone,
    required_topics,
    forbidden_topics,
    confidence,
  } = brief_intent;
  const confPct = averageConfidencePct(confidence);
  const requiredList = required_topics ?? [];
  const forbiddenList = forbidden_topics ?? [];
  const toneList = tone ?? [];

  return (
    <div className="flex flex-col gap-4" data-testid="restated-brief-body">
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">
          Length
        </span>
        <span
          className="text-3xl font-semibold tabular-nums"
          data-testid="restated-brief-duration"
        >
          {formatDurationPlain(duration_sec)}
        </span>
      </div>

      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">
            Audience
          </dt>
          <dd
            className="text-sm font-medium"
            data-testid="restated-brief-audience"
          >
            {humaniseAudience(audience)}
          </dd>
        </div>
        <div className="flex flex-col gap-1">
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">
            Tone
          </dt>
          <dd className="flex flex-wrap gap-1">
            {toneList.length === 0 ? (
              <span className="text-sm text-muted-foreground">
                No specific tone requested
              </span>
            ) : (
              toneList.map((t) => (
                <Badge
                  key={`tone-${t}`}
                  variant="secondary"
                  data-testid="restated-brief-tone"
                >
                  {t}
                </Badge>
              ))
            )}
          </dd>
        </div>
      </dl>

      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">
          Must cover
        </span>
        <div className="flex flex-wrap gap-1">
          {requiredList.length === 0 ? (
            <span className="text-sm text-muted-foreground">
              Nothing pinned — I&apos;ll pick a structure from the corpus.
            </span>
          ) : (
            requiredList.map((topic) => (
              <Badge
                key={`req-${topic}`}
                variant="default"
                data-testid="restated-brief-required"
              >
                {topic}
              </Badge>
            ))
          )}
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">
          Steer clear of
        </span>
        <div className="flex flex-wrap gap-1">
          {forbiddenList.length === 0 ? (
            <span className="text-sm text-muted-foreground">
              No topics excluded.
            </span>
          ) : (
            forbiddenList.map((topic) => (
              <Badge
                key={`fbd-${topic}`}
                variant="outline"
                className="text-muted-foreground"
                data-testid="restated-brief-forbidden"
              >
                {topic}
              </Badge>
            ))
          )}
        </div>
      </div>

      {confPct !== null ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>How sure I am overall:</span>
          <span
            className="font-medium text-foreground tabular-nums"
            data-testid="restated-brief-confidence"
          >
            {confPct}%
          </span>
        </div>
      ) : null}
    </div>
  );
}
