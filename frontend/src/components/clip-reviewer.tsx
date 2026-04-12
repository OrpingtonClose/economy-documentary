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
  const [mounted, setMounted] = useState(false);
  const [clips, setClips] = useState<ClipReviewItem[]>([]);
  const [gateBlocked, setGateBlocked] = useState(false);
  const [gateMessage, setGateMessage] = useState("");
  const [stageApproved, setStageApproved] = useState(false);
  const [approving, setApproving] = useState(false);

  // Feedback modal state
  const [feedbackModal, setFeedbackModal] = useState<{
    open: boolean;
    type: "approve" | "reject" | "regenerate";
    clipIdx: number;
    comment: string;
  }>({ open: false, type: "approve", clipIdx: -1, comment: "" });
  const [feedbackSent, setFeedbackSent] = useState<Record<number, string>>({});

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
    setMounted(true);
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

  const openFeedbackModal = (type: "approve" | "reject" | "regenerate", clipIdx: number) => {
    setFeedbackModal({ open: true, type, clipIdx, comment: "" });
  };

  const submitClipFeedback = async () => {
    const { type, clipIdx, comment } = feedbackModal;
    const clip = clips[clipIdx];
    if (!clip) return;

    try {
      if (type === "regenerate") {
        await fetch(`${BACKEND_URL}/agui/regenerate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            level: "clip",
            artifact_id: `clip_s${clip.scene_num}_p${clip.phrase_idx}`,
            scene_num: clip.scene_num,
            comment,
          }),
        });
      } else {
        await fetch(`${BACKEND_URL}/agui/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            feedback_type: type,
            artifact_id: `clip_s${clip.scene_num}_p${clip.phrase_idx}`,
            scene_num: clip.scene_num,
            comment,
          }),
        });
      }
      setFeedbackSent((prev) => ({ ...prev, [clipIdx]: type }));
      // Also update local state
      const updated = [...clips];
      if (type === "approve") {
        updated[clipIdx] = { ...clip, status: "approved" };
      } else if (type === "reject") {
        updated[clipIdx] = { ...clip, status: "rejected" };
      }
      setClips(updated);
    } catch {
      // ignore
    }
    setFeedbackModal({ open: false, type: "approve", clipIdx: -1, comment: "" });
  };

  if (!mounted) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-xl mb-2">Loading...</div>
        </div>
      </div>
    );
  }

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
      {/* Feedback Modal */}
      {feedbackModal.open && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="bg-pipeline-card border border-gray-600 rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-3">
              {feedbackModal.type === "approve" ? (
                <span className="text-green-400">Approve Clip</span>
              ) : feedbackModal.type === "reject" ? (
                <span className="text-red-400">Reject Clip</span>
              ) : (
                <span className="text-purple-400">Regenerate Clip</span>
              )}
            </h3>
            <p className="text-sm text-pipeline-muted mb-3">
              Scene {clips[feedbackModal.clipIdx]?.scene_num}, Phrase{" "}
              {clips[feedbackModal.clipIdx]?.phrase_idx}
            </p>
            <textarea
              className="w-full bg-pipeline-bg border border-gray-600 rounded p-3 text-sm text-white placeholder-gray-500 focus:border-pipeline-accent focus:outline-none"
              rows={4}
              placeholder={
                feedbackModal.type === "approve"
                  ? "Why approve? (optional — e.g., good motion, matches mood)"
                  : feedbackModal.type === "reject"
                  ? "Why reject? (required — e.g., artifacts, wrong movement, doesn't match prompt)"
                  : "Regeneration guidance (required — e.g., needs slower camera, wrong color grade)"
              }
              value={feedbackModal.comment}
              onChange={(e) =>
                setFeedbackModal((prev) => ({ ...prev, comment: e.target.value }))
              }
              autoFocus
            />
            <div className="flex gap-3 mt-4">
              <button
                onClick={submitClipFeedback}
                disabled={feedbackModal.type !== "approve" && !feedbackModal.comment.trim()}
                className={`flex-1 py-2 rounded text-sm font-medium transition-colors disabled:opacity-40 ${
                  feedbackModal.type === "approve"
                    ? "bg-green-700 hover:bg-green-600 text-white"
                    : feedbackModal.type === "reject"
                    ? "bg-red-700 hover:bg-red-600 text-white"
                    : "bg-purple-700 hover:bg-purple-600 text-white"
                }`}
              >
                Submit
              </button>
              <button
                onClick={() =>
                  setFeedbackModal({ open: false, type: "approve", clipIdx: -1, comment: "" })
                }
                className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

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
              <span className="text-xs text-pipeline-muted break-words">
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

          <div className="flex gap-2 items-center">
            {feedbackSent[idx] ? (
              <span
                className={`text-xs px-3 py-1 rounded ${
                  feedbackSent[idx] === "approve"
                    ? "bg-green-900 text-green-300"
                    : feedbackSent[idx] === "reject"
                    ? "bg-red-900 text-red-300"
                    : "bg-purple-900 text-purple-300"
                }`}
              >
                {feedbackSent[idx] === "approve"
                  ? "Approved"
                  : feedbackSent[idx] === "reject"
                  ? "Rejected"
                  : "Regenerating"}{" "}
                — feedback sent
              </span>
            ) : (
              <>
                <button
                  onClick={() => openFeedbackModal("approve", idx)}
                  className={`px-3 py-1 rounded text-xs transition-colors ${
                    clip.status === "approved"
                      ? "bg-green-600 text-white"
                      : "bg-green-800 text-green-200 hover:bg-green-700"
                  }`}
                >
                  Approve
                </button>
                <button
                  onClick={() => openFeedbackModal("reject", idx)}
                  className={`px-3 py-1 rounded text-xs transition-colors ${
                    clip.status === "rejected"
                      ? "bg-red-600 text-white"
                      : "bg-red-800 text-red-200 hover:bg-red-700"
                  }`}
                >
                  Reject
                </button>
                <button
                  onClick={() => openFeedbackModal("regenerate", idx)}
                  className="px-3 py-1 bg-pipeline-blue text-pipeline-text rounded text-xs hover:bg-pipeline-accent transition-colors"
                >
                  Regenerate
                </button>
              </>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
