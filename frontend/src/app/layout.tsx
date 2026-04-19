"use client";

import { CopilotKit } from "@copilotkit/react-core";
import "@copilotkit/react-ui/styles.css";
import "./globals.css";
import { Toaster } from "sonner";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-pipeline-bg text-pipeline-text min-h-screen">
        <CopilotKit runtimeUrl="/api/copilotkit">
          {children}
        </CopilotKit>
        <Toaster richColors position="top-right" />
      </body>
    </html>
  );
}
