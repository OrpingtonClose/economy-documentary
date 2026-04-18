"use client";

import { useState } from "react";
import type { PipelineEvent } from "@/lib/types";

/**
 * Expandable tool call / event renderer for the pipeline event stream.
 * Click to expand and see full metadata (agent, duration, result size, timestamp).
 */
export function ToolCallCard({ event }: { event: PipelineEvent }) {
  const [expanded, setExpanded] = useState(false);

  const typeColors: Record<string, string> = {
    phase_start: "border-l-green-500",
    phase_end: "border-l-blue-500",
    tool_start: "border-l-orange-500",
    tool_end: "border-l-purple-500",
    force_end: "border-l-red-500",
    stage_event: "border-l-cyan-500",
  };

  const typeIcons: Record<string, string> = {
    phase_start: ">>",
    phase_end: "<<",
    tool_start: "->",
    tool_end: "<-",
    force_end: "!!",
    stage_event: "~~",
  };

  const hasDetail =
    event.agent || event.duration !== undefined || event.result_chars !== undefined || event.time;

  return (
    <div
      className={`border-l-4 ${
        typeColors[event.type] || "border-l-gray-500"
      } bg-pipeline-bg rounded-r px-3 py-1 text-xs font-mono ${hasDetail ? "cursor-pointer hover:bg-gray-800/50" : ""}`}
      onClick={() => hasDetail && setExpanded(!expanded)}
    >
      <div className="flex items-center justify-between">
        <div>
          <span className="text-pipeline-muted mr-2">
            {typeIcons[event.type] || "  "}
          </span>
          <span className="text-pipeline-text">
            {event.type === "phase_start" && `Phase started: ${event.name}`}
            {event.type === "phase_end" &&
              `Phase ended: ${event.name} (${event.status})`}
            {event.type === "tool_start" &&
              `Tool call: ${event.tool} (${event.agent})`}
            {event.type === "tool_end" &&
              `Tool done: ${event.tool} (${event.duration?.toFixed(1)}s, ${event.result_chars} chars)`}
            {event.type === "force_end" && "Context window limit reached"}
            {event.type === "stage_event" && (
              <span className={
                (event as any).status === "error" ? "text-red-400" :
                (event as any).status === "recovered" ? "text-yellow-400" :
                (event as any).status === "clip_done" ? "text-green-400" :
                "text-cyan-400"
              }>
                {(event as any).detail || `${(event as any).stage}: ${(event as any).status}`}
              </span>
            )}
          </span>
        </div>
        {hasDetail && (
          <span className="text-pipeline-muted ml-2">
            {expanded ? "▼" : "▶"}
          </span>
        )}
      </div>

      {expanded && (
        <div className="mt-1 pl-6 border-t border-gray-700 pt-1 space-y-0.5 text-pipeline-muted">
          {event.agent && <div>Agent: <span className="text-pipeline-text">{event.agent}</span></div>}
          {event.tool && <div>Tool: <span className="text-pipeline-text">{event.tool}</span></div>}
          {event.name && <div>Name: <span className="text-pipeline-text">{event.name}</span></div>}
          {event.status && <div>Status: <span className="text-pipeline-text">{event.status}</span></div>}
          {event.duration !== undefined && (
            <div>Duration: <span className="text-pipeline-text">{event.duration.toFixed(2)}s</span></div>
          )}
          {event.result_chars !== undefined && (
            <div>Result size: <span className="text-pipeline-text">{event.result_chars.toLocaleString()} chars</span></div>
          )}
          {event.time > 0 && (
            <div>Timestamp: <span className="text-pipeline-text">{new Date(event.time * 1000).toLocaleTimeString()}</span></div>
          )}
        </div>
      )}
    </div>
  );
}
