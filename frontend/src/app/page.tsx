"use client";

import dynamic from "next/dynamic";
import { CopilotChat } from "@copilotkit/react-ui";
import { useState } from "react";
import { useRunSession } from "@/lib/run-session";
import { RunReconnectBanner } from "@/components/run-reconnect-banner";
import { NarratorAssistantMessage } from "@/components/narrator-assistant-message";
import { BriefUserMessage } from "@/components/brief-user-message";
import { ChatErrorMessage } from "@/components/chat-error-message";
import { ChatHeartbeat } from "@/components/chat-heartbeat";
import { ProgressStrip } from "@/components/progress-strip";
import { RestatedBriefCard } from "@/components/restated-brief-card";

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
const PreviewChips = dynamic(
  () => import("@/components/preview-chips").then((m) => m.PreviewChips),
  { ssr: false }
);
const SceneDrilldown = dynamic(
  () =>
    import("@/components/scene-drilldown").then((m) => m.SceneDrilldown),
  { ssr: false }
);
const IntentBar = dynamic(
  () => import("@/components/intent-bar").then((m) => m.IntentBar),
  { ssr: false }
);

// UX-07 (#249): the dashboard used to surface eight sibling tabs
// (OTIO, Pipeline, Reasoning, Scenario, Prompts, Clips, Timeline legacy,
// QA) which buried the primary "watch your film being built" surface
// under engineering diagnostics.  We now split the right-hand rail into:
//
//   - a default **Film** tab backed by the OTIO timeline, always visible;
//   - an **Advanced** disclosure that collapses the remaining debug
//     panels into a single secondary tab group.  The legacy "Timeline
//     (legacy)" view was removed entirely — it predates the OTIO
//     centrepiece and is no longer referenced.
const ADVANCED_TABS = [
  { id: "dashboard", label: "Pipeline Dashboard" },
  { id: "reasoning", label: "Agent Reasoning" },
  { id: "scenario", label: "Scenario Editor" },
  { id: "prompts", label: "Prompt Reviewer" },
  { id: "clips", label: "Clip Reviewer" },
  { id: "qa", label: "QA Dashboard" },
] as const;

type AdvancedTabId = (typeof ADVANCED_TABS)[number]["id"];
type PrimaryTab = "film" | "advanced";

export default function Home() {
  const [primaryTab, setPrimaryTab] = useState<PrimaryTab>("film");
  const [advancedTab, setAdvancedTab] = useState<AdvancedTabId>("dashboard");
  const runSession = useRunSession();

  return (
    <main className="flex h-screen">
      {/* DESIGN-01 (#253): the chat is the primary status surface, so
        * it takes the widest fixed column on the page. We pin a minimum
        * width so the chat is never narrower than the debug panels in
        * the Advanced tab (which run on a `flex-1` basis). */}
      <div className="w-2/5 min-w-[420px] border-r border-pipeline-blue flex flex-col">
        <div className="p-4 border-b border-pipeline-blue">
          <h1 className="text-xl font-bold text-pipeline-accent">
            Documentary Pipeline
          </h1>
          <p className="text-sm text-pipeline-muted">
            ADHD-friendly AI documentary generation
          </p>
        </div>
        <PreviewChips />
        <div className="flex-1 overflow-hidden">
          <CopilotChat
            labels={{
              title: "Documentary Assistant",
              initial:
                "Enter a topic to start generating your documentary. I'll guide you through script creation, audio generation, visual planning, and final assembly.",
            }}
            AssistantMessage={NarratorAssistantMessage}
            UserMessage={BriefUserMessage}
            ErrorMessage={ChatErrorMessage}
          />
        </div>
        {/* UX-03 (#245): immediate ack + ≤60s heartbeat during active
          * stages. Rendered inside the chat column so the aliveness
          * signal sits with the conversation log. */}
        <ChatHeartbeat />
      </div>

      {/* Right panel: tabbed content */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* UI-07: banner for reconnect / buffer-overflow / stale run-id. */}
        <RunReconnectBanner session={runSession} />

        {/* UX-08 (#250): top-of-rail progress ribbon placeholder. The
          * real ribbon (7 stage dots + stage label + ETA) is DESIGN-02
          * (#254); this is copy-level only so the primary dashboard has
          * a consistent "where are we" affordance in the meantime. */}
        <ProgressStrip />

        {/* ARCH-H4 (#159): halt button + directive input, always visible */}
        <DashboardIntervention />

        {/* UX-07 (#249): primary Film / Advanced disclosure. */}
        <div className="flex items-center gap-1 border-b border-pipeline-blue bg-pipeline-card px-2">
          <button
            type="button"
            onClick={() => setPrimaryTab("film")}
            className={`px-4 py-3 text-sm font-medium transition-colors ${
              primaryTab === "film"
                ? "text-pipeline-accent border-b-2 border-pipeline-accent"
                : "text-pipeline-muted hover:text-pipeline-text"
            }`}
            data-testid="primary-tab-film"
          >
            Film
          </button>
          <button
            type="button"
            onClick={() => setPrimaryTab("advanced")}
            className={`px-4 py-3 text-sm font-medium transition-colors ${
              primaryTab === "advanced"
                ? "text-pipeline-accent border-b-2 border-pipeline-accent"
                : "text-pipeline-muted hover:text-pipeline-text"
            }`}
            data-testid="primary-tab-advanced"
            title="For developers — pipeline internals, prompts, raw QA"
          >
            For developers
          </button>
        </div>

        {primaryTab === "advanced" && (
          <div className="flex flex-wrap gap-1 border-b border-pipeline-blue/60 bg-pipeline-bg px-2 py-1">
            {ADVANCED_TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setAdvancedTab(tab.id)}
                className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                  advancedTab === tab.id
                    ? "bg-pipeline-accent/20 text-pipeline-accent"
                    : "text-pipeline-muted hover:text-pipeline-text"
                }`}
                data-testid={`advanced-tab-${tab.id}`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        )}

        {/* Tab content */}
        <div className="flex-1 overflow-auto p-4">
          {/* DESIGN-03 (#255): restated-brief card always sits above
            * the primary timeline so the user can see — at a glance —
            * what the machine understood them to be asking for. */}
          {primaryTab === "film" && (
            <RestatedBriefCard className="mb-4" />
          )}
          {primaryTab === "film" && <OtioTimeline />}
          {primaryTab === "advanced" && advancedTab === "dashboard" && <PipelineDashboard />}
          {primaryTab === "advanced" && advancedTab === "reasoning" && <ReasoningTracePanel />}
          {primaryTab === "advanced" && advancedTab === "scenario" && <ScenarioEditor />}
          {primaryTab === "advanced" && advancedTab === "prompts" && <PromptReviewer />}
          {primaryTab === "advanced" && advancedTab === "clips" && <ClipReviewer />}
          {primaryTab === "advanced" && advancedTab === "qa" && <QADashboard />}
        </div>

        {/* DESIGN-06 (#258): persistent intent bar pinned at the bottom
          * of the main content column. Replaces the natural-language
          * input that used to live in <DashboardIntervention />. */}
        <IntentBar />

        {/* DESIGN-05 (#257): scene drilldown Sheet opens whenever a
          * slot / scene is selected from the OTIO timeline. Rendered at
          * the page root so its overlay covers the entire right column. */}
        <SceneDrilldown />
      </div>
    </main>
  );
}
