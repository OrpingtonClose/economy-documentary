"use client";

/**
 * UI-04 — Full slot drilldown panel (issues #189 / #203 / #204).
 *
 * Collapsible right-rail panel that renders, for the selected slot:
 *
 *   1. Header             — id, track, scene/phrase, duration, state badge
 *   2. Current take       — preview / thumbnail / waveform + verdict
 *   3. Take history       — revisions with outcome + ledger-stamped derivation
 *   4. Critiques          — per-critic rationale
 *   5. QA measurements    — numeric / status verdicts (LUFS, motion, ...)
 *   6. Artifacts          — flat list of media references
 *   7. Ledger             — records applicable to the slot (#202 helper)
 *   8. Reasoning trace    — digest preview + advanced-mode virtualised raw
 *                           feed (#204)
 *
 * The panel fetches ``/api/slots/{slot_id}/full`` on open and re-fetches
 * only when the slot changes. No new SSE channels. The top-right toggle
 * flips between ``friendly`` (header + current take expanded) and
 * ``advanced`` (everything expanded + raw reasoning enabled).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ReasoningTrace,
  SlotFullView,
  SlotTake,
} from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

type PanelMode = "friendly" | "advanced";

type SectionId =
  | "header"
  | "current_take"
  | "takes"
  | "critiques"
  | "qa"
  | "artifacts"
  | "ledger"
  | "reasoning";

const FRIENDLY_EXPANDED: Set<SectionId> = new Set([
  "header",
  "current_take",
]);

const ADVANCED_EXPANDED: Set<SectionId> = new Set([
  "header",
  "current_take",
  "takes",
  "critiques",
  "qa",
  "artifacts",
  "ledger",
  "reasoning",
]);

export function SlotDetailPanel({
  slotId,
  onClose,
}: {
  slotId: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<SlotFullView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<PanelMode>("friendly");
  const [expanded, setExpanded] = useState<Set<SectionId>>(FRIENDLY_EXPANDED);

  // Re-fetch whenever the selected slot changes. No SSE — this matches
  // the "panel fetches on demand" contract in #189.
  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    fetch(`${BACKEND_URL}/api/slots/${encodeURIComponent(slotId)}/full`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: SlotFullView) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [slotId]);

  const setPanelMode = useCallback((next: PanelMode) => {
    setMode(next);
    setExpanded(
      next === "advanced"
        ? new Set(ADVANCED_EXPANDED)
        : new Set(FRIENDLY_EXPANDED),
    );
  }, []);

  const toggleSection = useCallback((id: SectionId) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const currentTake = useMemo<SlotTake | null>(() => {
    if (!detail || detail.takes.length === 0) return null;
    const accepted = [...detail.takes].reverse().find((t) => t.outcome === "accepted");
    return accepted ?? detail.takes[detail.takes.length - 1];
  }, [detail]);

  return (
    <aside
      className="fixed right-0 top-0 z-30 flex h-full w-[min(560px,100vw)] flex-col border-l border-pipeline-blue/70 bg-pipeline-card shadow-2xl"
      aria-label={`Slot detail: ${slotId}`}
    >
      <header className="flex items-center justify-between gap-2 border-b border-pipeline-blue/60 px-4 py-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-widest text-pipeline-muted">
            slot drilldown
          </div>
          <div className="truncate font-mono text-sm text-pipeline-accent">
            {slotId}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div
            role="radiogroup"
            aria-label="Panel detail level"
            className="flex rounded border border-pipeline-blue/60 text-[10px] uppercase"
          >
            <ModeButton
              active={mode === "friendly"}
              onClick={() => setPanelMode("friendly")}
              label="friendly"
            />
            <ModeButton
              active={mode === "advanced"}
              onClick={() => setPanelMode("advanced")}
              label="advanced"
            />
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded bg-pipeline-bg px-3 py-1 text-xs text-pipeline-muted hover:bg-pipeline-blue/40"
          >
            close
          </button>
        </div>
      </header>

      <div className="flex-1 space-y-3 overflow-auto px-4 py-3 text-xs">
        {error && (
          <div className="rounded bg-red-900/50 px-2 py-1 text-red-100">
            failed to load: {error}
          </div>
        )}
        {!detail && !error && (
          <div className="text-pipeline-muted">loading slot detail…</div>
        )}
        {detail && (
          <>
            <Section
              id="header"
              title="Header"
              expanded={expanded.has("header")}
              onToggle={toggleSection}
            >
              <HeaderSection detail={detail} />
            </Section>

            <Section
              id="current_take"
              title="Current take"
              expanded={expanded.has("current_take")}
              onToggle={toggleSection}
            >
              <CurrentTakeSection
                detail={detail}
                currentTake={currentTake}
              />
            </Section>

            <Section
              id="takes"
              title={`Take history (${detail.takes.length})`}
              expanded={expanded.has("takes")}
              onToggle={toggleSection}
            >
              {detail.takes.length === 0 ? (
                <Empty>no takes yet</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.takes.map((take) => (
                    <TakeRow key={`${take.revision}-${take.artifact_id}`} take={take} />
                  ))}
                </ul>
              )}
            </Section>

            <Section
              id="critiques"
              title={`Critiques (${detail.critiques.length})`}
              expanded={expanded.has("critiques")}
              onToggle={toggleSection}
            >
              {detail.critiques.length === 0 ? (
                <Empty>no critic records</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.critiques.map((c, i) => (
                    <li
                      key={`${c.source}-${i}`}
                      className="rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]"
                    >
                      <div className="flex justify-between font-semibold">
                        <span>{c.source}</span>
                        <span className="text-pipeline-muted">{c.rating}</span>
                      </div>
                      {c.summary && <div>{c.summary}</div>}
                      {c.issues && c.issues.length > 0 && (
                        <ul className="mt-1 list-disc pl-4 text-[10px] text-pipeline-muted">
                          {c.issues.slice(0, 4).map((iss, j) => (
                            <li key={j}>{iss}</li>
                          ))}
                        </ul>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section
              id="qa"
              title={`QA measurements (${detail.qa_results.length})`}
              expanded={expanded.has("qa")}
              onToggle={toggleSection}
            >
              {detail.qa_results.length === 0 ? (
                <Empty>no QA verdicts</Empty>
              ) : (
                <QaTable rows={detail.qa_results} />
              )}
            </Section>

            <Section
              id="artifacts"
              title={`Artifacts (${detail.artifacts.length})`}
              expanded={expanded.has("artifacts")}
              onToggle={toggleSection}
            >
              {detail.artifacts.length === 0 ? (
                <Empty>no artifacts yet</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.artifacts.map((a, i) => (
                    <li
                      key={`${a.kind}-${i}`}
                      className="flex items-center justify-between gap-2 rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]"
                    >
                      <span className="truncate">
                        <span className="text-pipeline-muted">[{a.kind}]</span>{" "}
                        {a.label}
                      </span>
                      <a
                        href={a.url}
                        target="_blank"
                        rel="noreferrer"
                        className="shrink-0 text-pipeline-accent underline"
                      >
                        open
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section
              id="ledger"
              title={`Ledger (${detail.ledger_records.length})`}
              expanded={expanded.has("ledger")}
              onToggle={toggleSection}
            >
              {detail.ledger_records.length === 0 ? (
                <Empty>no ledger records in scope</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.ledger_records.map((l, i) => (
                    <LedgerRow key={i} record={l} />
                  ))}
                </ul>
              )}
            </Section>

            <Section
              id="reasoning"
              title="Reasoning trace"
              expanded={expanded.has("reasoning")}
              onToggle={toggleSection}
            >
              <ReasoningSection
                detail={detail}
                slotId={slotId}
                advanced={mode === "advanced"}
              />
            </Section>
          </>
        )}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function ModeButton({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      className={
        "px-2 py-1 tracking-wider transition-colors " +
        (active
          ? "bg-pipeline-accent/20 text-pipeline-accent"
          : "text-pipeline-muted hover:bg-pipeline-blue/30")
      }
    >
      {label}
    </button>
  );
}

function Section({
  id,
  title,
  expanded,
  onToggle,
  children,
}: {
  id: SectionId;
  title: string;
  expanded: boolean;
  onToggle: (id: SectionId) => void;
  children: React.ReactNode;
}) {
  const contentId = `slot-panel-section-${id}`;
  return (
    <section className="border-t border-pipeline-blue/30 first:border-t-0">
      <h3 className="m-0 flex">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => onToggle(id)}
          className="flex w-full items-center justify-between py-2 text-[10px] font-semibold uppercase tracking-widest text-pipeline-muted hover:text-pipeline-accent"
        >
          <span>{title}</span>
          <span aria-hidden>{expanded ? "▾" : "▸"}</span>
        </button>
      </h3>
      {expanded && <div id={contentId}>{children}</div>}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded border border-dashed border-pipeline-blue/40 px-2 py-1 text-[11px] text-pipeline-muted">
      {children}
    </div>
  );
}

function HeaderSection({ detail }: { detail: SlotFullView }) {
  const { slot } = detail;
  return (
    <dl className="grid grid-cols-2 gap-2 rounded bg-pipeline-bg/60 p-2 text-[11px]">
      <div>
        <dt className="text-pipeline-muted">track</dt>
        <dd className="font-mono">{slot.track}</dd>
      </div>
      <div>
        <dt className="text-pipeline-muted">scene / phrase</dt>
        <dd className="font-mono">
          {slot.scene_num} / {slot.phrase_idx}
        </dd>
      </div>
      <div>
        <dt className="text-pipeline-muted">duration</dt>
        <dd className="font-mono">{slot.duration_sec.toFixed(2)}s</dd>
      </div>
      <div>
        <dt className="text-pipeline-muted">status</dt>
        <dd>
          <StatusBadge status={slot.status} />
        </dd>
      </div>
      {slot.label && (
        <div className="col-span-2">
          <dt className="text-pipeline-muted">label</dt>
          <dd>{slot.label}</dd>
        </div>
      )}
      {slot.rung && (
        <div className="col-span-2">
          <dt className="text-pipeline-muted">rung</dt>
          <dd className="font-mono">{slot.rung}</dd>
        </div>
      )}
      {slot.failure_reason && (
        <div className="col-span-2">
          <dt className="text-pipeline-muted">failure reason</dt>
          <dd className="text-red-200">{slot.failure_reason}</dd>
        </div>
      )}
    </dl>
  );
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "delivered"
      ? "bg-emerald-900/60 text-emerald-200"
      : status === "failed"
        ? "bg-red-900/60 text-red-200"
        : status === "in_progress"
          ? "bg-amber-900/60 text-amber-200"
          : "bg-pipeline-blue/40 text-pipeline-muted";
  return (
    <span
      className={
        "inline-block rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider " +
        color
      }
    >
      {status}
    </span>
  );
}

function CurrentTakeSection({
  detail,
  currentTake,
}: {
  detail: SlotFullView;
  currentTake: SlotTake | null;
}) {
  const { slot } = detail;
  const mediaUrl =
    currentTake?.b2_url ||
    currentTake?.preview_url ||
    slot.preview_url ||
    "";
  return (
    <div className="space-y-2">
      {mediaUrl ? (
        slot.track === "V1_Video" ? (
          <video
            src={mediaUrl}
            controls
            preload="metadata"
            className="aspect-video w-full rounded bg-black"
          />
        ) : (
          <audio src={mediaUrl} controls preload="metadata" className="w-full" />
        )
      ) : slot.thumbnail_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={slot.thumbnail_url}
          alt={`Slot ${slot.slot_id} thumbnail`}
          className="aspect-video w-full rounded bg-black object-cover"
        />
      ) : (
        <Empty>no media yet</Empty>
      )}
      {currentTake ? (
        <div className="flex items-center justify-between rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]">
          <span className="font-mono">rev {currentTake.revision}</span>
          <span className="text-pipeline-muted">{currentTake.outcome}</span>
        </div>
      ) : (
        <Empty>no takes yet</Empty>
      )}
    </div>
  );
}

function TakeRow({ take }: { take: SlotTake }) {
  const tone =
    take.outcome === "accepted"
      ? "text-emerald-300"
      : take.outcome === "rejected"
        ? "text-red-300"
        : "text-pipeline-muted";
  return (
    <li className="rounded bg-pipeline-bg/60 px-2 py-1 font-mono text-[10px]">
      <div className="flex justify-between">
        <span>
          rev {take.revision} · {take.artifact_id || "(no id)"}
        </span>
        <span className={tone}>{take.outcome}</span>
      </div>
      {take.ledger_revision_at_derivation != null && (
        <div className="text-[10px] text-amber-300">
          ledger rev {take.ledger_revision_at_derivation.revision}
        </div>
      )}
      {take.b2_url && (
        <a
          href={take.b2_url}
          target="_blank"
          rel="noreferrer"
          className="text-[10px] text-pipeline-accent underline"
        >
          open take
        </a>
      )}
    </li>
  );
}

function QaTable({ rows }: { rows: SlotFullView["qa_results"] }) {
  return (
    <table className="w-full table-fixed border-separate border-spacing-y-1 text-[11px]">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-widest text-pipeline-muted">
          <th className="w-32 font-normal">source</th>
          <th className="w-16 font-normal">status</th>
          <th className="font-normal">summary</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${r.source ?? i}-${i}`} className="bg-pipeline-bg/60">
            <td className="truncate rounded-l px-2 py-1 font-mono">
              {String(r.source ?? r.category ?? "-")}
            </td>
            <td className="px-2 py-1 font-mono">
              {String(r.status ?? r.verdict ?? "-")}
            </td>
            <td className="rounded-r px-2 py-1">
              {String(r.summary ?? r.reason ?? "")}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LedgerRow({ record }: { record: Record<string, unknown> }) {
  const superseded = Boolean(
    (record.metadata as Record<string, unknown> | undefined)?.superseded,
  );
  return (
    <li
      className={
        "rounded bg-pipeline-bg/60 px-2 py-1 text-[11px] " +
        (superseded ? "opacity-50" : "")
      }
    >
      <div className="flex justify-between">
        <span className="font-mono text-[10px]">
          {String(record.scope ?? "-")}
          {record.scope_ref ? `:${String(record.scope_ref)}` : ""}
        </span>
        <span className="text-pipeline-muted">
          rev {String(record.revision ?? "-")}
          {superseded && " · superseded"}
        </span>
      </div>
      <div>
        {String(
          record.content ??
            record.statement ??
            record.text ??
            "",
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Reasoning section — digest preview + virtualised raw feed (advanced only)
// ---------------------------------------------------------------------------

function ReasoningSection({
  detail,
  slotId,
  advanced,
}: {
  detail: SlotFullView;
  slotId: string;
  advanced: boolean;
}) {
  const preview = detail.reasoning_trace_preview;
  return (
    <div className="space-y-2">
      <div>
        <div className="mb-1 text-[10px] uppercase tracking-widest text-pipeline-muted">
          digest preview (last {preview.length})
        </div>
        {preview.length === 0 ? (
          <Empty>no digests reference this slot</Empty>
        ) : (
          <ul className="space-y-1">
            {preview.map((d, i) => (
              <li
                key={i}
                className="rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]"
              >
                <div className="flex justify-between font-semibold">
                  <span>{String(d.agent ?? "agent")}</span>
                  <span className="text-pipeline-muted">
                    {String(d.importance ?? "")}
                  </span>
                </div>
                <div>{String(d.summary ?? "")}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
      {advanced && <RawReasoningFeed slotId={slotId} />}
    </div>
  );
}

/** Virtualised raw-trace list for the advanced mode.
 *
 * Uses a simple window-based virtualizer (not a library dependency) so
 * we can handle 1000+ entries smoothly: only the visible rows render.
 *
 * Rows are strictly fixed-height (``ROW_HEIGHT``) so virtualisation math
 * stays correct. Selecting a row surfaces its full payload in a
 * dedicated detail pane below the list rather than expanding inline —
 * this avoids overlapping rows and keeps the scrollbar accurate.
 */
