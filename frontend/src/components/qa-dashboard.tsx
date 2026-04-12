"use client";

import { useEffect, useState, useCallback } from "react";
import type { QAResult, PipelinePhase } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/**
 * QA Dashboard -- displays Timeline Guardian validation results
 * with expandable per-item drill-down.
 *
 * Data source: GET /agui/qa-results (derives pass/fail from pipeline output files)
 */
export function QADashboard() {
  const [results, setResults] = useState<QAResult[]>([]);
  const [expandedPhases, setExpandedPhases] = useState<Record<string, boolean>>({});

  const phases: PipelinePhase[] = [
    "scenario",
    "audio",
    "visual_direction",
    "production",
    "assembly",
  ];

  const togglePhase = (phase: string) => {
    setExpandedPhases((prev) => ({ ...prev, [phase]: !prev[phase] }));
  };

  // Poll AG-UI /qa-results endpoint
  const fetchQA = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/agui/qa-results`);
      const data = await res.json();
      if (data.results && data.results.length > 0) {
        setResults(data.results);
      }
    } catch {
      // ignore fetch errors
    }
  }, []);

  useEffect(() => {
    fetchQA();
    const interval = setInterval(fetchQA, 5000);
    return () => clearInterval(interval);
  }, [fetchQA]);

  if (results.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-xl mb-2">No QA Results Yet</div>
          <div className="text-pipeline-muted">
            The Timeline Guardian validates each pipeline phase automatically
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold text-pipeline-accent">
        Timeline Guardian — QA Results
      </h2>

      <div className="grid gap-4">
        {phases.map((phase) => {
          const result = results.find((r) => r.phase === phase);
          const hasDetails = result?.details && result.details.length > 0;
          const isExpanded = expandedPhases[phase];
          return (
            <div key={phase} className="bg-pipeline-card rounded-lg p-4">
              <div
                className={`flex items-center justify-between ${hasDetails ? "cursor-pointer" : ""}`}
                onClick={() => hasDetails && togglePhase(phase)}
              >
                <h3 className="font-semibold capitalize flex items-center gap-2">
                  {phase.replace("_", " ")}
                  {hasDetails && (
                    <span className="text-xs text-pipeline-muted">
                      {isExpanded ? "▼" : "▶"} {result.details?.length ?? 0} items
                    </span>
                  )}
                </h3>
                {result ? (
                  <span
                    className={`px-3 py-1 rounded-full text-xs font-medium ${
                      result.valid
                        ? "bg-green-800 text-green-200"
                        : "bg-red-800 text-red-200"
                    }`}
                  >
                    {result.valid ? "PASS" : "FAIL"}
                  </span>
                ) : (
                  <span className="px-3 py-1 rounded-full text-xs font-medium bg-pipeline-blue text-pipeline-muted">
                    PENDING
                  </span>
                )}
              </div>

              {result && !result.valid && result.errors && (
                <div className="mt-2 p-2 bg-red-900/30 rounded text-sm text-red-300">
                  {result.errors}
                </div>
              )}

              {result && result.valid && result.message && (
                <div className="mt-2 p-2 bg-green-900/30 rounded text-sm text-green-300">
                  {result.message}
                </div>
              )}

              {/* Expandable Details Drill-Down */}
              {isExpanded && result?.details && (
                <div className="mt-3 border-t border-gray-700 pt-3">
                  <div className="space-y-2 max-h-80 overflow-auto">
                    {result.details.map((detail, dIdx) => (
                      <QADetailRow key={dIdx} phase={phase} detail={detail} />
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function QADetailRow({ phase, detail }: { phase: string; detail: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);

  if (phase === "production") {
    const quality = String(detail.quality || "unknown");
    const passed = ["acceptable", "excellent", "good"].includes(quality);
    return (
      <div className="bg-pipeline-bg rounded p-2 text-xs">
        <div
          className="flex items-center justify-between cursor-pointer"
          onClick={() => setExpanded(!expanded)}
        >
          <span>
            Scene {String(detail.scene_num ?? "")}, Phrase {String(detail.phrase_idx ?? "")}
          </span>
          <div className="flex items-center gap-2">
            {detail.has_video ? (
              <span className="text-green-400">MP4</span>
            ) : (
              <span className="text-red-400">no video</span>
            )}
            <span className={passed ? "text-green-400" : "text-red-400"}>
              {quality}
            </span>
            <span className="text-pipeline-muted">{expanded ? "▼" : "▶"}</span>
          </div>
        </div>
        {expanded && (
          <div className="mt-2 pl-2 border-l border-gray-600 space-y-1 text-pipeline-muted">
            <div>Attempts: {String(detail.attempts ?? 0)}</div>
            {detail.qa_reason ? (
              <div className="text-yellow-400">Reason: {String(detail.qa_reason)}</div>
            ) : null}
          </div>
        )}
      </div>
    );
  }

  if (phase === "scenario") {
    return (
      <div className="bg-pipeline-bg rounded p-2 text-xs flex items-center justify-between">
        <span>
          Scene {String(detail.scene_num ?? "")}: {String(detail.title ?? "")}
        </span>
        <div className="flex items-center gap-2 text-pipeline-muted">
          <span>{String(detail.duration_sec ?? 0)}s</span>
          <span>{String(detail.voices ?? 0)} voices</span>
          {detail.has_hook ? <span className="text-yellow-400">hook</span> : null}
        </div>
      </div>
    );
  }

  if (phase === "audio") {
    return (
      <div className="bg-pipeline-bg rounded p-2 text-xs flex items-center justify-between">
        <span>{String(detail.file || "")}</span>
        <span className="text-pipeline-muted">{String(detail.size_kb || 0)} KB</span>
      </div>
    );
  }

  if (phase === "assembly") {
    return (
      <div className="bg-pipeline-bg rounded p-2 text-xs flex items-center justify-between">
        <span>{String(detail.file || "")}</span>
        <span className="text-pipeline-muted">{String(detail.size_mb || 0)} MB</span>
      </div>
    );
  }

  // Generic fallback
  return (
    <div className="bg-pipeline-bg rounded p-2 text-xs text-pipeline-muted">
      {JSON.stringify(detail)}
    </div>
  );
}
