#!/usr/bin/env python3
"""VM Agent — pydantic-deep agent that runs on GPU workers.

One tool: bash_command. Receives free-text instructions from media agents,
executes bash to run Qwen3-TTS or LTX-2.3, returns results.

Self-destruct: background bash loop destroys VM if idle >1 hour.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# DeepSeek setup
# ---------------------------------------------------------------------------

_DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not _DEEPSEEK_API_KEY and os.path.exists("/workspace/.deepseek_key"):
    with open("/workspace/.deepseek_key") as f:
        _DEEPSEEK_API_KEY = f.read().strip()

# ---------------------------------------------------------------------------
# Bash tool
# ---------------------------------------------------------------------------

def bash_command(command: str) -> str:
    """Run a bash command. Returns stdout + stderr."""
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        output = result.stdout.strip()
        if result.stderr.strip():
            output += "\n[stderr]:\n" + result.stderr.strip()
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output
    except Exception as exc:
        return f"bash error: {exc}"


# ---------------------------------------------------------------------------
# Agent prompt
# ---------------------------------------------------------------------------

_VM_AGENT_PROMPT = """You are a GPU worker agent on a Vast.ai VM.

Your job: receive instructions, run bash commands to generate media, report results.

TOOLS:
- bash_command: run any shell command

MEDIA ENGINES:
- Qwen3-TTS: python /workspace/scripts/run_qwen3_tts.py --text "..." --voice V1 --output /workspace/out.wav
- LTX-2.3: python /workspace/scripts/run_ltx_2_3.py --prompt "..." --duration 5 --output /workspace/out.mp4

RULES:
- ALWAYS verify the output file exists after generation
- Report the exact output file path
- If generation fails, report the error
- Keep responses concise — just the facts

SELF-DESTRUCT: If idle for 1 hour, this VM destroys itself. Process requests promptly.
"""


# ---------------------------------------------------------------------------
# FastAPI surface
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import Response
    from openai import OpenAI
    import uvicorn
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    sys.exit(1)

app = FastAPI(title="VM Agent")


_DS_CLIENT: OpenAI | None = None
if _DEEPSEEK_API_KEY:
    _DS_CLIENT = OpenAI(api_key=_DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")


def _touch_activity() -> None:
    """Update heartbeat file for self-destruct monitor."""
    try:
        with open("/workspace/.vm_activity", "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


@app.get("/")
def health() -> Response:
    _touch_activity()
    return Response(content="ok", media_type="text/plain")


@app.post("/")
async def handle(request: Request) -> Response:
    """Receive free-text instructions, run agent with bash tool, return results."""
    _touch_activity()

    body = await request.body()
    instruction = body.decode("utf-8").strip()
    if not instruction:
        return Response(content="error: empty instruction", media_type="text/plain", status_code=400)

    if _DS_CLIENT is None:
        return Response(content="error: no DEEPSEEK_API_KEY", media_type="text/plain", status_code=500)

    # Build conversation
    messages = [
        {"role": "system", "content": _VM_AGENT_PROMPT},
        {"role": "user", "content": instruction},
    ]

    # Run agent loop (simple: single-turn for now, bash commands executed inline)
    # For multi-turn with tool use, we'd need a proper agent framework.
    # For now: agent responds, we execute any bash commands it suggests.
    try:
        resp = _DS_CLIENT.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.3,
        )
        agent_text = resp.choices[0].message.content or ""

        # Execute bash commands mentioned in the response
        # Look for "BASH_COMMAND:" markers
        lines = agent_text.splitlines()
        executed = []
        for i, line in enumerate(lines):
            if line.strip().startswith("BASH_COMMAND:"):
                # Next line(s) until empty or next marker
                cmd_lines = []
                for j in range(i + 1, len(lines)):
                    l = lines[j]
                    if l.strip() == "" or l.strip().startswith("BASH_COMMAND:") or l.strip().startswith("REASON:"):
                        break
                    cmd_lines.append(l)
                cmd = "\n".join(cmd_lines).strip()
                if cmd:
                    result = bash_command(cmd)
                    executed.append(f"$ {cmd}\n{result}")
                    # Append result to messages for next turn
                    messages.append({"role": "assistant", "content": agent_text})
                    messages.append({"role": "user", "content": f"Command result:\n{result}\n\nContinue."})
                    # Get next response
                    resp = _DS_CLIENT.chat.completions.create(
                        model="deepseek-v4-flash",
                        messages=messages,
                        temperature=0.3,
                    )
                    agent_text = resp.choices[0].message.content or ""
                    lines = agent_text.splitlines()
                    break  # Restart scan

        final = agent_text
        if executed:
            final = "\n".join(executed) + "\n\n" + final

        return Response(content=final, media_type="text/plain")

    except Exception as exc:
        return Response(content=f"error: {exc}", media_type="text/plain", status_code=500)


# ---------------------------------------------------------------------------
# Self-destruct monitor (background bash)
# ---------------------------------------------------------------------------

def _start_self_destruct() -> None:
    """Start a background bash loop that destroys this VM if idle >1 hour."""
    script = """#!/bin/bash
INSTANCE_ID="${VAST_INSTANCE_ID:-${INSTANCE_ID:-}}"
API_KEY="${VAST_API_KEY:-${VAST_AI_KEY:-}}"
if [ -z "$INSTANCE_ID" ] || [ -z "$API_KEY" ]; then
  echo "self-destruct: missing INSTANCE_ID or API_KEY" >> /workspace/self_destruct.log
  exit 1
fi
while true; do
  sleep 60
  if [ -f /workspace/.vm_activity ]; then
    LAST=$(cat /workspace/.vm_activity)
    NOW=$(date +%s)
    IDLE=$((NOW - LAST))
    if [ "$IDLE" -gt 3600 ]; then
      echo "$(date): idle ${IDLE}s > 3600s. Destroying VM $INSTANCE_ID" >> /workspace/self_destruct.log
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="VM Agent")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    _touch_activity()
    _start_self_destruct()

    print(f"VM Agent starting on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
