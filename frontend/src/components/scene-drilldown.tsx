"use client";

/**
 * DESIGN-05 (#257) — Scene drilldown rail.
 *
 * A shadcn `Sheet` that opens on the right whenever a scene (or slot on
 * the OTIO timeline) is selected via the shared selection store. It
 * replaces the six legacy debug panels as the primary drilldown surface
 * for "everything about this scene" — the engineering panels still live
 * under the Advanced / For developers disclosure in `page.tsx`, but the
 * Sheet is now the default way to inspect a scene's narration, visual
 * prompt, QA results, and reasoning trace.
 *
 * Tabs:
 *
 *   1. Narration — narration text for the scene plus voice takes, each
 *      playable via a native `<audio>` element.
 *   2. Visual    — the visual prompt plus clip takes, each playable via
 *      a native `<video>` element.
 *   3. QA        — plain-English translations of the internal reviewer
 *      labels that ship in ``SlotQaResult.status`` and ``.source``.
 *   4. Why       — reasoning trace (digest preview), collapsed by
 *      default so it stays an intentional "look under the hood" rather
 *      than the primary surface.
 *
 * A single "Redo this scene" button at the bottom POSTs to the existing
 * `/api/directive` endpoint with ``scope="scene"`` so the reviewer can
 * trigger a scene-scoped re-manifestation without typing anything.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { CostPreviewDialog } from "@/components/cost-preview-dialog";
import {
  fetchDirectiveEstimate,
  type CostEstimate,
} from "@/lib/cost-estimate";
import { clearSelection, useSelection } from "@/lib/stores/selection";
import { useOtioStream } from "@/lib/otio-stream";
import type {
  OtioSlot,
  SlotCritique,
  SlotFullView,
  SlotQaResult,
  SlotTake,
} from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

type TabId = "narration" | "visual" | "qa" | "why";

/**
 * Translate an internal QA verdict label (``pass`` / ``warn`` /
 * ``escalate`` / ``fail``) into plain-English copy for reviewers who
 * never learned the reviewer taxonomy. Copy is intentionally positive —
 * only real problems use warning language.
 */
function qaStatusToPlainEnglish(status: string | undefined | null): {
  label: string;
  tone: "ok" | "warn" | "fail";
} {
  const normalised = String(status ?? "").toLowerCase();
  if (normalised === "pass" || normalised === "accepted") {
    return { label: "Looks good", tone: "ok" };
  }
  if (normalised === "warn" || normalised === "warning") {
    return { label: "Worth a look", tone: "warn" };
  }
  if (normalised === "escalate" || normalised === "escalated") {
    return { label: "Needs your attention", tone: "warn" };
  }
  if (normalised === "fail" || normalised === "failed" || normalised === "rejected") {
    return { label: "Needs a redo", tone: "fail" };
  }
  if (!normalised) return { label: "Pending", tone: "warn" };
  return { label: normalised, tone: "warn" };
}

/** Map internal reviewer source ids to a friendly label. */
function qaSourceToPlainEnglish(source: string | undefined | null): string {
  const normalised = String(source ?? "").toLowerCase();
  const map: Record<string, string> = {
    loudness_qa: "Loudness",
    motion_qa: "Motion",
    anti_cheat: "Anti-cheat",
    structural: "Structure",
    semantic: "Meaning",
    bearnaise: "Bearnaise pass",
    duration: "Duration",
    lufs: "Loudness",
  };
  if (map[normalised]) return map[normalised];
  if (!normalised) return "Reviewer";
  return normalised.replace(/_/g, " ");
}

/** Pick narration + visual slots for the scene containing `slotId`. */
function siblingsForScene(
  timeline: ReturnType<typeof useOtioStream>["timeline"],
  slotId: string,
): {
  scene: OtioSlot | null;
  narrationSlot: OtioSlot | null;
  visualSlot: OtioSlot | null;
} {
  if (!timeline) return { scene: null, narrationSlot: null, visualSlot: null };
  let selected: OtioSlot | null = null;
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      if (slot.slot_id === slotId) {
        selected = slot;
        break;
      }
    }
    if (selected) break;
  }
  if (!selected) return { scene: null, narrationSlot: null, visualSlot: null };
  let narrationSlot: OtioSlot | null = null;
  let visualSlot: OtioSlot | null = null;
  for (const track of timeline.tracks) {
    for (const slot of track.slots) {
      if (slot.scene_num !== selected.scene_num) continue;
      if (slot.track === "A1_Narration" && !narrationSlot) narrationSlot = slot;
      if (slot.track === "V1_Video" && !visualSlot) visualSlot = slot;
    }
  }
  return { scene: selected, narrationSlot, visualSlot };
}

