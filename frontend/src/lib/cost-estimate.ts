/**
 * Shared cost-estimate helpers for DESIGN-07 (#259).
 *
 * Every control action that reruns work or changes the plan shows a
 * plain-English cost preview before it fires. The estimate has three
 * numbers: how many stages/scenes will be rerun, roughly how many
 * minutes of extra runtime it adds, and roughly how many dollars of
 * GPU time it will cost.
 *
 * The preferred source of truth is the backend ``POST
 * /agui/estimate_directive`` helper endpoint, but we fall back to a
 * deterministic client-side heuristic so the dialog still renders if
 * the backend is unreachable or the endpoint hasn't been wired up yet.
 * The client-side numbers are deliberately conservative: stages × an
 * average GPU cost. A proper server-side estimate is a backend TODO.
 */
export type CostEstimate = {
  /** How many stages / scenes will be rerun. */
  stages: number;
  /**
   * Human-friendly label for the unit we are counting -- "scene(s)" by
   * default for scene-scoped work, "stage(s)" for pipeline-wide actions.
   * Used verbatim inside the plain-English summary.
   */
  stage_label: string;
  /** Rough added runtime, in minutes. */
  eta_minutes: number;
  /** Rough added cost, in US dollars. */
  dollars: number;
  /** Preferred plain-English summary sentence. */
  summary: string;
  /** Optional backend-provided note (e.g. "estimate is a placeholder"). */
  note?: string;
};

/**
 * Context used to compute a cost estimate. Shape mirrors the payload
 * the directive endpoint already accepts so callers can reuse it.
 */
export type DirectiveCostContext = {
  /** The directive text (used by the backend for finer-grained sizing). */
  directive?: string;
  /**
   * Optional slot context: when set we know exactly one scene is
   * affected, which shrinks the estimate dramatically. Matches the
   * :class:`SlotContext` shape used by the intervention bar.
   */
  slot_context?: Record<string, unknown> | null;
  /**
   * Optional explicit stage name (used by the rewind dropdown -- one
   * of the pipeline stages from ``KNOWN_PIPELINE_STAGES``).
   */
  stage?: string | null;
  /**
   * Short action description used in the fallback summary, e.g.
   * "Redo this scene" or "Rewind to narration".
   */
  action?: string;
};

// Deliberately conservative per-stage GPU-time estimates. These are
// client-side fallbacks only; the backend estimate is the source of
// truth and should replace them once wired up.
//
// The numbers assume a typical 5-scene documentary:
//   scenario ≈ 1 min / $0.05  (LLM only, near-free)
//   visual_director ≈ 3 min / $0.20  (LLM only)
//   audio ≈ 5 min / $0.40  (TTS worker)
//   video ≈ 8 min / $1.20  (LTX GPU, dominant cost)
//   assembly ≈ 2 min / $0.10  (CPU ffmpeg)
const PER_STAGE_MINUTES: Record<string, number> = {
  scenario: 1,
  visual_director: 3,
  audio: 5,
  video: 8,
  assembly: 2,
  // generic fallback used when the stage is not recognised -- roughly
  // equivalent to a single video regen.
  __fallback__: 7,
};
const PER_STAGE_DOLLARS: Record<string, number> = {
  scenario: 0.05,
  visual_director: 0.2,
  audio: 0.4,
  video: 1.2,
  assembly: 0.1,
  __fallback__: 0.7,
};

function stageMinutes(stage?: string | null): number {
  if (stage && stage in PER_STAGE_MINUTES) return PER_STAGE_MINUTES[stage];
  return PER_STAGE_MINUTES.__fallback__;
}
function stageDollars(stage?: string | null): number {
  if (stage && stage in PER_STAGE_DOLLARS) return PER_STAGE_DOLLARS[stage];
  return PER_STAGE_DOLLARS.__fallback__;
}

/**
 * Produce a deterministic client-side estimate. Used as the fallback
 * when the backend helper endpoint is unavailable.
 */
export function estimateDirectiveLocal(
  ctx: DirectiveCostContext,
): CostEstimate {
  // Scope inference: a slot context with a scene/clip reference means
  // exactly one scene is affected; otherwise assume three scenes (the
  // median documentary touches roughly half the scenes on a directive).
  const slot = ctx.slot_context ?? null;
  const sceneScoped =
    slot != null &&
    (slot.scene_num !== undefined ||
      slot.scene_id !== undefined ||
      slot.clip_id !== undefined ||
      slot.voice_block_id !== undefined);
  const stages = sceneScoped ? 1 : 3;
  const minutesPerStage = stageMinutes(ctx.stage);
  const dollarsPerStage = stageDollars(ctx.stage);
  const eta_minutes = Math.max(1, Math.round(stages * minutesPerStage));
  const dollars = Math.round(stages * dollarsPerStage * 100) / 100;
  const stage_label = sceneScoped ? "scene" : "scene";
  const unit =
    stages === 1 ? `${stage_label}` : `${stage_label}s`;
  const summary = `This will rerun ${stages} ${unit}, add about ${eta_minutes} minutes, and cost about $${dollars.toFixed(2)}.`;
  return {
    stages,
    stage_label,
    eta_minutes,
    dollars,
    summary,
    note: "Client-side estimate (backend not wired up yet).",
  };
}

type EstimateResponse = Partial<CostEstimate>;

/**
 * Fetch a cost estimate from the backend helper endpoint, falling back
 * to the deterministic client-side heuristic on any network / parse
 * error. Never throws: the UI always gets a usable estimate.
 */
export async function fetchDirectiveEstimate(
  ctx: DirectiveCostContext,
  opts: { backendUrl?: string; signal?: AbortSignal } = {},
): Promise<CostEstimate> {
  const backendUrl =
    opts.backendUrl ??
    (typeof process !== "undefined"
      ? process.env.NEXT_PUBLIC_BACKEND_URL
      : undefined) ??
    "http://localhost:8000";
  try {
    const res = await fetch(`${backendUrl}/agui/estimate_directive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ctx),
      signal: opts.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as EstimateResponse;
    if (
      typeof data.stages === "number" &&
      typeof data.eta_minutes === "number" &&
      typeof data.dollars === "number"
    ) {
      const stages = data.stages;
      const stage_label = data.stage_label ?? "scene";
      const eta_minutes = data.eta_minutes;
      const dollars = data.dollars;
      const unit = stages === 1 ? stage_label : `${stage_label}s`;
      const summary =
        data.summary ??
        `This will rerun ${stages} ${unit}, add about ${eta_minutes} minutes, and cost about $${dollars.toFixed(2)}.`;
      return {
        stages,
        stage_label,
        eta_minutes,
        dollars,
        summary,
        note: data.note,
      };
    }
    throw new Error("malformed estimate response");
  } catch {
    return estimateDirectiveLocal(ctx);
  }
}
