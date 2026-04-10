"use client";

import type { PipelineEvent } from "@/lib/types";

/**
 * Generic tool call renderer for the event stream.
 */
export function ToolCallCard({ event }: { event: PipelineEvent }) {
  const typeColors: Record<string, string> = {
    phase_start: "border-l-green-500",
    phase_end: "border-l-blue-500",
    tool_start: "border-l-orange-500",
    tool_end: "border-l-purple-500",
    force_end: "border-l-red-500",
  };

  const typeIcons: Record<string, string> = {
    phase_start: ">>",
    phase_end: "<<",
    tool_start: "->",
    tool_end: "<-",
    force_end: "!!",
  };

  return (
    <div
      className={`border-l-4 ${
        typeColors[event.type] || "border-l-gray-500"
      } bg-pipeline-bg rounded-r px-3 py-1 text-xs font-mono`}
    >
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
      </span>
    </div>
  );
}