type FetchState = {
  data: SlotFullView | null;
  error: string | null;
  loading: boolean;
};

function useSlotFull(slotId: string | null): FetchState {
  const [state, setState] = useState<FetchState>({
    data: null,
    error: null,
    loading: false,
  });

  useEffect(() => {
    if (!slotId) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let cancelled = false;
    setState({ data: null, error: null, loading: true });
    fetch(`${BACKEND_URL}/api/slots/${encodeURIComponent(slotId)}/full`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<SlotFullView>;
      })
      .then((data) => {
        if (!cancelled) setState({ data, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            data: null,
            error: err instanceof Error ? err.message : String(err),
            loading: false,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [slotId]);

  return state;
}

/** Extract the first string-valued field from a bag of candidate keys. */
function pickString(
  bag: Record<string, unknown> | undefined | null,
  keys: string[],
): string | null {
  if (!bag) return null;
  for (const key of keys) {
    const v = bag[key];
    if (typeof v === "string" && v.trim().length > 0) return v.trim();
  }
  return null;
}

export interface SceneDrilldownProps {
  /** Test-only override — inject a slot id so tests don't depend on the store. */
  slotIdOverride?: string | null;
  /** Test-only override — inject the onClose handler. */
  onCloseOverride?: () => void;
}

export function SceneDrilldown({
  slotIdOverride,
  onCloseOverride,
}: SceneDrilldownProps = {}) {
  const { selectedSlotId } = useSelection();
  const effectiveSlotId =
    slotIdOverride !== undefined ? slotIdOverride : selectedSlotId;
  const { timeline } = useOtioStream();
  const { scene, narrationSlot, visualSlot } = useMemo(
    () => (effectiveSlotId ? siblingsForScene(timeline, effectiveSlotId) : {
      scene: null,
      narrationSlot: null,
      visualSlot: null,
    }),
    [timeline, effectiveSlotId],
  );

  // Prefer the track-specific slot when fetching per-tab content so the
  // takes lists actually align with the tab (narration audio / video
  // clips). If the scene only has one slot rendered so far, fall back
  // to the selected id.
  const narrationSlotId = narrationSlot?.slot_id ?? (
    scene?.track === "A1_Narration" ? scene.slot_id : null
  );
  const visualSlotId = visualSlot?.slot_id ?? (
    scene?.track === "V1_Video" ? scene.slot_id : null
  );

  const narrationFull = useSlotFull(narrationSlotId);
  const visualFull = useSlotFull(visualSlotId);

  const [tab, setTab] = useState<TabId>("narration");
  const [redoSubmitting, setRedoSubmitting] = useState(false);
  const [redoStatus, setRedoStatus] = useState<
    | { kind: "idle" }
    | { kind: "ok"; message: string }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  // Monotonic request id so an in-flight redo from Scene 3 can't clobber
  // the banner after the reviewer has moved on to Scene 5. Every scene
  // change and every new redo click bumps the counter; fetch callbacks
  // bail out when their captured id no longer matches.
  const redoRequestIdRef = useRef(0);

  // DESIGN-07 (#259): Redo opens a cost-preview dialog first -- the
  // actual /api/directive POST only fires after the reviewer confirms.
  const [previewOpen, setPreviewOpen] = useState(false);
  const [estimate, setEstimate] = useState<CostEstimate | null>(null);
  const [estimateLoading, setEstimateLoading] = useState(false);
  const estimateFetchIdRef = useRef(0);

  // Reset the redo banner whenever the drilldown swaps to a different
  // scene — otherwise a stale "Redo queued"/error banner from Scene 3
  // would persist when the reviewer selects Scene 5.
  useEffect(() => {
    redoRequestIdRef.current += 1;
    estimateFetchIdRef.current += 1;
    setRedoStatus({ kind: "idle" });
    setRedoSubmitting(false);
    setPreviewOpen(false);
    setEstimate(null);
    setEstimateLoading(false);
  }, [effectiveSlotId]);

  const handleClose = useCallback(() => {
    if (onCloseOverride) onCloseOverride();
    else clearSelection();
  }, [onCloseOverride]);

  const submitRedo = useCallback(async () => {
    if (!scene || redoSubmitting) return;
    redoRequestIdRef.current += 1;
    const requestId = redoRequestIdRef.current;
    setRedoSubmitting(true);
    setRedoStatus({ kind: "idle" });
    try {
      const body = {
        directive: `Redo Scene ${scene.scene_num}`,
        slot_context: {
          scope: "scene",
          scope_ref: String(scene.scene_num),
          scene_num: scene.scene_num,
        },
        reviewer: "dashboard-user",
      };
      const res = await fetch(`${BACKEND_URL}/api/directive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // The reviewer switched scenes (or clicked redo again) while the
      // request was in flight — drop the response on the floor so we
      // don't overwrite the current scene's banner.
      if (redoRequestIdRef.current !== requestId) return;
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setRedoStatus({
          kind: "error",
          message: text.slice(0, 200) || `Redo failed (${res.status})`,
        });
        return;
      }
      setRedoStatus({
        kind: "ok",
        message: "Redo queued — we'll rebuild this scene.",
      });
    } catch (err) {
      if (redoRequestIdRef.current !== requestId) return;
      setRedoStatus({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      if (redoRequestIdRef.current === requestId) setRedoSubmitting(false);
      setPreviewOpen(false);
    }
  }, [scene, redoSubmitting]);

  const openRedoPreview = useCallback(() => {
    if (!scene || redoSubmitting) return;
    setEstimate(null);
    setEstimateLoading(true);
    setPreviewOpen(true);
    const myId = ++estimateFetchIdRef.current;
    void fetchDirectiveEstimate(
      {
        directive: `Redo Scene ${scene.scene_num}`,
        slot_context: {
          scope: "scene",
          scope_ref: String(scene.scene_num),
          scene_num: scene.scene_num,
        },
      },
      { backendUrl: BACKEND_URL },
    ).then((est) => {
      // Drop stale responses: a cancel-then-reopen (or a scene switch
      // while the fetch is in flight) must not leak numbers into the
      // newer preview.
      if (estimateFetchIdRef.current !== myId) return;
      setEstimate(est);
      setEstimateLoading(false);
    });
  }, [scene, redoSubmitting]);

  const open = Boolean(effectiveSlotId);
  const sceneNumLabel = scene ? `Scene ${scene.scene_num}` : "Scene";
  const sceneLabel = scene?.label?.trim() || sceneNumLabel;

  return (
    <Sheet
      open={open}
      onOpenChange={(next) => {
        if (!next) handleClose();
      }}
    >
      <SheetContent
        side="right"
        className="flex w-[min(640px,100vw)] flex-col gap-0 p-0 sm:max-w-[640px]"
        data-testid="scene-drilldown"
      >
        <SheetHeader className="space-y-1 border-b p-6 pb-4">
          <SheetTitle className="text-base font-semibold" data-testid="scene-drilldown-title">
            {sceneNumLabel}
            {scene?.label && scene.label.trim() && (
              <span className="text-muted-foreground"> · {scene.label}</span>
            )}
          </SheetTitle>
          <SheetDescription className="text-xs">
            Everything about {sceneLabel.toLowerCase()} — narration, visuals,
            quality checks, and reasoning.
          </SheetDescription>
        </SheetHeader>

        <Tabs
          value={tab}
          onValueChange={(v) => setTab(v as TabId)}
          className="flex min-h-0 flex-1 flex-col"
        >
          <div className="border-b px-6 pt-3">
            <TabsList className="w-full justify-start">
              <TabsTrigger value="narration" data-testid="scene-tab-narration">
                Narration
              </TabsTrigger>
              <TabsTrigger value="visual" data-testid="scene-tab-visual">
                Visual
              </TabsTrigger>
              <TabsTrigger value="qa" data-testid="scene-tab-qa">
                QA
              </TabsTrigger>
              <TabsTrigger value="why" data-testid="scene-tab-why">
                Why
              </TabsTrigger>
            </TabsList>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-6">
            <TabsContent value="narration" className="mt-0 space-y-4">
              <NarrationTab
                slot={narrationSlot ?? (scene?.track === "A1_Narration" ? scene : null)}
                state={narrationFull}
              />
            </TabsContent>
            <TabsContent value="visual" className="mt-0 space-y-4">
              <VisualTab
                slot={visualSlot ?? (scene?.track === "V1_Video" ? scene : null)}
                state={visualFull}
              />
            </TabsContent>
            <TabsContent value="qa" className="mt-0 space-y-4">
              <QaTab narration={narrationFull.data} visual={visualFull.data} />
            </TabsContent>
            <TabsContent value="why" className="mt-0">
              <WhyTab narration={narrationFull.data} visual={visualFull.data} />
            </TabsContent>
          </div>
        </Tabs>

        <footer className="flex flex-col gap-2 border-t p-6">
          {redoStatus.kind === "ok" && (
            <div
              className="rounded border border-emerald-500/50 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-900 dark:text-emerald-100"
              data-testid="scene-redo-ok"
              role="status"
            >
              {redoStatus.message}
            </div>
          )}
          {redoStatus.kind === "error" && (
            <div
              className="rounded border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-destructive"
              data-testid="scene-redo-error"
              role="alert"
            >
              {redoStatus.message}
            </div>
          )}
          <Button
            type="button"
            onClick={openRedoPreview}
            disabled={!scene || redoSubmitting}
            data-testid="scene-redo-button"
          >
            {redoSubmitting ? "Queuing…" : "Redo this scene"}
          </Button>
        </footer>
        <CostPreviewDialog
          open={previewOpen}
          onOpenChange={(next) => {
            // Cancelling invalidates the in-flight estimate so its
            // late-arriving response can't leak into a future preview.
            if (!next) estimateFetchIdRef.current += 1;
            setPreviewOpen(next);
          }}
          title={scene ? `Redo Scene ${scene.scene_num}` : "Redo this scene"}
          description={
            scene
              ? `Rebuild Scene ${scene.scene_num}${
                  scene.label?.trim() ? ` (${scene.label.trim()})` : ""
                } from the brief.`
              : undefined
          }
          estimate={estimate}
          loading={estimateLoading}
          onConfirm={submitRedo}
          confirmLabel="Redo this scene"
          cancelLabel="Cancel"
          submitting={redoSubmitting}
          dataTestId="scene-redo-cost-preview"
        />
      </SheetContent>
    </Sheet>
  );
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
      {children}
    </h3>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded border border-dashed border-border bg-muted/30 px-3 py-4 text-xs text-muted-foreground">
      {children}
    </p>
  );
}

function NarrationTab({
  slot,
  state,
}: {
  slot: OtioSlot | null;
  state: FetchState;
}) {
  const narrationText =
    pickString(slot?.metadata ?? null, ["text", "narration", "narration_text", "transcript"]) ??
    slot?.label?.trim() ??
    null;

  return (
    <div className="space-y-4" data-testid="scene-drilldown-narration">
      <section className="space-y-1">
        <SectionHeading>Narration text</SectionHeading>
        {narrationText ? (
          <p className="whitespace-pre-wrap rounded bg-muted/30 px-3 py-2 text-sm">
            {narrationText}
          </p>
        ) : (
          <Empty>No narration copy yet for this scene.</Empty>
        )}
      </section>
      <section className="space-y-2">
        <SectionHeading>Voice takes</SectionHeading>
        <TakesList takes={state.data?.takes ?? []} kind="audio" loading={state.loading} />
      </section>
    </div>
  );
}

function VisualTab({
  slot,
  state,
}: {
  slot: OtioSlot | null;
  state: FetchState;
}) {
  const prompt =
    pickString(slot?.metadata ?? null, [
      "prompt",
      "visual_prompt",
      "visual_concept",
      "description",
    ]) ?? null;

  return (
    <div className="space-y-4" data-testid="scene-drilldown-visual">
      <section className="space-y-1">
        <SectionHeading>Visual prompt</SectionHeading>
        {prompt ? (
          <p className="whitespace-pre-wrap rounded bg-muted/30 px-3 py-2 text-sm">
            {prompt}
          </p>
        ) : (
          <Empty>No visual prompt recorded for this scene yet.</Empty>
        )}
      </section>
      <section className="space-y-2">
        <SectionHeading>Clip takes</SectionHeading>
        <TakesList takes={state.data?.takes ?? []} kind="video" loading={state.loading} />
      </section>
    </div>
  );
}

function TakesList({
  takes,
  kind,
  loading,
}: {
  takes: SlotTake[];
  kind: "audio" | "video";
  loading: boolean;
}) {
  if (loading && takes.length === 0) {
    return <Empty>Loading takes…</Empty>;
  }
  if (takes.length === 0) {
    return <Empty>No takes yet.</Empty>;
  }
  return (
    <ul className="space-y-2" data-testid={`scene-takes-${kind}`}>
      {takes.map((take) => {
        const src = take.b2_url || take.preview_url;
        const verdict = qaStatusToPlainEnglish(take.outcome);
        return (
          <li
            key={`${take.revision}-${take.artifact_id}`}
            className="flex flex-col gap-2 rounded border border-border bg-muted/20 p-3"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium">Take {take.revision}</span>
              <Badge variant={verdict.tone === "ok" ? "secondary" : "outline"}>
                {verdict.label}
              </Badge>
            </div>
            {src ? (
              kind === "audio" ? (
                <audio
                  controls
                  preload="none"
                  src={src}
                  data-testid="scene-take-audio"
                  className="w-full"
                />
              ) : (
                <video
                  controls
                  preload="none"
                  src={src}
                  data-testid="scene-take-video"
                  className="w-full rounded"
                />
              )
            ) : (
              <span className="text-xs text-muted-foreground">
                Preview not available yet.
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function QaTab({
  narration,
  visual,
}: {
  narration: SlotFullView | null;
  visual: SlotFullView | null;
}) {
  const rows = useMemo(() => {
    const acc: Array<SlotQaResult & { _origin: string }> = [];
    for (const r of narration?.qa_results ?? []) {
      acc.push({ ...r, _origin: "narration" });
    }
    for (const r of visual?.qa_results ?? []) {
      acc.push({ ...r, _origin: "visual" });
    }
    return acc;
  }, [narration, visual]);

  const critiques = useMemo(() => {
    const acc: Array<SlotCritique & { _origin: string }> = [];
    for (const c of narration?.critiques ?? []) acc.push({ ...c, _origin: "narration" });
    for (const c of visual?.critiques ?? []) acc.push({ ...c, _origin: "visual" });
    return acc;
  }, [narration, visual]);

  return (
    <div className="space-y-4" data-testid="scene-drilldown-qa">
      <section className="space-y-2">
        <SectionHeading>Quality checks</SectionHeading>
        {rows.length === 0 ? (
          <Empty>No quality checks have run for this scene yet.</Empty>
        ) : (
          <ul className="space-y-2">
            {rows.map((r, i) => {
              const verdict = qaStatusToPlainEnglish(r.status);
              return (
                <li
                  key={`${r._origin}-${r.source ?? i}-${i}`}
                  className="flex items-start justify-between gap-3 rounded border border-border bg-muted/20 p-3"
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">
                        {qaSourceToPlainEnglish(r.source)}
                      </span>
                      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {r._origin}
                      </span>
                    </div>
                    {r.summary && (
                      <p className="text-xs text-muted-foreground">
                        {r.summary}
                      </p>
                    )}
                  </div>
                  <Badge variant={verdict.tone === "ok" ? "secondary" : "outline"}>
                    {verdict.label}
                  </Badge>
                </li>
              );
            })}
          </ul>
        )}
      </section>
      {critiques.length > 0 && (
        <section className="space-y-2">
          <SectionHeading>Reviewer notes</SectionHeading>
          <ul className="space-y-2">
            {critiques.map((c, i) => (
              <li
                key={`${c._origin}-${c.source}-${i}`}
                className="rounded border border-border bg-muted/20 p-3 text-xs"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{qaSourceToPlainEnglish(c.source)}</span>
                  <HoverCard>
                    <HoverCardTrigger asChild>
                      <span className="cursor-help text-muted-foreground">
                        {c.rating.toLowerCase()}
                      </span>
                    </HoverCardTrigger>
                    <HoverCardContent className="w-60 text-xs">
                      Internal reviewer rating. Higher ratings mean fewer
                      issues flagged by the automated check.
                    </HoverCardContent>
                  </HoverCard>
                </div>
                {c.summary && <p className="mt-1 text-muted-foreground">{c.summary}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function WhyTab({
  narration,
  visual,
}: {
  narration: SlotFullView | null;
  visual: SlotFullView | null;
}) {
  const traces = useMemo(() => {
    const acc: Array<Record<string, unknown> & { _origin: string }> = [];
    for (const d of narration?.reasoning_trace_preview ?? []) {
      acc.push({ ...d, _origin: "narration" });
    }
    for (const d of visual?.reasoning_trace_preview ?? []) {
      acc.push({ ...d, _origin: "visual" });
    }
    return acc;
  }, [narration, visual]);

  return (
    <Collapsible data-testid="scene-drilldown-why">
      <CollapsibleTrigger
        className="flex w-full items-center justify-between rounded border border-border bg-muted/20 px-3 py-2 text-sm"
        data-testid="scene-why-toggle"
      >
        <span>Show reasoning trace</span>
        <span className="text-xs text-muted-foreground">
          {traces.length} {traces.length === 1 ? "entry" : "entries"}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 space-y-2">
        {traces.length === 0 ? (
          <Empty>
            No reasoning recorded yet. This shows up once agents start planning
            the scene.
          </Empty>
        ) : (
          <ul className="space-y-2">
            {traces.map((d, i) => (
              <li
                key={i}
                className="rounded border border-border bg-muted/20 p-3 text-xs"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">
                    {String(d.agent ?? "Agent")}
                  </span>
                  <span className="text-muted-foreground">
                    {String(d._origin)}
                  </span>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
                  {String(d.summary ?? d.content ?? "")}
                </p>
              </li>
            ))}
          </ul>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