const RAW_ROW_HEIGHT = 52;
const RAW_LIST_HEIGHT = 320;

function RawReasoningFeed({ slotId }: { slotId: string }) {
  const [traces, setTraces] = useState<ReasoningTrace[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTraces(null);
    setError(null);
    setSelectedId(null);
    fetch(
      `${BACKEND_URL}/api/reasoning/raw?slot_id=${encodeURIComponent(slotId)}&limit=1000`,
    )
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: { traces: ReasoningTrace[] }) => {
        if (!cancelled) setTraces(data.traces ?? []);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [slotId]);

  const selected = useMemo(() => {
    if (traces == null || selectedId == null) return null;
    return traces.find((t) => t.id === selectedId) ?? null;
  }, [traces, selectedId]);

  if (error) {
    return (
      <div className="rounded bg-red-900/50 px-2 py-1 text-red-100">
        raw trace load failed: {error}
      </div>
    );
  }
  if (traces == null) {
    return <div className="text-pipeline-muted">loading raw traces…</div>;
  }

  return (
    <div className="space-y-2">
      <div className="mb-1 text-[10px] uppercase tracking-widest text-pipeline-muted">
        raw trace ({traces.length})
      </div>
      {traces.length === 0 ? (
        <Empty>no raw entries for this slot</Empty>
      ) : (
        <VirtualList
          items={traces}
          itemHeight={RAW_ROW_HEIGHT}
          height={RAW_LIST_HEIGHT}
          renderItem={(trace) => (
            <RawTraceRow
              key={trace.id}
              trace={trace}
              selected={trace.id === selectedId}
              onSelect={() =>
                setSelectedId((prev) => (prev === trace.id ? null : trace.id))
              }
            />
          )}
        />
      )}
      {selected && <RawTraceDetail trace={selected} />}
    </div>
  );
}

