#!/usr/bin/env python3
import argparse
import io
import math
import os
import re
import struct
import subprocess
import time
import wave
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

class StrictEndpointMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/":
            return PlainTextResponse("Not Found: Only root '/' is permitted", status_code=404)
        if request.method not in ("GET", "POST"):
            return PlainTextResponse("Method Not Allowed: Only GET and POST permitted", status_code=405)
        if request.query_params:
            return PlainTextResponse("Bad Request: Query parameters are prohibited", status_code=400)
        return await call_next(request)

app = FastAPI(title="Mock GPU Worker")
app.add_middleware(StrictEndpointMiddleware)

OUTPUT_DIR = "/tmp/documentary-pipeline/output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def _make_wav(duration_sec: float, freq: float = 440.0, sample_rate: int = 16000) -> bytes:
    """Generate a simple sine-wave WAV file."""
    n_samples = int(duration_sec * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n_samples):
            sample = int(32767 * math.sin(2 * math.pi * freq * i / sample_rate))
            w.writeframes(struct.pack('<h', sample))
    return buf.getvalue()

@app.get("/")
def health() -> Response:
    # Always report bootstrap is complete and both models are ready
    gpu_desc = "The active GPU is RTX 3090 (VRAM: 0.0/24.0GB)."
    boot_desc = "The bootstrap process is fully complete."
    model_desc = "Regarding models, the Qwen3-TTS audio model is loaded and ready and the LTX-2.3 video model is loaded and ready."
    content = f"The GPU worker is currently healthy and active. {gpu_desc} {boot_desc} {model_desc}\n\nHere is the latest snippet from the system logs:\nWorker started successfully."
    return Response(content=content, media_type="text/plain")

@app.post("/")
async def handle(request: Request) -> Response:
    body = await request.body()
    text = body.decode("utf-8").strip()
    if not text:
        return Response(content="error: empty instruction", media_type="text/plain", status_code=400)

    print(f"Mock GPU Worker received: {text}")

    # Parse optional context prefix
    instruction = text
    if text.startswith("CONTEXT:"):
        parts = text.split("\n\n", 1)
        if len(parts) == 2:
            instruction = parts[1]

    # Detect TTS or LTX
    text_match = re.search(r'--text\s+"([^"]+)"', instruction)
    voice_match = re.search(r'--voice\s+(\S+)', instruction)
    prompt_match = re.search(r'--prompt\s+"([^"]+)"', instruction)
    duration_match = re.search(r'--duration\s+(\d+\.?\d*)', instruction)
    output_match = re.search(r'--output\s+(\S+)', instruction)

    # Resolve filename
    output_val = output_match.group(1) if output_match else ""
    filename = os.path.basename(output_val) if output_val else ""

    if text_match:
        # TTS Job
        prompt_text = text_match.group(1)
        voice = voice_match.group(1) if voice_match else "default"
        if not filename:
            filename = f"audio_{int(time.time())}.wav"
        
        # Determine duration from GSA
        duration = 5.0
        try:
            async with httpx.AsyncClient() as client:
                gsa_resp = await client.get("http://127.0.0.1:8000/", timeout=2.0)
                if gsa_resp.status_code == 200:
                    slots = gsa_resp.json().get("otio", {}).get("slots", {})
                    # Find slot with matching text
                    for slot in slots.values():
                        if slot.get("text") == prompt_text or prompt_text in slot.get("text", ""):
                            duration = float(slot.get("scripted_sec") or 5.0)
                            break
        except Exception as e:
            print(f"Error querying GSA: {e}")

        # Generate audio file
        wav_bytes = _make_wav(duration)
        dest_path = os.path.join(OUTPUT_DIR, filename)
        with open(dest_path, "wb") as f:
            f.write(wav_bytes)

        size_str = f"{len(wav_bytes) / 1024 / 1024:.2f} MB"
        result_text = f"RESULT: Generated narration audio. Output: /workspace/output/{filename} ({size_str}, {duration}s)"
        return Response(content=result_text, media_type="text/plain")

    elif prompt_match:
        # Video Job
        prompt = prompt_match.group(1)
        duration = float(duration_match.group(1)) if duration_match else 5.0
        if not filename:
            filename = f"video_{int(time.time())}.mp4"

        dest_path = os.path.join(OUTPUT_DIR, filename)
        # Generate black video using ffmpeg
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=512x320:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", dest_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
            )
        except Exception as e:
            print(f"ffmpeg failed: {e}")
            # Write dummy bytes as fallback
            with open(dest_path, "wb") as f:
                f.write(b"dummy mp4 content")

        size_bytes = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        size_str = f"{size_bytes / 1024 / 1024:.2f} MB"
        result_text = f"RESULT: Generated video clip. Output: /workspace/output/{filename} ({size_str}, {duration}s)"
        return Response(content=result_text, media_type="text/plain")

    else:
        # Generic instruction fallback
        result_text = "RESULT: Command executed successfully."
        return Response(content=result_text, media_type="text/plain")

def main():
    parser = argparse.ArgumentParser(description="Mock GPU Worker")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
