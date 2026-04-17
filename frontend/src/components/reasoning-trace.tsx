"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import type { ReasoningDigest, ReasoningTrace } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/** Phase badge colors */
const PHASE_COLORS: Record<string, string> = {
  scenario: "bg-blue-900/50 text-blue-300",
  audio: "bg-purple-900/50 text-purple-300",
  visual_direction: "bg-cyan-900/50 text-cyan-300",
  production: "bg-orange-900/50 text-orange-300",
  assembly: "bg-green-900/50 text-green-300",
  pipeline: "bg-gray-700/50 text-gray-300",
  unknown: "bg-gray-800/50 text-gray-400",
};

/** Importance styling */
const IMPORTANCE_BORDER: Record<string, string> = {
  high: "border-l-red-400",
  medium: "border-l-yellow-500/50",
  low: "border-l-gray-600/30",
};

const IMPORTANCE_BG: Record<string, string> = {
  high: "bg-red-950/20",
  medium: "",
  low: "opacity-70",
};

function formatTime(timestamp: number): string {
  const d = new Date(timestamp * 1000);
  return d.toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatRelative(timestamp: number): string {
  const ago = Date.now() / 1000 - timestamp;
  if (ago < 60) return `${Math.floor(ago)}s ago`;
  if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
  return `${Math.floor(ago / 3600)}h ago`;
}

/** Render markdown-style bold (**text**) in summaries */
function RichSummary({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return (
    <span>
      {parts.map((part, i) => {
        if (part.startsWith("**") && part.endsWith("**")) {
          return (
            <span key={i} className="font-semibold text-pipeline-accent">
              {part.slice(2, -2)}
            </span>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </span>
  );
}

/** Raw trace row for drill-down */
function RawTraceRow({ trace }: { trace: ReasoningTrace }) {
  const [expanded, setExpanded] = useState(false);
  const hasContent = trace.content && trace.content.length > 0;

  return (
    <div
      className="flex items-start gap-2 text-[11px] py-0.5 hover:bg-white/5 cursor-pointer"
      onClick={() => hasContent && setExpanded(!expanded)}
    >
      <span className="text-gray-500 font-mono shrink-0 w-14">
        {formatTime(trace.timestamp)}
      </span>
      <span className="text-gray-500 shrink-0 w-24 truncate">
        {trace.event_type}
      </span>
      <span className="text-gray-400 truncate">
        {trace.content
          ? trace.content.slice(0, 120) + (trace.content.length > 120 ? "…" : "")
          : "—"}
      </span>
      {expanded && hasContent && (
        <pre className="absolute left-0 right-0 mt-5 mx-4 z-10 text-xs text-gray-300 bg-gray-900 border border-gray-700 rounded p-2 max-h-64 overflow-auto whitespace-pre-wrap">
          {trace.content}
        </pre>
      )}
    </div>
  );
}

/** A single digest entry */
function DigestEntry({ digest }: { digest: ReasoningDigest }) {
  const [expanded, setExpanded] = useState(false);
  const [rawTraces, setRawTraces] = useState<ReasoningTrace[]>([]);
  const [loadingRaw, setLoadingRaw] = useState(false);

  const borderColor = IMPORTANCE_BORDER[digest.importance] || "border-l-gray-600";
  const bgColor = IMPORTANCE_BG[digest.importance] || "";
  const phaseColor = PHASE_COLORS[digest.phase] || PHASE_COLORS.unknown;

  const loadRawTraces = async () => {
    if (rawTraces.length > 0 || digest.raw_trace_ids.length === 0) return;
    setLoadingRaw(true);
    try {
      // Fetch raw traces by agent name around the digest timestamp
      const params = new URLSearchParams({
        agent: digest.agent,
        limit: "50",
        since: String(digest.timestamp - 1),
      });
      const res = await fetch(`${BACKEND_URL}/agui/reasoning/raw?${params}`);
      if (res.ok) {
        const data = await res.json();
        // Filter to only the trace IDs in this digest
        const idSet = new Set(digest.raw_trace_ids);
        const filtered = (data.traces || []).filter(
          (t: ReasoningTrace) => idSet.has(t.id)
        );
        setRawTraces(filtered.length > 0 ? filtered : data.traces || []);
      }
    } catch {
      // ignore
    }
    setLoadingRaw(false);
  };

  const handleExpand = () => {
    const next = !expanded;
    setExpanded(next);
    if (next) loadRawTraces();
  };

  const { details } = digest;

  return (
    <div
      className={`border-l-2 ${borderColor} ${bgColor} pl-3 py-2 mb-1 rounded-r transition-colors hover:bg-white/5`}
    >
      {/* Header row */}
      <div
        className="flex items-start gap-2 cursor-pointer"
        onClick={handleExpand}
      >
        <span className="text-gray-500 text-xs font-mono shrink-0 w-14">
          {formatTime(digest.timestamp)}
        </span>
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${phaseColor}`}
        >
          {digest.phase}
        </span>
        <span className="text-sm text-gray-200 leading-snug">
          <RichSummary text={digest.summary} />
        </span>
      </div>

      {/* Inline detail badges */}
      {(details.rating || details.tokens || details.errors) && (
        <div className="flex items-center gap-2 mt-1 ml-16 flex-wrap">
          {details.rating && (
            <span
              className={`text-[10px] px-1.5 py-0.5 rounded ${
                details.rating === "EXCELLENT"
                  ? "bg-green-900/50 text-green-300"
                  : details.rating === "GOOD"
                    ? "bg-blue-900/50 text-blue-300"
                    : details.rating === "FAIR"
                      ? "bg-yellow-900/50 text-yellow-300"
                      : "bg-red-900/50 text-red-300"
              }`}
            >
              {details.rating}
            </span>
          )}
          {details.tokens && (
            <span className="text-[10px] text-gray-500">
              {details.tokens.in.toLocaleString()}→
              {details.tokens.out.toLocaleString()} tok
            </span>
          )}
          {details.errors && details.errors.length > 0 && (
            <span className="text-[10px] text-red-400">
              {details.errors.length} error
              {details.errors.length > 1 ? "s" : ""}
            </span>
          )}
          {details.tools_used && details.tools_used.length > 0 && (
            <span className="text-[10px] text-purple-400">
              {details.tools_used.join(", ")}
            </span>
          )}
          {details.plan && (
            <span className="text-[10px] text-orange-400">
              plan: {details.plan.batches} batches
              {details.plan.strategy ? ` (${details.plan.strategy})` : ""}
              {details.plan.estimated_gpu_minutes
                ? ` ~${details.plan.estimated_gpu_minutes}min GPU`
                : ""}
            </span>
          )}
        </div>
      )}

      {/* Expanded: raw traces */}
      {expanded && (
        <div className="mt-2 ml-14 border-t border-white/5 pt-2">
          {details.feedback && (
            <p className="text-xs text-gray-400 italic mb-2">
              {details.feedback}
            </p>
          )}
          {details.focus_areas && details.focus_areas.length > 0 && (
            <p className="text-xs text-gray-500 mb-2">
              Focus: {details.focus_areas.join(", ")}
            </p>
          )}
          {loadingRaw ? (
            <p className="text-xs text-gray-500">Loading raw traces…</p>
          ) : rawTraces.length > 0 ? (
            <div className="relative">
              <p className="text-[10px] text-gray-500 mb-1">
                Raw events ({rawTraces.length}):
              </p>
              {rawTraces.map((t) => (
                <RawTraceRow key={t.id} trace={t} />
              ))}
            </div>
          ) : (
            <p className="text-[10px] text-gray-500">
              {digest.raw_trace_ids.length} raw events (click to expand)
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function ReasoningTracePanel() {
  const [digests, setDigests] = useState<ReasoningDigest[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [phaseFilter, setPhaseFilter] = useState<string>("all");
  const [importanceFilter, setImportanceFilter] = useState<string>("all");
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastTimestampRef = useRef<number>(0);

  const fetchDigests = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (lastTimestampRef.current > 0) {
        params.set("since", String(lastTimestampRef.current));
      }
      if (phaseFilter !== "all") params.set("phase", phaseFilter);
      if (importanceFilter !== "all") params.set("importance", importanceFilter);

      const res = await fetch(
        `${BACKEND_URL}/agui/reasoning/digests?${params}`
      );
      if (!res.ok) return;
      const data = await res.json();
      const newDigests: ReasoningDigest[] = data.digests || [];

      if (newDigests.length > 0) {
        if (lastTimestampRef.current === 0) {
          setDigests(newDigests);
        } else {
          setDigests((prev) => {
            const existingIds = new Set(prev.map((d) => d.id));
            const fresh = newDigests.filter((d) => !existingIds.has(d.id));
            if (fresh.length === 0) return prev;
            const combined = [...prev, ...fresh];
            return combined.slice(-300);
          });
        }
        lastTimestampRef.current =
          newDigests[newDigests.length - 1].timestamp;
      }
    } catch {
      // ignore
    }
  }, [phaseFilter, importanceFilter]);

  useEffect(() => {
    // Reset on filter change
    lastTimestampRef.current = 0;
    setDigests([]);
    fetchDigests();

    const interval = setInterval(fetchDigests, 2500);
    return () => clearInterval(interval);
  }, [fetchDigests]);

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [digests, autoScroll]);

  // Stats
  const highCount = digests.filter((d) => d.importance === "high").length;
  const totalTokensIn = digests.reduce(
    (sum, d) => sum + (d.details.tokens?.in || 0),
    0
  );
  const totalTokensOut = digests.reduce(
    (sum, d) => sum + (d.details.tokens?.out || 0),
    0
  );
  const phases = Array.from(new Set(digests.map((d) => d.phase))).sort();

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-pipeline-card border-b border-pipeline-blue">
        <div className="flex items-center gap-4 text-xs">
          <span className="text-pipeline-muted">
            Events: <span className="text-pipeline-accent">{digests.length}</span>
          </span>
          {totalTokensIn > 0 && (
            <span className="text-pipeline-muted">
              Tokens:{" "}
              <span className="text-yellow-400">
                {totalTokensIn.toLocaleString()}
              </span>
              →
              <span className="text-green-400">
                {totalTokensOut.toLocaleString()}
              </span>
            </span>
          )}
          {highCount > 0 && (
            <span className="text-red-400">
              {highCount} important
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <select
            value={phaseFilter}
            onChange={(e) => setPhaseFilter(e.target.value)}
            className="text-xs bg-black/30 text-gray-300 border border-white/10 rounded px-1 py-0.5"
          >
            <option value="all">All phases</option>
            {phases.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <select
            value={importanceFilter}
            onChange={(e) => setImportanceFilter(e.target.value)}
            className="text-xs bg-black/30 text-gray-300 border border-white/10 rounded px-1 py-0.5"
          >
            <option value="all">All importance</option>
            <option value="high">High only</option>
            <option value="medium">Medium+</option>
          </select>
          <label className="flex items-center gap-1 text-xs text-gray-400">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="rounded"
            />
            Auto-scroll
          </label>
        </div>
      </div>

      {/* Digest list */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto p-2"
        onScroll={() => {
          if (!scrollRef.current) return;
          const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
          if (scrollHeight - scrollTop - clientHeight > 100) {
            setAutoScroll(false);
          }
        }}
      >
        {digests.length === 0 ? (
          <div className="text-center text-gray-500 text-sm py-8">
            No reasoning digests yet. Start a pipeline run to see agent
            thinking here.
          </div>
        ) : (
          digests.map((digest) => (
            <DigestEntry key={digest.id} digest={digest} />
          ))
        )}
      </div>
    </div>
  );
}
