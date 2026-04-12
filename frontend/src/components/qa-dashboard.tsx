"use client";

import { useEffect, useState, useCallback } from "react";
import type { QAResult, PipelinePhase } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/**
 * QA Dashboard -- displays Timeline Guardian validation results.
 *
 * Data source: GET /agui/qa-results (derives pass/fail from pipeline output files)
 */
export function QADashboard() {
  const [results, setResults] = useState<QAResult[]>([]);

  const phases: PipelinePhase[] = [
    "scenario",
    "audio",
    "visual_direction",
    "production",
    "assembly",
  ];

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
          return (
            <div key={phase} className="bg-pipeline-card rounded-lg p-4">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold capitalize">
                  {phase.replace("_", " ")}
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
            </div>
          );
        })}
      </div>
    </div>
  );
}
