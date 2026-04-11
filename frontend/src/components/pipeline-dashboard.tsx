"use client";

import { useEffect, useState } from "react";
import type { PipelineSnapshot, PipelineEvent } from "@/lib/types";
import { ToolCallCard } from "@/components/tool-call-card";

const DASHBOARD_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export function PipelineDashboard() {
  const [snapshot, setSnapshot] = useState<PipelineSnapshot | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const eventSource = new EventSource(`${DASHBOARD_URL}/dashboard/stream`);

    eventSource.onopen = () => setConnected(true);
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // SSE sends {"status":"idle"} when no pipeline is active —
        // only update snapshot when we have a real run payload.
        if (data.run_id) {
          setSnapshot(data as PipelineSnapshot);
        } else if (data.status === "idle") {
          setSnapshot(null);
          setConnected(true);
        }
      } catch {
        // ignore parse errors
      }
    };
    eventSource.onerror = () => setConnected(false);

    return () => eventSource.close();
  }, []);

  if (!snapshot) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-2xl mb-2">
            {connected ? "Waiting for pipeline..." : "Connecting..."}
          </div>
          <div className="text-pipeline-muted">
            Start a documentary topic in the chat to begin
          </div>
        </div>
      </div>
    );
  }

  const phaseNames = [
    "scenario",
    "audio",
    "visual_direction",
    "production",
    "assembly",
  ];

  return (
    <div className="space-y-6">
      {/* KPI Cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KPICard label="Status" value={snapshot.status} />
        <KPICard label="Elapsed" value={`${snapshot.elapsed_sec}s`} />
        <KPICard label="Tool Calls" value={String(snapshot.total_tools)} />
        <KPICard label="LLM Calls" value={String(snapshot.total_llm_calls)} />
      </div>

      {/* Phase Progress */}
      <div className="bg-pipeline-card rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3 text-pipeline-accent">
          Pipeline Phases
        </h2>
        <div className="flex gap-2">
          {phaseNames.map((phase, idx) => {
            const isActive = snapshot.active_phase === phase;
            const isCompleted = idx < snapshot.phases_completed;
            return (
              <div
                key={phase}
                className={`flex-1 rounded-md p-3 text-center text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-pipeline-accent text-white"
                    : isCompleted
                    ? "bg-green-800 text-green-200"
                    : "bg-pipeline-blue text-pipeline-muted"
                }`}
              >
                {phase.replace("_", " ")}
              </div>
            );
          })}
        </div>
      </div>

      {/* Recent Events */}
      <div className="bg-pipeline-card rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3 text-pipeline-accent">
          Recent Events
        </h2>
        <div className="space-y-1 max-h-64 overflow-auto">
          {(snapshot.recent_events ?? []).map((event, idx) => (
            <ToolCallCard key={idx} event={event} />
          ))}
          {(snapshot.recent_events ?? []).length === 0 && (
            <div className="text-pipeline-muted text-sm">No events yet</div>
          )}
        </div>
      </div>

      {/* Force End Warning */}
      {snapshot.force_end && (
        <div className="bg-red-900 border border-red-600 rounded-lg p-4">
          <div className="text-red-300 font-semibold">
            Context window limit reached — pipeline is wrapping up
          </div>
        </div>
      )}
    </div>
  );
}

function KPICard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-pipeline-card rounded-lg p-4 text-center">
      <div className="text-2xl font-bold text-pipeline-accent">{value}</div>
      <div className="text-sm text-pipeline-muted">{label}</div>
    </div>
  );
}
