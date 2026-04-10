"use client";

import { CopilotChat } from "@copilotkit/react-ui";
import { PipelineDashboard } from "@/components/pipeline-dashboard";
import { ScenarioEditor } from "@/components/scenario-editor";
import { PromptReviewer } from "@/components/prompt-reviewer";
import { ClipReviewer } from "@/components/clip-reviewer";
import { TimelineView } from "@/components/timeline-view";
import { QADashboard } from "@/components/qa-dashboard";
import { useState } from "react";
import type { PipelinePhase } from "@/lib/types";

export default function Home() {
  const [activeTab, setActiveTab] = useState<string>("dashboard");

  const tabs = [
    { id: "dashboard", label: "Pipeline Dashboard" },
    { id: "scenario", label: "Scenario Editor" },
    { id: "prompts", label: "Prompt Reviewer" },
    { id: "clips", label: "Clip Reviewer" },
    { id: "timeline", label: "Timeline" },
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
          {activeTab === "dashboard" && <PipelineDashboard />}
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
