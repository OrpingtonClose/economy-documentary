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
import argparse
import uvicorn

from pydantic_ai import Agent


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEEPSEEK_API_KEY = ""
if os.path.exists("/workspace/.deepseek_key"):
    with open("/workspace/.deepseek_key") as f:
        _DEEPSEEK_API_KEY = f.read().strip()
if _DEEPSEEK_API_KEY:
    os.environ["DEEPSEEK_API_KEY"] = _DEEPSEEK_API_KEY
    os.environ["OPENAI_API_KEY"] = _DEEPSEEK_API_KEY

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

_AGENT_PROMPT = f"""You are a GPU worker agent on a Vast.ai VM. You are a deep agent — smart, capable of complex troubleshooting, and able to ask for help when you need it.

MODE: {_WORKER_MODE}

Your job is to generate media artifacts (narration audio or video clips) using the tools available on this VM.

WHAT YOU HAVE ACCESS TO:
- Qwen3-TTS: a text-to-speech model for generating narration audio. Model weights are at /workspace/models/qwen3-tts-voicedesign/ and the runner script is at repo/scripts/run_qwen3_tts.py.
- LTX-2.3: a video generation model for producing documentary clips. The inference environment is at /workspace/ltx-2-repo/ and the runner script is at repo/scripts/run_ltx_2_3.py.
- Model pin manifest: repo/scripts/model_pin.py contains exact model names, revisions, and SHA256 hashes for all required weights. If a model is missing, read this file to find what to download.
- bash_command: your only tool. Use it to inspect the environment, run generation, debug failures, install missing dependencies, and verify outputs.

HOW TO RESPOND:
Use one of these markers at the start of your response:

RESULT:
Use this when you have successfully generated the artifact. Include the exact output file path and a brief summary of what you did.
Example:
RESULT: Generated narration audio. Output: /workspace/output/audio_scene1_V1.wav (2.3 MB, 12.4s)

QUESTION:
Use this when you need clarification, missing information, or additional context before you can proceed.
Example:
QUESTION: The prompt mentions "warm sunset lighting" but the visual notes say "cool blue dawn." Which mood should I target?

ERROR:
Use this when you have tried to troubleshoot but cannot recover. Include the full error and what you already attempted.
Example:
ERROR: LTX-2.3 checkpoint is missing at /workspace/models/ltx23/ltx-2.3-22b-dev.safetensors. I checked the directory and it only contains gemma/. I attempted to re-run the download command but it failed with permission denied.

WORKFLOW:
1. Read the request carefully. If anything is unclear, ASK (use QUESTION:).
2. Check that required models, scripts, and dependencies are present. If missing, try to fix it (download, install, symlink).
3. Generate the artifact. If generation fails, troubleshoot — read logs, check disk space, verify CUDA is available, retry with adjusted parameters.
4. Verify the output file exists and has a reasonable size (not 0 bytes).
5. Report back with RESULT:, QUESTION:, or ERROR:.

You are not a script runner. You are a capable collaborator. Think, troubleshoot, and communicate.

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
# VAST_INSTANCE_ID is set by the Vast.ai platform in every container.
INSTANCE_ID="${VAST_INSTANCE_ID:-}"
# API key is written by the onstart script (avoids env var for secrets).
API_KEY=""
if [ -f /workspace/.vast_api_key ]; then
  API_KEY=$(cat /workspace/.vast_api_key)
fi
if [ -z "$INSTANCE_ID" ] || [ -z "$API_KEY" ]; then
  echo "self-destruct: missing instance id or api key" >> /workspace/self_destruct.log
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
