"use client";

/**
 * CopilotKit sidebar for the playground.
 *
 * Renders a CopilotKit chat sidebar that communicates with the
 * Strands agent via the AG-UI protocol. The sidebar appears on
 * the right side of the workbench and allows the user to:
 *
 * - Ask questions about the pipeline state
 * - Trigger component runs via natural language
 * - Get narrated explanations of evaluation results
 *
 * The sidebar is optional — the main workbench (component list,
 * run stream, pipeline orchestrator) works without it. The
 * CopilotProvider must be mounted above this component.
 */

import { CopilotSidebar } from "@copilotkit/react-ui";
import "@copilotkit/react-ui/styles.css";

export function PlaygroundCopilotSidebar() {
  return (
    <CopilotSidebar
      labels={{
        title: "Pipeline Assistant",
        initial: "Ask about the pipeline, trigger a run, or inspect evaluation results.",
      }}
      defaultOpen={false}
    />
  );
}
