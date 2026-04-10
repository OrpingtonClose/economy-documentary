import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

/**
 * AG-UI proxy: forwards CopilotKit requests to the FastAPI backend.
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.text();

    const response = await fetch(BACKEND_URL, {
      method: "POST",
      headers: {
        "Content-Type": request.headers.get("Content-Type") || "application/json",
        Accept: "text/event-stream",
      },
      body,
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
