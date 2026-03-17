#!/usr/bin/env python3
"""
Generate V3 narration using Qwen3-TTS with Aiden voice (calm documentary narrator).
Processes narration in chunks to handle the full 2000+ word script.
"""

import torch
import json
import os
import numpy as np
import soundfile as sf
import time

# Config
MODEL_NAME = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
SCRIPT_PATH = "/root/v3_script.json"
OUTPUT_DIR = "/root/qwen_tts_chunks"
FINAL_OUTPUT = "/root/qwen_narration.wav"
SPEAKER = "Aiden"
LANGUAGE = "English"
# Calm, authoritative documentary narrator instruction
INSTRUCT = "Speak in a calm, measured, and authoritative documentary narrator tone. Slow pace, clear enunciation, thoughtful pauses between sentences. Like a high-quality financial documentary."

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Load script
with open(SCRIPT_PATH, 'r') as f:
    script = json.load(f)

# Collect narration segments in order
segments = []
for seg in script['segments']:
    narration = seg.get('narration', '').strip()
    if narration:
        segments.append({
            'id': seg['id'],
            'text': narration
        })

print(f"Total narration segments: {len(segments)}")

# Load model
print("Loading Qwen3-TTS model...")
start = time.time()
from qwen_tts import Qwen3TTSModel

model = Qwen3TTSModel.from_pretrained(
    MODEL_NAME,
    device_map="cuda:0",
    dtype=torch.bfloat16,
    attn_implementation="flash_attention_2"
)
print(f"Model loaded in {time.time()-start:.1f}s")

# Generate each segment
all_wavs = []
sample_rate = None

for i, seg in enumerate(segments):
    seg_id = seg['id']
    text = seg['text']
    chunk_path = os.path.join(OUTPUT_DIR, f"{seg_id}.wav")
    
    # Check if already generated (resume capability)
    if os.path.exists(chunk_path):
        print(f"[{i+1}/{len(segments)}] {seg_id} - already exists, loading...")
        data, sr = sf.read(chunk_path)
        all_wavs.append(data)
        sample_rate = sr
        continue
    
    print(f"[{i+1}/{len(segments)}] Generating {seg_id} ({len(text)} chars)...")
    seg_start = time.time()
    
    try:
        wavs, sr = model.generate_custom_voice(
            text=text,
            language=LANGUAGE,
            speaker=SPEAKER,
            instruct=INSTRUCT
        )
        
        # wavs is a list of tensors or a tensor
        if isinstance(wavs, list):
            wav_data = wavs[0]
        else:
            wav_data = wavs
        
        if isinstance(wav_data, torch.Tensor):
            wav_data = wav_data.cpu().numpy()
        
        # Ensure 1D
        if wav_data.ndim > 1:
            wav_data = wav_data.squeeze()
        
        sample_rate = sr
        duration = len(wav_data) / sr
        
        # Save chunk
        sf.write(chunk_path, wav_data, sr)
        all_wavs.append(wav_data)
        
        elapsed = time.time() - seg_start
        print(f"  OK - {elapsed:.1f}s - duration: {duration:.1f}s - saved to {chunk_path}")
        
    except Exception as e:
        print(f"  ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        # Generate silence as placeholder (2 seconds)
        silence = np.zeros(int(sr * 2.0) if sr else 48000, dtype=np.float32)
        all_wavs.append(silence)
        sf.write(chunk_path, silence, sr or 24000)

# Add short silence between segments (0.8 seconds)
print("\nConcatenating all segments with inter-segment pauses...")
final_parts = []
silence_samples = int((sample_rate or 24000) * 0.8)
silence = np.zeros(silence_samples, dtype=np.float32)

for i, wav in enumerate(all_wavs):
    final_parts.append(wav.astype(np.float32))
    if i < len(all_wavs) - 1:
        final_parts.append(silence)

final_wav = np.concatenate(final_parts)
sf.write(FINAL_OUTPUT, final_wav, sample_rate)

total_duration = len(final_wav) / sample_rate
print(f"\nFinal narration: {FINAL_OUTPUT}")
print(f"Duration: {total_duration:.1f}s ({total_duration/60:.1f} min)")
print(f"Sample rate: {sample_rate}Hz")
print("Done!")
