#!/usr/bin/env python3
"""
Qwen3-TTS narration generator for all 42 scenes.
Generates 3 voice tracks per scene (V1=Curious Challenger, V2=Patient Explainer, V3=Encouraging Guide)
then concatenates them into per-scene audio files.

Usage: python3 generate_tts.py --scenes /workspace/scenes_parsed.json --output-dir /workspace/tts_output
"""

import argparse
import json
import os
import time
import subprocess
import numpy as np
import soundfile as sf
import torch

def get_voice_instructions():
    """Return voice design instructions for each narrator role."""
    return {
        "V1": "A young, curious male voice with a slightly informal, conversational tone. "
              "He sounds like someone in their late 20s who is genuinely puzzled and wants to understand. "
              "Slightly fast-paced, energetic, with rising intonation when asking questions. "
              "American English accent.",
        "V2": "A calm, authoritative male voice in his 50s. Deep, measured, patient. "
              "He speaks slowly and clearly like a seasoned economics professor explaining something complex "
              "to someone he respects. Warm but serious. Precise diction. "
              "Slight pauses between key concepts for emphasis. British English accent.",
        "V3": "A warm, encouraging female voice in her 40s. She sounds like a thoughtful guide "
              "who believes in the listener's ability to understand. Gentle, reassuring, with a slight smile "
              "in her voice. Medium pace, smooth delivery. "
              "American English accent."
    }

def generate_scene_audio(model, scene, output_dir, voice_instructions):
    """Generate TTS for all voice blocks in a scene, concatenate into one file."""
    scene_num = scene["scene_num"]
    scene_dir = os.path.join(output_dir, f"scene_{scene_num:02d}")
    os.makedirs(scene_dir, exist_ok=True)
    
    voice_blocks = scene.get("voice_blocks", {})
    audio_parts = []
    
    for voice_key in ["V1", "V2", "V3"]:
        if voice_key not in voice_blocks:
            continue
        
        block = voice_blocks[voice_key]
        text = block["text"]
        
        if not text.strip():
            continue
        
        voice_file = os.path.join(scene_dir, f"{voice_key}.wav")
        
        # Check if already generated
        if os.path.exists(voice_file):
            print(f"  Scene {scene_num} {voice_key}: already exists, skipping")
            audio_parts.append(voice_file)
            continue
        
        instruct = voice_instructions[voice_key]
        
        print(f"  Scene {scene_num} {voice_key} ({block['role']}): {len(text)} chars...")
        
        # Split long texts into chunks of ~4000 chars at sentence boundaries
        chunks = split_text(text, max_chars=4000)
        chunk_wavs = []
        
        for i, chunk in enumerate(chunks):
            try:
                wavs, sr = model.generate_voice_design(
                    text=chunk,
                    instruct=instruct,
                    language="English",
                    non_streaming_mode=True,
                    do_sample=True,
                    top_k=50,
                    top_p=0.9,
                    temperature=0.7,
                    repetition_penalty=1.1,
                )
                chunk_wavs.append((wavs[0], sr))
                print(f"    Chunk {i+1}/{len(chunks)}: {len(wavs[0])/sr:.1f}s")
            except Exception as e:
                print(f"    ERROR on chunk {i+1}: {e}")
                # Generate a short silence as fallback
                sr = 24000
                silence = np.zeros(int(sr * 0.5), dtype=np.float32)
                chunk_wavs.append((silence, sr))
        
        # Concatenate chunks with 0.3s silence between them
        if chunk_wavs:
            sr = chunk_wavs[0][1]
            gap = np.zeros(int(sr * 0.3), dtype=np.float32)
            combined = []
            for j, (wav, _) in enumerate(chunk_wavs):
                combined.append(wav)
                if j < len(chunk_wavs) - 1:
                    combined.append(gap)
            full_wav = np.concatenate(combined)
            sf.write(voice_file, full_wav, sr)
            duration = len(full_wav) / sr
            print(f"    => {voice_key}: {duration:.1f}s saved to {voice_file}")
            audio_parts.append(voice_file)
        
        # Free GPU memory between generations
        torch.cuda.empty_cache()
    
    # Concatenate all voice parts with 1.0s silence gap between voices
    if audio_parts:
        scene_file = os.path.join(scene_dir, f"scene_{scene_num:02d}_narration.wav")
        if not os.path.exists(scene_file):
            concat_with_gaps(audio_parts, scene_file, gap_seconds=1.0)
            # Get duration
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", scene_file],
                capture_output=True, text=True
            )
            dur = float(result.stdout.strip()) if result.stdout.strip() else 0
            print(f"  => Scene {scene_num} narration: {dur:.1f}s total")
            
            # Write metadata
            meta = {
                "scene_num": scene_num,
                "title": scene.get("title", ""),
                "duration_seconds": dur,
                "voices": list(voice_blocks.keys()),
                "voice_instructions": {k: voice_instructions[k] for k in voice_blocks.keys()},
            }
            with open(os.path.join(scene_dir, "metadata.json"), "w") as f:
                json.dump(meta, f, indent=2)
        else:
            print(f"  Scene {scene_num} narration: already exists")
    
    return scene_dir

