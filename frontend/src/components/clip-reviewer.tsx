"use client";

import { useEffect, useState, useCallback } from "react";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/**
 * Gate 3: Clip Reviewer — video + text rejection interface.
 *
 * Displays generated video clips alongside their narration text.
 * User can approve, reject, or request regeneration of individual clips.
 *
 * Data source: GET /agui/clips (reads video status files on disk)
 */

interface ClipReviewItem {
  scene_num: number;
  phrase_idx: number;
  video_path: string;
  narration_text: string;
  duration: number;
  lora_id: string;
  status: "pending" | "approved" | "rejected";
  quality?: string;
  qa_reason?: string;
  attempts?: number;
}

export function ClipReviewer() {
  const [clips, setClips] = useState<ClipReviewItem[]>([]);
  const [gateBlocked, setGateBlocked] = useState(false);
  const [gateMessage, setGateMessage] = useState("");
  const [stageApproved, setStageApproved] = useState(false);
  const [approving, setApproving] = useState(false);

  // Poll AG-UI /clips endpoint
  const fetchClips = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/agui/clips`);
      const data = await res.json();
      if (data.gate?.blocked) {
        setGateBlocked(true);
        setGateMessage(data.gate.message || "Waiting for previous stage approval");
        return;
      }
      setGateBlocked(false);
      if (data.clips && data.clips.length > 0) {
        setClips(data.clips);
      }
    } catch {
      // ignore fetch errors
    }
  }, []);

  // Check approval state
  const fetchApprovalState = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/agui/approval-state`);
      const data = await res.json();
      if (data.state?.clips?.approved) {
        setStageApproved(true);
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    fetchClips();
    fetchApprovalState();
    const interval = setInterval(() => {
      fetchClips();
      fetchApprovalState();
    }, 5000);
    return () => clearInterval(interval);
  }, [fetchClips, fetchApprovalState]);

  const handleApproveAll = async () => {
    setApproving(true);
    try {
      const res = await fetch(`${BACKEND_URL}/agui/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage: "clips" }),
      });
      if (res.ok) {
        setStageApproved(true);
      }
    } catch {
      // ignore
    } finally {
      setApproving(false);
    }
  };

  if (gateBlocked) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-xl mb-2 text-yellow-400">Waiting for Approval</div>
          <div className="text-pipeline-muted">{gateMessage}</div>
          <div className="mt-4 text-sm text-pipeline-muted">
            Approve visual prompts on the Prompt Reviewer tab to unlock this stage
          </div>
        </div>
      </div>
    );
  }

  if (clips.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-xl mb-2">No Clips Yet</div>
          <div className="text-pipeline-muted">
            Video clips will appear after the production supervisor generates them
          </div>
        </div>
      </div>
    );
  }

  const approvedCount = clips.filter((c) => c.status === "approved").length;
  const rejectedCount = clips.filter((c) => c.status === "rejected").length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-pipeline-accent">
          Clip Review: {clips.length} Clips
        </h2>
        <div className="flex gap-4 text-sm">
          {stageApproved ? (
            <span className="px-3 py-1 bg-green-900 text-green-300 rounded-md">
              Clips Approved
            </span>
          ) : (
            <button
              onClick={handleApproveAll}
              disabled={approving}
              className="px-3 py-1 bg-green-700 text-white rounded-md hover:bg-green-600 disabled:opacity-50"
            >
              {approving ? "Approving..." : "Approve All Clips"}
            </button>
          )}
          <span className="text-green-400">{approvedCount} approved</span>
          <span className="text-red-400">{rejectedCount} rejected</span>
          <span className="text-pipeline-muted">
            {clips.length - approvedCount - rejectedCount} pending
          </span>
        </div>
      </div>

      {clips.map((clip, idx) => (
        <div key={idx} className="bg-pipeline-card rounded-lg p-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="font-semibold">
              Scene {clip.scene_num}, Phrase {clip.phrase_idx}
            </h3>
            <div className="flex items-center gap-2">
              <span className="text-sm text-pipeline-muted">
                {clip.duration > 0 ? `${clip.duration.toFixed(1)}s` : ""}
              </span>
              {clip.lora_id && (
                <span className="text-xs px-2 py-1 rounded bg-pipeline-blue">
                  {clip.lora_id}
                </span>
              )}
            </div>
          </div>

          {/* QA Status */}
          <div className="mb-3 flex items-center gap-2">
            <span
              className={`text-xs px-2 py-1 rounded-full ${
                clip.quality === "acceptable"
                  ? "bg-green-800 text-green-200"
                  : clip.quality === "unknown"
                  ? "bg-yellow-800 text-yellow-200"
                  : "bg-red-800 text-red-200"
              }`}
            >
              QA: {clip.quality || "pending"}
            </span>
            {clip.attempts && clip.attempts > 1 && (
              <span className="text-xs text-pipeline-muted">
                ({clip.attempts} attempts)
              </span>
            )}
            {clip.qa_reason && (
              <span className="text-xs text-pipeline-muted truncate max-w-xs">
                {clip.qa_reason}
              </span>
            )}
          </div>

          {/* Video preview placeholder */}
          <div className="mb-3 bg-pipeline-bg rounded-lg aspect-video flex items-center justify-center">
            {clip.video_path ? (
              <span className="text-green-400 text-sm">Video generated</span>
            ) : (
              <span className="text-pipeline-muted">Generating...</span>
            )}
          </div>

          {/* Narration text */}
          <div className="mb-3 text-sm italic text-pipeline-muted">
            &ldquo;{clip.narration_text}&rdquo;
          </div>

          <div className="flex gap-2">
            <button
              onClick={() => {
                const updated = [...clips];
                updated[idx] = { ...clip, status: "approved" };
                setClips(updated);
              }}
              className={`px-3 py-1 rounded text-xs ${
                clip.status === "approved"
                  ? "bg-green-600 text-white"
                  : "bg-green-800 text-green-200 hover:bg-green-700"
              }`}
            >
              Approve
            </button>
            <button
              onClick={() => {
                const updated = [...clips];
                updated[idx] = { ...clip, status: "rejected" };
                setClips(updated);
              }}
              className={`px-3 py-1 rounded text-xs ${
                clip.status === "rejected"
                  ? "bg-red-600 text-white"
                  : "bg-red-800 text-red-200 hover:bg-red-700"
              }`}
            >
              Reject
            </button>
            <button className="px-3 py-1 bg-pipeline-blue text-pipeline-text rounded text-xs hover:bg-pipeline-accent">
              Regenerate
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
