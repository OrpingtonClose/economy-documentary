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
    # 1. Generate 30 audio clips of 3.0s duration each
    audio_clips = []
    video_clips = []
    for i in range(1, 31):
        a_path = f"/tmp/debug_audio_{i}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "3.0", a_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        audio_clips.append(a_path)

        v_path = f"/tmp/debug_video_{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3.0", "-c:v", "libx264", "-pix_fmt", "yuv420p", v_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        video_clips.append(v_path)

    # 2. Concat video clips
    final_video_path = "/tmp/debug_final_video.mp4"
    concat_video_file = "/tmp/debug_concat_video.txt"
    with open(concat_video_file, "w") as f:
        for c in video_clips:
            f.write(f"file '{c}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_video_file, "-c", "copy", final_video_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Concat video size: {os.path.getsize(final_video_path)}")

    # 3. Concat audio clips
    final_audio_path = "/tmp/debug_final_audio.wav"
    concat_audio_file = "/tmp/debug_concat_audio.txt"
    with open(concat_audio_file, "w") as f:
        for c in audio_clips:
            f.write(f"file '{c}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_audio_file, "-c", "copy", final_audio_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Concat audio size: {os.path.getsize(final_audio_path)}")

    # 4. Normalize
    normalized_audio_path = "/tmp/debug_normalized_audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", final_audio_path, "-af", "loudnorm=I=-16:TP=-1.0:LRA=11", normalized_audio_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Normalized audio size: {os.path.getsize(normalized_audio_path)}")

    # 5. Mux
    output_path = "/tmp/debug_final_documentary.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", final_video_path, "-i", normalized_audio_path, "-c:v", "copy", "-c:a", "aac", "-shortest", output_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Muxed movie size: {os.path.getsize(output_path)}")

    # 6. Extract
    extracted_audio_wav = "/tmp/debug_extracted_normalized_audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", output_path, "-vn", "-acodec", "pcm_s16le", "-ac", "1", extracted_audio_wav],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    print(f"Extracted audio size: {os.path.getsize(extracted_audio_wav)}")

    # 7. Measure
    lufs = measure_lufs_integrated(extracted_audio_wav)
    print(f"LUFS: {lufs}")

if __name__ == "__main__":
    test()
