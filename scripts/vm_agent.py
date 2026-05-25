#!/usr/bin/env python3
"""VM Agent — pydantic-deep agent running on GPU workers.

- Receives context from media agents via HTTP POST
- Has one tool: bash_command
- Uses deepseek-v4-flash
- Persistent memory across requests
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

from pydantic_ai import Agent


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEEPSEEK_API_KEY = ""
if os.path.exists("/workspace/.deepseek_key"):
    with open("/workspace/.deepseek_key") as f:
        _DEEPSEEK_API_KEY = f.read().strip()

_WORKER_MODE = "both"
_CONTEXT = ""


# ---------------------------------------------------------------------------
# Bash tool
# ---------------------------------------------------------------------------

async def bash_command(command: str) -> str:
    """Run a bash command and return the output."""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip()
    if stderr:
        output += "\n[stderr]:\n" + stderr.decode().strip()
    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"
    return output


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

_AGENT_PROMPT = f"""You are a GPU worker agent on a Vast.ai VM.

MODE: {_WORKER_MODE}

You have ONE tool: bash_command. Use it to run media generation scripts.

AVAILABLE SCRIPTS:
- Qwen3-TTS: python repo/scripts/run_qwen3_tts.py --text "..." --voice V1 --output /workspace/out.wav
- LTX-2.3: /workspace/ltx-2.3-repo/.venv/bin/python repo/scripts/run_ltx_2_3.py --prompt "..." --duration 5 --output /workspace/out.mp4

RULES:
- ALWAYS verify the output file exists after generation (ls -la <path>)
- Report the exact output file path
- If generation fails, report the full error
- Keep responses concise

CONTEXT FROM MEDIA AGENT:
{_CONTEXT}
"""

_agent = Agent(
    "openai:deepseek-v4-flash",
    system_prompt=_AGENT_PROMPT,
)

# Register bash tool
_agent.tool_plain(bash_command)


# ---------------------------------------------------------------------------
# Self-destruct monitor (background bash)
# ---------------------------------------------------------------------------

def _start_self_destruct() -> None:
    script = """#!/bin/bash
INSTANCE_ID="${VAST_INSTANCE_ID:-${INSTANCE_ID:-}}"
API_KEY="${VAST_API_KEY:-${VAST_AI_KEY:-}}"
if [ -z "$INSTANCE_ID" ] || [ -z "$API_KEY" ]; then
  echo "self-destruct: missing vars" >> /workspace/self_destruct.log
  exit 1
fi
while true; do
  sleep 60
  if [ -f /workspace/.vm_activity ]; then
    LAST=$(cat /workspace/.vm_activity)
    NOW=$(date +%s)
    IDLE=$((NOW - LAST))
    if [ "$IDLE" -gt 3600 ]; then
      echo "$(date): idle ${IDLE}s > 3600s. Destroying $INSTANCE_ID" >> /workspace/self_destruct.log
      vastai --api-key "$API_KEY" destroy instance "$INSTANCE_ID" >> /workspace/self_destruct.log 2>&1
      exit 0
    fi
  fi
done
"""
    path = "/workspace/self_destruct.sh"
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    threading.Thread(
        target=lambda: subprocess.run(["/bin/bash", path]),
        name="self-destruct",
        daemon=True,
    ).start()


def _touch_activity() -> None:
    try:
        with open("/workspace/.vm_activity", "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import Response
    import uvicorn
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    sys.exit(1)

app = FastAPI(title="VM Agent")


@app.get("/")
def health() -> Response:
    _touch_activity()
    return Response(content="ok", media_type="text/plain")


@app.post("/")
async def handle(request: Request) -> Response:
    """Receive free-text instruction + optional context, run agent, return result."""
    _touch_activity()

    body = await request.body()
    text = body.decode("utf-8").strip()
    if not text:
        return Response(content="error: empty instruction", media_type="text/plain", status_code=400)

    # Parse optional context prefix
    instruction = text
    if text.startswith("CONTEXT:"):
        parts = text.split("\n\n", 1)
        if len(parts) == 2:
            instruction = parts[1]

    try:
        result = await _agent.run(instruction)
        return Response(content=result.output, media_type="text/plain")
    except Exception as exc:
        return Response(content=f"error: {exc}", media_type="text/plain", status_code=500)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="VM Agent")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    _touch_activity()
    _start_self_destruct()
    print(f"VM Agent starting on {args.host}:{args.port} (mode={_WORKER_MODE})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
