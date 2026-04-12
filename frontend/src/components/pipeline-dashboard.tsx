"use client";

import { useEffect, useState, useCallback } from "react";
import type { PipelineSnapshot, Artifact, Escalation } from "@/lib/types";
import { ToolCallCard } from "@/components/tool-call-card";

const DASHBOARD_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export function PipelineDashboard() {
  const [snapshot, setSnapshot] = useState<PipelineSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [activeTab, setActiveTab] = useState<"pipeline" | "artifacts" | "escalations">("pipeline");

  // Pipeline SSE stream
  useEffect(() => {
    const eventSource = new EventSource(`${DASHBOARD_URL}/dashboard/stream`);

    eventSource.onopen = () => setConnected(true);
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
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

  // AG-UI SSE stream for artifacts and escalations
  useEffect(() => {
    const aguiSource = new EventSource(`${DASHBOARD_URL}/agui/stream`);

    aguiSource.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "artifact" || msg.type === "artifact_update") {
          fetchArtifacts();
        } else if (msg.type === "escalation") {
          fetchEscalations();
        }
      } catch {
        // ignore
      }
    };

    return () => aguiSource.close();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchArtifacts = useCallback(async () => {
    try {
      const res = await fetch(`${DASHBOARD_URL}/agui/artifacts`);
      const data = await res.json();
      setArtifacts(data.artifacts || []);
    } catch {
      // ignore fetch errors
    }
  }, []);

  const fetchEscalations = useCallback(async () => {
    try {
      const res = await fetch(`${DASHBOARD_URL}/agui/escalations`);
      const data = await res.json();
      setEscalations(data.escalations || []);
    } catch {
      // ignore fetch errors
    }
  }, []);

  // Initial load
  useEffect(() => {
    fetchArtifacts();
    fetchEscalations();
  }, [fetchArtifacts, fetchEscalations]);

  const pendingEscalations = escalations.filter((e) => !e.resolved);

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
      {/* Tab Navigation */}
      <div className="flex gap-2 border-b border-gray-700 pb-2">
        {(["pipeline", "artifacts", "escalations"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 rounded-t-md text-sm font-medium transition-colors ${
              activeTab === tab
                ? "bg-pipeline-accent text-white"
                : "bg-pipeline-blue text-pipeline-muted hover:text-white"
            }`}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
            {tab === "escalations" && pendingEscalations.length > 0 && (
              <span className="ml-2 bg-red-600 text-white rounded-full px-2 py-0.5 text-xs">
                {pendingEscalations.length}
              </span>
            )}
            {tab === "artifacts" && artifacts.length > 0 && (
              <span className="ml-2 bg-gray-600 text-white rounded-full px-2 py-0.5 text-xs">
                {artifacts.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* KPI Cards (always visible) */}
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

      {/* Pending Escalations Alert */}
      {pendingEscalations.length > 0 && activeTab !== "escalations" && (
        <div
          className="bg-red-900 border border-red-600 rounded-lg p-4 cursor-pointer"
          onClick={() => setActiveTab("escalations")}
        >
          <div className="text-red-300 font-semibold">
            {pendingEscalations.length} escalation{pendingEscalations.length > 1 ? "s" : ""} need your attention
          </div>
          <div className="text-red-400 text-sm mt-1">
            {pendingEscalations[0]?.diagnosis?.root_cause || "Pipeline needs human decision"}
          </div>
        </div>
      )}

      {/* Tab Content */}
      {activeTab === "pipeline" && (
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
      )}

      {activeTab === "artifacts" && (
        <ArtifactPanel artifacts={artifacts} />
      )}

      {activeTab === "escalations" && (
        <EscalationPanel escalations={escalations} />
      )}

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

// ---------------------------------------------------------------------------
// Artifact Panel
// ---------------------------------------------------------------------------

function ArtifactPanel({ artifacts }: { artifacts: Artifact[] }) {
  const videoClips = artifacts.filter((a) => a.type === "video_clip");
  const narrations = artifacts.filter((a) => a.type === "narration");

  return (
    <div className="space-y-4">
      <div className="bg-pipeline-card rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3 text-pipeline-accent">
          Video Clips ({videoClips.length})
        </h2>
        {videoClips.length === 0 ? (
          <div className="text-pipeline-muted text-sm">No video clips yet</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {videoClips.map((clip) => (
              <ArtifactCard key={clip.id} artifact={clip} />
            ))}
          </div>
        )}
      </div>

      <div className="bg-pipeline-card rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3 text-pipeline-accent">
          Narration Clips ({narrations.length})
        </h2>
        {narrations.length === 0 ? (
          <div className="text-pipeline-muted text-sm">No narration clips yet</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {narrations.map((clip) => (
              <ArtifactCard key={clip.id} artifact={clip} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single artifact card with feedback buttons
// ---------------------------------------------------------------------------

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const [feedbackSent, setFeedbackSent] = useState<string | null>(null);

  const statusColors: Record<string, string> = {
    generating: "bg-yellow-700 text-yellow-200",
    pending_review: "bg-blue-700 text-blue-200",
    approved: "bg-green-700 text-green-200",
    rejected: "bg-red-700 text-red-200",
    regenerating: "bg-purple-700 text-purple-200",
  };

  const sendFeedback = async (feedbackType: string) => {
    try {
      await fetch(`${DASHBOARD_URL}/agui/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feedback_type: feedbackType,
          artifact_id: artifact.id,
          scene_num: artifact.scene_num,
        }),
      });
      setFeedbackSent(feedbackType);
    } catch {
      // ignore
    }
  };

  const triggerRegenerate = async () => {
    try {
      await fetch(`${DASHBOARD_URL}/agui/regenerate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          level: "clip",
          artifact_id: artifact.id,
          scene_num: artifact.scene_num,
        }),
      });
      setFeedbackSent("regenerate");
    } catch {
      // ignore
    }
  };

  return (
    <div className="bg-pipeline-blue rounded-lg p-3 border border-gray-700">
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-white">
          S{artifact.scene_num}
          {artifact.phrase_idx > 0 && `.${artifact.phrase_idx}`}
          {artifact.language && ` (${artifact.language.toUpperCase()})`}
        </span>
        <span className={`text-xs px-2 py-0.5 rounded-full ${statusColors[artifact.status] || "bg-gray-600 text-gray-200"}`}>
          {artifact.status.replace("_", " ")}
        </span>
      </div>

      {/* Details */}
      <div className="text-xs text-pipeline-muted space-y-1">
        <div>{artifact.type.replace("_", " ")}</div>
        {artifact.duration_sec > 0 && <div>{artifact.duration_sec.toFixed(1)}s</div>}
        {artifact.qa_scores?.quality && (
          <div>QA: {artifact.qa_scores.quality}</div>
        )}
        {artifact.metadata?.prompt ? (
          <div className="truncate" title={String(artifact.metadata.prompt)}>
            {String(artifact.metadata.prompt).slice(0, 60)}...
          </div>
        ) : null}
      </div>

      {/* Feedback Buttons */}
      {artifact.status === "pending_review" && !feedbackSent && (
        <div className="flex gap-2 mt-3">
          <button
            onClick={() => sendFeedback("approve")}
            className="flex-1 bg-green-700 hover:bg-green-600 text-white text-xs py-1 px-2 rounded transition-colors"
          >
            Approve
          </button>
          <button
            onClick={() => sendFeedback("reject")}
            className="flex-1 bg-red-700 hover:bg-red-600 text-white text-xs py-1 px-2 rounded transition-colors"
          >
            Reject
          </button>
          <button
            onClick={triggerRegenerate}
            className="flex-1 bg-purple-700 hover:bg-purple-600 text-white text-xs py-1 px-2 rounded transition-colors"
          >
            Regen
          </button>
        </div>
      )}

      {feedbackSent && (
        <div className="text-xs text-green-400 mt-2">
          {feedbackSent} sent
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Escalation Panel
// ---------------------------------------------------------------------------

function EscalationPanel({ escalations }: { escalations: Escalation[] }) {
  const pending = escalations.filter((e) => !e.resolved);
  const resolved = escalations.filter((e) => e.resolved);

  return (
    <div className="space-y-4">
      <div className="bg-pipeline-card rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3 text-red-400">
          Pending Escalations ({pending.length})
        </h2>
        {pending.length === 0 ? (
          <div className="text-pipeline-muted text-sm">No pending escalations</div>
        ) : (
          <div className="space-y-3">
            {pending.map((esc) => (
              <EscalationCard key={esc.id} escalation={esc} />
            ))}
          </div>
        )}
      </div>

      {resolved.length > 0 && (
        <div className="bg-pipeline-card rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-3 text-pipeline-muted">
            Resolved ({resolved.length})
          </h2>
          <div className="space-y-3">
            {resolved.map((esc) => (
              <EscalationCard key={esc.id} escalation={esc} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single escalation card with action buttons
// ---------------------------------------------------------------------------

function EscalationCard({ escalation }: { escalation: Escalation }) {
  const [responding, setResponding] = useState(false);

  const respondToEscalation = async (action: string) => {
    setResponding(true);
    try {
      await fetch(`${DASHBOARD_URL}/agui/escalations/${escalation.id}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
    } catch {
      // ignore
    }
    setResponding(false);
  };

  const severityColors: Record<string, string> = {
    warning: "border-yellow-600 bg-yellow-900/30",
    critical: "border-red-600 bg-red-900/30",
  };

  return (
    <div className={`rounded-lg p-4 border ${severityColors[escalation.severity] || "border-gray-600"}`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold text-white">
          {escalation.operation_name}
        </span>
        <span className={`text-xs px-2 py-0.5 rounded-full ${
          escalation.resolved ? "bg-green-700 text-green-200" : "bg-red-700 text-red-200"
        }`}>
          {escalation.resolved ? "resolved" : escalation.severity}
        </span>
      </div>

      {/* Diagnosis */}
      <div className="mb-3">
        <div className="text-sm text-red-300 font-medium">
          {escalation.diagnosis?.root_cause || "Unknown root cause"}
        </div>
        <div className="text-xs text-pipeline-muted mt-1">
          Confidence: {escalation.diagnosis?.confidence || "unknown"}
        </div>
        {escalation.diagnosis?.proposed_fix && (
          <div className="text-xs text-yellow-400 mt-1">
            Proposed fix: {escalation.diagnosis.proposed_fix}
          </div>
        )}
      </div>

      {/* Error Chain */}
      {escalation.error_chain && escalation.error_chain.length > 0 && (
        <div className="mb-3">
          <div className="text-xs font-medium text-pipeline-muted mb-1">
            Recovery attempts ({escalation.error_chain.length}):
          </div>
          <div className="space-y-1 max-h-32 overflow-auto">
            {escalation.error_chain.map((attempt, idx) => (
              <div key={idx} className="text-xs text-gray-400 bg-gray-800 rounded px-2 py-1">
                L{attempt.level} #{attempt.attempt}: {attempt.strategy}
                {attempt.error && (
                  <span className="text-red-400 ml-1">— {attempt.error.slice(0, 80)}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Action Buttons */}
      {!escalation.resolved && !responding && (
        <div className="flex gap-2">
          <button
            onClick={() => respondToEscalation("retry_with_fix")}
            className="flex-1 bg-blue-700 hover:bg-blue-600 text-white text-xs py-2 px-3 rounded transition-colors"
          >
            Retry with Fix
          </button>
          <button
            onClick={() => respondToEscalation("skip")}
            className="flex-1 bg-yellow-700 hover:bg-yellow-600 text-white text-xs py-2 px-3 rounded transition-colors"
          >
            Skip
          </button>
          <button
            onClick={() => respondToEscalation("abort")}
            className="flex-1 bg-red-700 hover:bg-red-600 text-white text-xs py-2 px-3 rounded transition-colors"
          >
            Abort
          </button>
        </div>
      )}

      {responding && (
        <div className="text-xs text-yellow-400 mt-2">Sending response...</div>
      )}

      {escalation.resolved && escalation.response && (
        <div className="text-xs text-green-400 mt-2">
          Resolved: {String(escalation.response.action || "acknowledged")}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI Card
// ---------------------------------------------------------------------------

function KPICard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-pipeline-card rounded-lg p-4 text-center">
      <div className="text-2xl font-bold text-pipeline-accent">{value}</div>
      <div className="text-sm text-pipeline-muted">{label}</div>
    </div>
  );
}
