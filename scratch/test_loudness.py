import os
import subprocess
import tempfile
import math
import numpy as np

def measure_lufs_integrated(audio_path: str) -> float:
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_pcm_path = os.path.join(tmpdir, "raw.pcm")
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-vn", "-f", "s16le", "-ac", "1", "-ar", "44100", raw_pcm_path],
            capture_output=True, check=True
        )
        with open(raw_pcm_path, "rb") as f:
            raw = f.read()
            
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(np.square(pcm, dtype=np.float64)))
    print(f"DEBUG: pcm size = {len(pcm)}, rms = {rms}")
    if rms <= 0.0:
        return -70.0
    return 20.0 * math.log10(rms) + (-3.0)

def test():
    # 1. Generate 90s audio
    audio_path = "/tmp/debug_audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "90.0", audio_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Generated raw audio size: {os.path.getsize(audio_path)}")

    # 2. Normalize
    norm_path = "/tmp/debug_norm.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-af", "loudnorm=I=-16:TP=-1.0:LRA=11", norm_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Normalized audio size: {os.path.getsize(norm_path)}")

    # 3. Generate 90s video (320x240)
    video_path = "/tmp/debug_video.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=90.0", "-c:v", "libx264", "-pix_fmt", "yuv420p", video_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Video size: {os.path.getsize(video_path)}")

    # 4. Mux
    out_path = "/tmp/debug_muxed.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-i", norm_path, "-c:v", "copy", "-c:a", "aac", "-shortest", out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Muxed video size: {os.path.getsize(out_path)}")

    # 5. Extract
    ext_audio_path = "/tmp/debug_extracted.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", out_path, "-vn", "-acodec", "pcm_s16le", "-ac", "1", ext_audio_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Extracted audio size: {os.path.getsize(ext_audio_path)}")

    # 6. Measure
    lufs = measure_lufs_integrated(ext_audio_path)
    print(f"LUFS: {lufs}")

if __name__ == "__main__":
    test()
