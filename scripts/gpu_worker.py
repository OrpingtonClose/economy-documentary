#!/usr/bin/env python3
"""GPU Worker — thin HTTP wrapper around bash commands.

No model loading, no generation logic, no monitoring.
POST / receives text, runs bash to execute standalone scripts, returns result.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("gpu_worker")

app = FastAPI(title="GPU Worker")


# ---------------------------------------------------------------------------
# Bash execution
# ---------------------------------------------------------------------------

def bash_command(command: str) -> dict:
    """Execute a bash command. Returns dict with stdout, stderr, returncode."""
    logger.info("bash: %s", command[:200])
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": -1}


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

@app.get("/")
def health() -> Response:
    _touch_activity()
    return Response(content="ok", media_type="text/plain")


@app.post("/")
async def handle(request: Request) -> Response:
    """Receive instruction text, execute via bash, return raw result."""
    _touch_activity()

    body = await request.body()
    text = body.decode("utf-8").strip()
    if not text:
        return Response(content="error: empty text", media_type="text/plain", status_code=400)

    logger.info("Received instruction (%d chars)", len(text))

    # Parse instruction for known patterns
    # Audio: look for --text and --voice
    # Video: look for --prompt and --duration
    import re

    text_match = re.search(r'--text\s+"([^"]+)"', text)
    voice_match = re.search(r'--voice\s+(\w+)', text)
    prompt_match = re.search(r'--prompt\s+"([^"]+)"', text)
    duration_match = re.search(r'--duration\s+(\d+\.?\d*)', text)
    output_match = re.search(r'--output\s+(\S+)', text)

    if text_match and voice_match:
        # Audio generation
        t = text_match.group(1)
        v = voice_match.group(1)
        out = output_match.group(1) if output_match else f"/workspace/output/audio_{int(time.time())}.wav"
        cmd = f"python /workspace/scripts/run_qwen3_tts.py --text '{t}' --voice {v} --output {out}"
        result = bash_command(cmd)
        if result["returncode"] == 0:
            return Response(content=f"OK: {out}\n{result['stdout']}", media_type="text/plain")
        return Response(content=f"ERROR:\n{result['stderr']}", media_type="text/plain", status_code=500)

    elif prompt_match:
        # Video generation
        p = prompt_match.group(1)
        d = duration_match.group(1) if duration_match else "5"
        out = output_match.group(1) if output_match else f"/workspace/output/video_{int(time.time())}.mp4"
        cmd = f"python /workspace/scripts/run_ltx_2_3.py --prompt '{p}' --duration {d} --output {out}"
        result = bash_command(cmd)
        if result["returncode"] == 0:
            return Response(content=f"OK: {out}\n{result['stdout']}", media_type="text/plain")
        return Response(content=f"ERROR:\n{result['stderr']}", media_type="text/plain", status_code=500)

    # Fallback: execute as raw bash
    result = bash_command(text)
    status = 200 if result["returncode"] == 0 else 500
    return Response(
        content=f"exit={result['returncode']}\n{result['stdout']}\n{result['stderr']}",
        media_type="text/plain",
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="GPU Worker")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    _touch_activity()
    _start_self_destruct()
    logger.info("Starting GPU Worker on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
