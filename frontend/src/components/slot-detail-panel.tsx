"use client";

/**
 * ARCH-H3 — Slot detail side panel.
 *
 * Clicking any slot on the centrepiece OTIO opens this read-only side
 * panel that aggregates, for the selected slot:
 *
 *   - Artifact history (B1 ledger-revision stamp on each revision).
 *   - QA verdicts (E3 stylistic records + any coherence evaluator output).
 *   - Reasoning digests (H5 digest writer).
 *   - In-scope preference-ledger records (scope filter matches
 *     scene/block/clip).
 *   - Current rung on the content / infra ladder.
 *   - The latest preview assembly that includes the slot (workstream G).
 *
 * No mutation — user intent flows through the Preference Interpreter (H4),
 * not from this panel.
 */

import { useEffect, useState } from "react";
import type { SlotDetailView } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export function SlotDetailPanel({
  slotId,
  onClose,
}: {
  slotId: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<SlotDetailView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    fetch(`${BACKEND_URL}/agui/slots/${encodeURIComponent(slotId)}/detail`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: SlotDetailView) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [slotId]);

  return (
    <aside className="fixed right-0 top-0 z-30 flex h-full w-[min(520px,100vw)] flex-col border-l border-pipeline-blue/70 bg-pipeline-card shadow-2xl">
      <header className="flex items-center justify-between border-b border-pipeline-blue/60 px-4 py-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-pipeline-muted">
            slot detail
          </div>
          <div className="font-mono text-sm text-pipeline-accent">
            {slotId}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded bg-pipeline-bg px-3 py-1 text-xs text-pipeline-muted hover:bg-pipeline-blue/40"
        >
          close
        </button>
      </header>

      <div className="flex-1 space-y-4 overflow-auto px-4 py-3 text-xs">
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
            <Section title="Artifact history (B1 ledger-stamped)">
              {detail.artifact_history.length === 0 ? (
                <Empty>no artifacts yet</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.artifact_history.map((row, idx) => (
                    <li
                      key={String(row.id ?? idx)}
                      className="rounded bg-pipeline-bg/60 px-2 py-1 font-mono text-[10px]"
                    >
                      <div className="flex justify-between">
                        <span>{String(row.id ?? "-")}</span>
                        <span className="text-pipeline-muted">
                          {String(row.status ?? "-")}
                        </span>
                      </div>
                      {row.ledger_revision_at_derivation != null && (
                        <div className="text-[10px] text-amber-300">
                          ledger rev{" "}
                          {(row.ledger_revision_at_derivation as {
                            revision: number;
                          }).revision}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section title="QA verdicts (E3 + coherence)">
              {detail.qa_verdicts.length === 0 ? (
                <Empty>no verdicts recorded</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.qa_verdicts.map((v, i) => (
                    <li
                      key={i}
                      className="rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]"
                    >
                      <div className="font-semibold">
                        {String(v.verdict ?? v.category ?? v.id ?? "verdict")}
                      </div>
                      <div className="text-pipeline-muted">
                        {String(v.summary ?? v.reason ?? "")}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section title="Reasoning digests (H5)">
              {detail.reasoning_digests.length === 0 ? (
                <Empty>no digests reference this slot</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.reasoning_digests.map((d, i) => (
                    <li
                      key={i}
                      className="rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]"
                    >
                      <div className="font-semibold">
                        {String(d.agent ?? "agent")} ·{" "}
                        {String(d.importance ?? "")}
                      </div>
                      <div>{String(d.summary ?? "")}</div>
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section title="In-scope ledger records (A4)">
              {detail.ledger_records.length === 0 ? (
                <Empty>no ledger records in scope</Empty>
              ) : (
                <ul className="space-y-1">
                  {detail.ledger_records.map((l, i) => (
                    <li
                      key={i}
                      className="rounded bg-pipeline-bg/60 px-2 py-1 text-[11px]"
                    >
                      <div className="flex justify-between">
                        <span className="font-mono text-[10px]">
                          {String(l.scope ?? "-")}
                        </span>
                        <span className="text-pipeline-muted">
                          rev {String(l.revision ?? "-")}
                        </span>
                      </div>
                      <div>{String(l.statement ?? l.text ?? "")}</div>
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section title="Current rung (content / infra ladder)">
              {Object.keys(detail.current_rung || {}).length === 0 ? (
                <Empty>no rung recorded</Empty>
              ) : (
                <pre className="whitespace-pre-wrap rounded bg-pipeline-bg/60 p-2 font-mono text-[10px]">
                  {JSON.stringify(detail.current_rung, null, 2)}
                </pre>
              )}
            </Section>

            <Section title="Latest preview assembly (workstream G)">
              {Object.keys(detail.latest_preview || {}).length === 0 ? (
                <Empty>no preview assembly yet</Empty>
              ) : (
                <pre className="whitespace-pre-wrap rounded bg-pipeline-bg/60 p-2 font-mono text-[10px]">
                  {JSON.stringify(detail.latest_preview, null, 2)}
                </pre>
              )}
            </Section>
          </>
        )}
      </div>
    </aside>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-pipeline-muted">
        {title}
      </h3>
      {children}
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
