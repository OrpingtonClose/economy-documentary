import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

/**
 * AG-UI proxy: forwards CopilotKit requests to the FastAPI backend.
 *
 * Handles the CopilotKit "info" method locally (runtime discovery)
 * and proxies all other requests (agent runs) to the AG-UI backend.
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.text();

    // CopilotKit sends {method: "info"} to discover available agents.
    // The AG-UI ADK endpoint only handles RunAgentInput, so we
    // intercept the info request and return agent metadata directly.
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(body);
    } catch {
      // not JSON — fall through to proxy
    }

    const method = parsed.method as string | undefined;

    // --- info: runtime discovery ---
    if (method === "info") {
      return NextResponse.json({
        agents: {
          default: {
            description:
              "ADHD-friendly AI documentary pipeline — generates scenario, audio, visuals, and assembles the final film.",
          },
        },
      });
    }

    // --- agent/connect: session setup (no actual agent run) ---
    // Return a minimal SSE stream so CopilotKit considers the connection
    // established without triggering a real pipeline run on the backend.
    if (method === "agent/connect") {
      const innerBody = (parsed.body ?? {}) as Record<string, unknown>;
      const threadId = innerBody.threadId ?? "unknown";
      const runId = innerBody.runId ?? "unknown";

      const ssePayload = [
        `data: ${JSON.stringify({ type: "RUN_STARTED", threadId, runId })}\n\n`,
        `data: ${JSON.stringify({ type: "RUN_FINISHED", threadId, runId })}\n\n`,
      ].join("");

      return new NextResponse(ssePayload, {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }

    // --- agent/run (and anything else): forward to AG-UI backend ---
    // Unwrap the single-endpoint envelope before forwarding.
    const forwardBody =
      parsed.body && typeof parsed.body === "object"
        ? JSON.stringify(parsed.body)
        : body;

    const response = await fetch(BACKEND_URL, {
      method: "POST",
      headers: {
        "Content-Type": request.headers.get("Content-Type") || "application/json",
        Accept: "text/event-stream",
      },
      body: forwardBody,
    });

    // Stream the SSE response back to the client
    if (response.body) {
      return new NextResponse(response.body, {
        status: response.status,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }

    return NextResponse.json(
      { error: "No response body from backend" },
      { status: 502 }
    );
  } catch (error) {
    console.error("AG-UI proxy error:", error);
    return NextResponse.json(
      { error: "Failed to connect to backend" },
      { status: 502 }
    );
  }
}
