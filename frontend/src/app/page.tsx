"use client";

import dynamic from "next/dynamic";
import { CopilotChat } from "@copilotkit/react-ui";
import { useState } from "react";
import type { PipelinePhase } from "@/lib/types";
import { useRunSession } from "@/lib/run-session";
import { RunReconnectBanner } from "@/components/run-reconnect-banner";

// Dynamic imports with ssr:false to prevent hydration issues
// that strip onClick handlers from buttons inside map loops
const PipelineDashboard = dynamic(
  () => import("@/components/pipeline-dashboard").then((m) => m.PipelineDashboard),
  { ssr: false }
);
const ScenarioEditor = dynamic(
  () => import("@/components/scenario-editor").then((m) => m.ScenarioEditor),
  { ssr: false }
);
const PromptReviewer = dynamic(
  () => import("@/components/prompt-reviewer").then((m) => m.PromptReviewer),
  { ssr: false }
);
const ClipReviewer = dynamic(
  () => import("@/components/clip-reviewer").then((m) => m.ClipReviewer),
  { ssr: false }
);
const TimelineView = dynamic(
  () => import("@/components/timeline-view").then((m) => m.TimelineView),
  { ssr: false }
);
const OtioTimeline = dynamic(
  () => import("@/components/otio-timeline").then((m) => m.OtioTimeline),
  { ssr: false }
);
const QADashboard = dynamic(
  () => import("@/components/qa-dashboard").then((m) => m.QADashboard),
  { ssr: false }
);
const ReasoningTracePanel = dynamic(
  () => import("@/components/reasoning-trace").then((m) => m.ReasoningTracePanel),
  { ssr: false }
);
const DashboardIntervention = dynamic(
  () =>
    import("@/components/dashboard-intervention").then(
      (m) => m.DashboardIntervention
    ),
  { ssr: false }
);

export default function Home() {
  const [activeTab, setActiveTab] = useState<string>("otio");
  const runSession = useRunSession();

  const tabs = [
    { id: "otio", label: "OTIO Timeline" },
    { id: "dashboard", label: "Pipeline Dashboard" },
    { id: "reasoning", label: "Agent Reasoning" },
    { id: "scenario", label: "Scenario Editor" },
    { id: "prompts", label: "Prompt Reviewer" },
    { id: "clips", label: "Clip Reviewer" },
    { id: "timeline", label: "Timeline (legacy)" },
    { id: "qa", label: "QA Dashboard" },
  ];

  return (
    <main className="flex h-screen">
      {/* Left panel: CopilotKit chat */}
      <div className="w-1/3 border-r border-pipeline-blue flex flex-col">
        <div className="p-4 border-b border-pipeline-blue">
          <h1 className="text-xl font-bold text-pipeline-accent">
            Documentary Pipeline
          </h1>
          <p className="text-sm text-pipeline-muted">
            ADHD-friendly AI documentary generation
          </p>
        </div>
        <div className="flex-1 overflow-hidden">
          <CopilotChat
            labels={{
              title: "Documentary Assistant",
              initial:
                "Enter a topic to start generating your documentary. I'll guide you through script creation, audio generation, visual planning, and final assembly.",
            }}
          />
        </div>
      </div>

      {/* Right panel: tabbed content */}
      <div className="flex-1 flex flex-col">
        {/* UI-07: banner for reconnect / buffer-overflow / stale run-id. */}
        <RunReconnectBanner session={runSession} />

        {/* ARCH-H4 (#159): halt button + directive input, always visible */}
        <DashboardIntervention />

        {/* Tab bar */}
        <div className="flex border-b border-pipeline-blue bg-pipeline-card">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-3 text-sm font-medium transition-colors ${
                activeTab === tab.id
                  ? "text-pipeline-accent border-b-2 border-pipeline-accent"
                  : "text-pipeline-muted hover:text-pipeline-text"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div className="flex-1 overflow-auto p-4">
          {activeTab === "otio" && <OtioTimeline />}
          {activeTab === "dashboard" && <PipelineDashboard />}
          {activeTab === "reasoning" && <ReasoningTracePanel />}
          {activeTab === "scenario" && <ScenarioEditor />}
          {activeTab === "prompts" && <PromptReviewer />}
          {activeTab === "clips" && <ClipReviewer />}
          {activeTab === "timeline" && <TimelineView />}
          {activeTab === "qa" && <QADashboard />}
        </div>
      </div>
    </main>
  );
}
