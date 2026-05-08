"use client";

/**
 * CopilotKit provider for the playground.
 *
 * Wraps the app with CopilotKit's context and points the runtime
 * at the AG-UI endpoint on the backend. The playground's Strands
 * agents are exposed as "co-agents" that CopilotKit can drive.
 *
 * Usage: wrap your layout or page with <CopilotProvider>.
 */

import { CopilotKit } from "@copilotkit/react-core";
import type { ReactNode } from "react";

/** Backend AG-UI endpoint — CopilotKit's runtime proxies to this. */
const AGUI_RUNTIME_URL = "/agui";

interface CopilotProviderProps {
  readonly children: ReactNode;
}

export function CopilotProvider({ children }: CopilotProviderProps) {
  return (
    <CopilotKit runtimeUrl={AGUI_RUNTIME_URL} agent="strands">
      {children}
    </CopilotKit>
  );
}
