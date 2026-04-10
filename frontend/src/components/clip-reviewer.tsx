"use client";

import { useState } from "react";

/**
 * Gate 3: Clip Reviewer — video + text rejection interface.
 *
 * Displays generated video clips alongside their narration text.
 * User can approve, reject, or request regeneration of individual clips.
 */

interface ClipReviewItem {
  scene_num: number;
  phrase_idx: number;
  video_path: string;
  narration_text: string;
  duration: number;
  lora_id: string;
  status: "pending" | "approved" | "rejected";
}

export function ClipReviewer() {
  const [clips, setClips] = useState<ClipReviewItem[]>([]);

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
                {clip.duration.toFixed(1)}s
              </span>
              <span className="text-xs px-2 py-1 rounded bg-pipeline-blue">
                {clip.lora_id}
              </span>
            </div>
          </div>

          {/* Video preview placeholder */}
          <div className="mb-3 bg-pipeline-bg rounded-lg aspect-video flex items-center justify-center">
            <span className="text-pipeline-muted">Video Preview</span>
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
