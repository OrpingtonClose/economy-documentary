import os
import subprocess
import numpy as np

def main():
    os.makedirs("/tmp/test_concat", exist_ok=True)
    p1 = "/tmp/test_concat/A1:1:s1_b1.wav"
    p2 = "/tmp/test_concat/A1:1:s1_b2.wav"
    concat_txt = "/tmp/test_concat/concat.txt"
    out_wav = "/tmp/test_concat/out.wav"
    
    # Generate two sine wave placeholders with colons in their names
    print("Generating placeholders...")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "3.0", p1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "3.0", p2],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    
    # Write to concat file
    with open(concat_txt, "w") as f:
        f.write(f"file '{p1}'\n")
        f.write(f"file '{p2}'\n")
        
    print("Running ffmpeg concat...")
    res = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", out_wav],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    print(f"FFmpeg exit code: {res.returncode}")
    print(f"FFmpeg stderr:\n{res.stderr.decode('utf-8')}")
    
    # Check output file size and content
    if os.path.exists(out_wav):
        size = os.path.getsize(out_wav)
        print(f"Output size: {size} bytes")
        # Measure size of raw PCM
        raw_path = "/tmp/test_concat/raw.pcm"
        subprocess.run(
            ["ffmpeg", "-y", "-i", out_wav, "-vn", "-f", "s16le", "-ac", "1", "-ar", "44100", raw_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        with open(raw_path, "rb") as f:
            raw = f.read()
        print(f"Raw PCM size: {len(raw)} bytes")
    else:
        print("Output file does not exist!")

if __name__ == "__main__":
    main()
