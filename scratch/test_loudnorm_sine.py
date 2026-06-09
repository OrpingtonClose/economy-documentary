import os
import subprocess
import numpy as np
import math

def main():
    os.makedirs("/tmp/test_sine", exist_ok=True)
    concat_txt = "/tmp/test_sine/concat.txt"
    concat_wav = "/tmp/test_sine/concat.wav"
    norm_wav = "/tmp/test_sine/norm.wav"
    
    # Generate 30 placeholders of 3.0 seconds each
    clips = []
    for i in range(30):
        p = f"/tmp/test_sine/clip_{i}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "3.0", p],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        clips.append(p)
        
    with open(concat_txt, "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
            
    # Concat
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", concat_wav],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    
    # Normalize
    print("Normalizing concatenated WAV...")
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", concat_wav, "-af", "loudnorm=I=-16:TP=-1.0:LRA=11", norm_wav],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    print(f"Loudnorm exit code: {res.returncode}")
    print(f"Loudnorm stderr: {res.stderr.decode('utf-8')[-200:]}")
    
    # Measure LUFS of norm_wav
    raw_path = "/tmp/test_sine/raw.pcm"
    subprocess.run(
        ["ffmpeg", "-y", "-i", norm_wav, "-vn", "-f", "s16le", "-ac", "1", "-ar", "44100", raw_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    with open(raw_path, "rb") as f:
        raw = f.read()
    print(f"Normalized WAV raw PCM size: {len(raw)} bytes")
    if len(raw) > 0:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(np.square(pcm, dtype=np.float64)))
        print(f"RMS: {rms:.6f}")
        if rms > 0:
            lufs = 20.0 * math.log10(rms) + (-3.0)
            print(f"LUFS: {lufs:.2f} LUFS")
        else:
            print("RMS is zero!")
    else:
        print("Raw size is zero!")

if __name__ == "__main__":
    main()
