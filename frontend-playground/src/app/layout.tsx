import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { CopilotProvider } from "./copilot/CopilotProvider";
import { PlaygroundCopilotSidebar } from "./copilot/PlaygroundCopilotSidebar";

export const metadata: Metadata = {
  title: "Documentary Playground",
  description:
    "Workbench for the 15 atomic components of the documentary pipeline. Each component exposes its cases, declared models, evaluator stack, and a live run surface.",
};

interface RootLayoutProps {
  readonly children: ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="en" className="bg-pg-bg text-pg-text">
      <body className="min-h-screen bg-pg-bg text-pg-text antialiased">
        <CopilotProvider>
          {children}
          <PlaygroundCopilotSidebar />
        </CopilotProvider>
      </body>
    </html>
  );
}