def split_text(text, max_chars=4000):
    """Split text into chunks at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]
    
    sentences = []
    current = ""
    for char in text:
        current += char
        if char in '.!?' and len(current) > 50:
            sentences.append(current)
            current = ""
    if current.strip():
        sentences.append(current)
    
    chunks = []
    current_chunk = ""
    for sent in sentences:
        if len(current_chunk) + len(sent) > max_chars and current_chunk:
            chunks.append(current_chunk.strip())
            current_chunk = sent
        else:
            current_chunk += sent
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    
    return chunks if chunks else [text]

def concat_with_gaps(wav_files, output_file, gap_seconds=1.0):
    """Concatenate WAV files with silence gaps using ffmpeg."""
    # Build ffmpeg filter
    inputs = []
    filter_parts = []
    
    for i, f in enumerate(wav_files):
        inputs.extend(["-i", f])
    
    # Create filter: add silence gaps between files
    n = len(wav_files)
    if n == 1:
        subprocess.run(["cp", wav_files[0], output_file], check=True)
        return
    
    # Use ffmpeg concat with silence
    filter_str = ""
    for i in range(n):
        filter_str += f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=24000:channel_layouts=mono[a{i}];"
    
    # Add silence between each pair
    concat_inputs = ""
    for i in range(n):
        concat_inputs += f"[a{i}]"
        if i < n - 1:
            # Add silence
            filter_str += f"aevalsrc=0:d={gap_seconds}:s=24000:c=mono[s{i}];"
            concat_inputs += f"[s{i}]"
    
    total_streams = n + (n - 1)  # audio files + silence gaps
    filter_str += f"{concat_inputs}concat=n={total_streams}:v=0:a=1[out]"
    
    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_str, "-map", "[out]", output_file]
    subprocess.run(cmd, check=True, capture_output=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenes", required=True, help="Path to scenes_parsed.json")
    parser.add_argument("--output-dir", required=True, help="Output directory for TTS files")
    parser.add_argument("--model-path", default="/workspace/models/qwen-tts-voicedesign", help="Qwen3-TTS model path")
    parser.add_argument("--start-scene", type=int, default=1, help="Start from this scene number")
    parser.add_argument("--end-scene", type=int, default=42, help="End at this scene number")
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Load scenes
    with open(args.scenes) as f:
        scenes = json.load(f)
    
    print(f"Loaded {len(scenes)} scenes")
    print(f"Processing scenes {args.start_scene} to {args.end_scene}")
    
    # Load model on GPU with BF16
    print(f"Loading Qwen3-TTS VoiceDesign from {args.model_path} (GPU, BF16)...")
    import torch
    from qwen_tts import Qwen3TTSModel
    model = Qwen3TTSModel.from_pretrained(
        args.model_path,
        device_map="cuda:0",
        torch_dtype=torch.bfloat16,
    )
    print(f"Model loaded on {next(model.model.parameters()).device}!")
    
    voice_instructions = get_voice_instructions()
    
    # Process scenes
    for scene in scenes:
        scene_num = scene["scene_num"]
        if scene_num < args.start_scene or scene_num > args.end_scene:
            continue
        
        print(f"\n{'='*60}")
        print(f"Scene {scene_num}: {scene.get('title', 'Untitled')}")
        print(f"{'='*60}")
        
        start = time.time()
        generate_scene_audio(model, scene, args.output_dir, voice_instructions)
        elapsed = time.time() - start
        print(f"  Time: {elapsed:.1f}s")
    
    print("\n\nDone! All TTS generated.")

if __name__ == "__main__":
    main()
