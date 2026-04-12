"use client";

import { useEffect, useState, useCallback } from "react";
import type { TimelineStatus, TimelineTrack } from "@/lib/types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

/**
 * OTIO Timeline Visualization.
 *
 * Displays the multi-track timeline with video, narration, and music
 * tracks. Shows clips, gaps, and metadata for each item.
 */
export function TimelineView() {
  const [timeline, setTimeline] = useState<TimelineStatus | null>(null);

  // Poll AG-UI /timeline endpoint
  const fetchTimeline = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/agui/timeline`);
      const data = await res.json();
      if (data.timeline) {
        setTimeline(data.timeline as TimelineStatus);
      }
    } catch {
      // ignore fetch errors
    }
  }, []);

  useEffect(() => {
    fetchTimeline();
    const interval = setInterval(fetchTimeline, 5000);
    return () => clearInterval(interval);
  }, [fetchTimeline]);

  if (!timeline) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-xl mb-2">No Timeline Yet</div>
          <div className="text-pipeline-muted">
            The timeline will appear after the scenario director creates the
            track structure
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold text-pipeline-accent">
        {timeline.timeline_name}
      </h2>

      {timeline.tracks.map((track) => (
        <TrackRow key={track.name} track={track} />
      ))}
    </div>
  );
}

function TrackRow({ track }: { track: TimelineTrack }) {
  const trackColors: Record<string, string> = {
    V1_Video: "border-blue-500",
    A1_Narration: "border-green-500",
    A2_Music: "border-purple-500",
  };

  return (
    <div className="bg-pipeline-card rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <div
            className={`w-3 h-3 rounded-full ${
              trackColors[track.name]
                ? trackColors[track.name].replace("border-", "bg-")
                : "bg-gray-500"
            }`}
          />
          <h3 className="font-semibold">{track.name}</h3>
        </div>
        <span className="text-sm text-pipeline-muted">
          {track.total_clips} clips, {track.total_gaps} gaps
        </span>
      </div>

      <div className="flex gap-1 overflow-x-auto">
        {track.clips.map((clip, idx) => (
          <div
            key={idx}
            className="min-w-24 p-2 bg-pipeline-bg rounded text-xs"
            style={{ flex: `${Math.max(clip.duration, 1)} 0 0` }}
          >
            <div className="font-medium truncate">{clip.name}</div>
            <div className="text-pipeline-muted">{clip.duration.toFixed(1)}s</div>
          </div>
        ))}
        {track.gaps.map((gap, idx) => (
          <div
            key={`gap-${idx}`}
            className="min-w-12 p-2 bg-red-900/20 border border-red-800 rounded text-xs"
          >
            <div className="text-red-400 truncate">{gap.name}</div>
          </div>
        ))}
        {track.total_clips === 0 && track.total_gaps === 0 && (
          <div className="text-pipeline-muted text-sm">Empty track</div>
        )}
      </div>
    </div>
  );
}
