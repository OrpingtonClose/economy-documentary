#!/usr/bin/env python3
"""Minimal test worker — plain text GET/POST interface."""
import argparse
import io
import math
import struct
import wave

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI()


def _make_wav(duration_sec: float = 1.0, freq: float = 440.0, sample_rate: int = 16000) -> bytes:
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
async def get_endpoint():
    return Response(content="ok test vram=0.0/0.0GB", media_type="text/plain")


@app.post("/")
async def post_endpoint(request: Request):
    text = (await request.body()).decode("utf-8").strip()
    # Return a 1-second sine wave as WAV
    wav_bytes = _make_wav(duration_sec=1.0)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"X-Audio-Duration": "1.000"},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