function RawTraceRow({
  trace,
  selected,
  onSelect,
}: {
  trace: ReasoningTrace;
  selected: boolean;
  onSelect: () => void;
}) {
  const ts = new Date(trace.timestamp * 1000).toISOString().slice(11, 23);
  const digest =
    (trace.content || "").slice(0, 160).replace(/\s+/g, " ") || trace.event_type;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={
        "flex w-full flex-col justify-center gap-0.5 overflow-hidden rounded px-2 py-1 text-left text-[11px] " +
        (selected
          ? "bg-pipeline-accent/20 text-pipeline-accent"
          : "bg-pipeline-bg/60 hover:bg-pipeline-blue/30")
      }
      style={{ height: RAW_ROW_HEIGHT - 4, marginBottom: 4 }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 gap-2">
          <span className="font-mono text-[10px] text-pipeline-muted">{ts}</span>
          <span className="truncate font-semibold">
            {trace.agent_name || "-"}
          </span>
        </span>
        <span className="shrink-0 text-[10px] text-pipeline-muted">
          {trace.event_type}
        </span>
      </div>
      <div className="truncate text-[11px] text-pipeline-muted">{digest}</div>
    </button>
  );
}

function RawTraceDetail({ trace }: { trace: ReasoningTrace }) {
  const ts = new Date(trace.timestamp * 1000).toISOString();
  const hasMeta = Object.keys(trace.metadata || {}).length > 0;
  return (
    <div className="rounded border border-pipeline-blue/40 bg-pipeline-bg/60 p-2 text-[11px]">
      <div className="mb-1 flex justify-between gap-2 text-[10px] uppercase tracking-widest text-pipeline-muted">
        <span>
          {trace.event_type} · {trace.agent_name || "-"}
        </span>
        <span className="font-mono">{ts}</span>
      </div>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-black/30 p-2 font-mono text-[10px]">
        {trace.content}
        {hasMeta &&
          `\n\nmetadata:\n${JSON.stringify(trace.metadata, null, 2)}`}
      </pre>
    </div>
  );
}

/** Minimal virtualised list (no external deps).
 *
 * Renders only the rows intersecting the visible window plus a small
 * overscan buffer. Row height is strictly fixed: each rendered row is
 * wrapped in a ``height: itemHeight`` box with ``overflow: hidden`` so
 * the positioning math stays correct regardless of what the caller
 * renders inside.
 */
function VirtualList<T>({
  items,
  itemHeight,
  height,
  renderItem,
}: {
  items: T[];
  itemHeight: number;
  height: number;
  renderItem: (item: T, index: number) => React.ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);

  const overscan = 4;
  const total = items.length * itemHeight;
  const startIdx = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan);
  const endIdx = Math.min(
    items.length,
    Math.ceil((scrollTop + height) / itemHeight) + overscan,
  );
  const visible = items.slice(startIdx, endIdx);
  const offset = startIdx * itemHeight;

  return (
    <div
      ref={containerRef}
      onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}
      style={{ height, overflow: "auto", position: "relative" }}
      className="rounded border border-pipeline-blue/40"
    >
      <div style={{ height: total, position: "relative" }}>
        <div style={{ transform: `translateY(${offset}px)` }}>
          {visible.map((item, i) => (
            <div
              key={startIdx + i}
              style={{ height: itemHeight, overflow: "hidden" }}
            >
              {renderItem(item, startIdx + i)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
