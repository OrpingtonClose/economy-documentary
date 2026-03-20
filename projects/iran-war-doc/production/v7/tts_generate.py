#!/usr/bin/env python3
"""
WAR ECONOMY — Qwen3-TTS Narration Generator
=============================================
Generates narration audio for all 26 scenes using Qwen3-TTS VoiceDesign model.
Three distinct narrator voices:
  V1 - Financial Journalist: sharp, confident, data-driven male
  V2 - Intelligence/Economic Analyst: precise, measured, connecting-the-dots male  
  V3 - Historian: authoritative, contemplative, long-view male

Usage:
  python3 tts_generate.py --script narration_script.json --output-dir /workspace/audio
"""

import argparse
import json
import os
import re
import time
import logging
import torch
import soundfile as sf

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# Voice design instructions for each narrator
VOICE_DESIGNS = {
    "V1": {
        "name": "V1_Financial_Journalist",
        "instruct": (
            "Male, early 40s, baritone voice. Speaks like a seasoned financial journalist "
            "delivering a hard-hitting documentary. Sharp, confident, data-driven but human. "
            "Measured pace with emphasis on key numbers. Slight gravitas. "
            "Think of a PBS Frontline narrator — authoritative yet accessible. "
            "Clear enunciation, moderate pace around 145 words per minute."
        ),
    },
    "V2": {
        "name": "V2_Intelligence_Analyst",
        "instruct": (
            "Male, late 30s, slightly deeper voice than V1. Speaks like an intelligence analyst "
            "briefing senior officials — precise, methodical, connecting dots others miss. "
            "Calm and controlled, with subtle intensity when revealing key findings. "
            "Slightly faster pace than V1, around 155 words per minute. "
            "Think of a serious investigative reporter. Clinical but compelling."
        ),
    },
    "V3": {
        "name": "V3_Historian",
        "instruct": (
            "Male, early 50s, warm tenor-baritone. Speaks like a distinguished historian — "
            "contemplative, authoritative, taking the long view. Draws on centuries of context. "
            "Slightly slower, more deliberate pace around 140 words per minute. "
            "Think of Ken Burns documentary narration — thoughtful pauses, "
            "weight on historical parallels. Measured gravitas."
        ),
    },
}


def parse_narration_segments(narration_text):
    """Parse narration text into segments by voice.
    
    Format: V1 (Financial Journalist): "text..." or V1: "text..."
    Returns list of (voice_id, text) tuples.
    """
    segments = []
    # Pattern: V1/V2/V3 optionally followed by (description): then quoted or unquoted text
    pattern = r'(V[123])\s*(?:\([^)]*\))?\s*:\s*"?(.*?)(?:"|(?=\nV[123]\s*(?:\([^)]*\))?\s*:)|\Z)'
    
    matches = list(re.finditer(pattern, narration_text, re.DOTALL))
    
    if not matches:
        # If no voice markers, treat entire text as V1
        clean = narration_text.strip().strip('"')
        if clean:
            segments.append(("V1", clean))
        return segments
    
    for m in matches:
        voice = m.group(1)
        text = m.group(2).strip().strip('"').strip()
        # Clean up any remaining markdown
        text = re.sub(r'\s+', ' ', text)
        if text:
            segments.append((voice, text))
    
    return segments


def generate_audio(model, voice_id, text, output_path, max_retries=2):
    """Generate audio for a single segment using VoiceDesign."""
    voice_cfg = VOICE_DESIGNS[voice_id]
    
    for attempt in range(max_retries + 1):
        try:
            # Estimate max tokens: ~12 tokens/sec of audio, ~2.5 words/sec speech
            # So for N words, expect ~N/2.5 seconds, ~N/2.5*12 = ~4.8*N tokens
            # Add 50% margin, minimum 2048
            est_tokens = max(2048, int(len(text.split()) * 7.2))
            wavs, sr = model.generate_voice_design(
                text=text,
                language="English",
                instruct=voice_cfg["instruct"],
                max_new_tokens=est_tokens,
            )
            sf.write(output_path, wavs[0], sr)
            duration = len(wavs[0]) / sr
            file_size = os.path.getsize(output_path)
            return duration, file_size
        except Exception as e:
            if attempt < max_retries:
                log.warning(f"  Attempt {attempt+1} failed: {e}, retrying...")
                torch.cuda.empty_cache()
                time.sleep(2)
            else:
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True, help="Path to narration_script.json")
    parser.add_argument("--output-dir", required=True, help="Output directory for audio files")
    parser.add_argument("--start-scene", type=int, default=0, help="Scene number to start from (skip earlier)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Load narration script
    with open(args.script) as f:
        scenes = json.load(f)

    log.info("=" * 60)
    log.info("WAR ECONOMY — Qwen3-TTS Narration Generator")
    log.info("=" * 60)
    log.info(f"Scenes: {len(scenes)} | Output: {args.output_dir}")

    # Load model
    log.info("Loading Qwen3-TTS VoiceDesign model...")
    t0 = time.time()
    from qwen_tts import Qwen3TTSModel
    
    model = Qwen3TTSModel.from_pretrained(
        "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="flash_attention_2",
    )
    log.info(f"Model loaded in {time.time()-t0:.1f}s")

    # Generate audio for each scene
    total_duration = 0
    total_segments = 0
    results = []

    for scene_data in scenes:
        scene_num = scene_data["scene_number"]
        scene_title = scene_data["scene_title"]
        
        if scene_num < args.start_scene:
            log.info(f"[Scene {scene_num}] Skipping (before start-scene)")
            continue

        scene_dir = os.path.join(args.output_dir, f"scene_{scene_num:02d}")
        os.makedirs(scene_dir, exist_ok=True)
        
        # Check if scene already completed
        manifest_path = os.path.join(scene_dir, "manifest.json")
        if os.path.exists(manifest_path):
            log.info(f"[Scene {scene_num}] {scene_title} — already complete, skipping")
            with open(manifest_path) as f:
                mf = json.load(f)
            total_duration += mf.get("total_duration", 0)
            total_segments += mf.get("segment_count", 0)
            results.append(mf)
            continue

        log.info(f"\n{'='*60}")
        log.info(f"[Scene {scene_num}/{len(scenes)}] {scene_title}")
        log.info(f"{'='*60}")

        segments = parse_narration_segments(scene_data["narration_text"])
        log.info(f"  Segments: {len(segments)} | Words: {scene_data['word_count']}")

        scene_segments = []
        scene_duration = 0
        scene_start = time.time()

        for seg_idx, (voice_id, text) in enumerate(segments):
            seg_filename = f"seg_{seg_idx:02d}_{voice_id}.wav"
            seg_path = os.path.join(scene_dir, seg_filename)

            if os.path.exists(seg_path):
                log.info(f"  [{seg_idx+1}/{len(segments)}] {voice_id} — already exists, skipping")
                info = sf.info(seg_path)
                dur = info.duration
            else:
                word_count = len(text.split())
                log.info(f"  [{seg_idx+1}/{len(segments)}] {voice_id} | {word_count} words")
                
                try:
                    dur, fsize = generate_audio(model, voice_id, text, seg_path)
                    log.info(f"    ✓ {dur:.1f}s audio, {fsize/1024:.0f}KB")
                except Exception as e:
                    log.error(f"    ✗ Failed: {e}")
                    scene_segments.append({
                        "index": seg_idx,
                        "voice": voice_id,
                        "file": seg_filename,
                        "status": "failed",
                        "error": str(e),
                    })
                    continue

            scene_duration += dur
            total_segments += 1
            scene_segments.append({
                "index": seg_idx,
                "voice": voice_id,
                "file": seg_filename,
                "text": text[:100] + "..." if len(text) > 100 else text,
                "duration_sec": round(dur, 2),
                "status": "complete",
            })

        total_duration += scene_duration
        elapsed = time.time() - scene_start
        
        scene_manifest = {
            "scene_number": scene_num,
            "scene_title": scene_title,
            "segment_count": len(scene_segments),
            "total_duration": round(scene_duration, 2),
            "generation_time": round(elapsed, 1),
            "segments": scene_segments,
        }
        
        with open(manifest_path, 'w') as f:
            json.dump(scene_manifest, f, indent=2)
        
        results.append(scene_manifest)
        log.info(f"  ✓ Scene {scene_num} complete: {scene_duration:.1f}s audio in {elapsed:.1f}s")

    # Save overall manifest
    overall = {
        "total_scenes": len(results),
        "total_segments": total_segments,
        "total_duration_sec": round(total_duration, 2),
        "total_duration_min": round(total_duration / 60, 1),
        "scenes": results,
    }
    
    overall_path = os.path.join(args.output_dir, "narration_manifest.json")
    with open(overall_path, 'w') as f:
        json.dump(overall, f, indent=2)

    log.info(f"\n{'='*60}")
    log.info(f"COMPLETE: {len(results)} scenes, {total_segments} segments")
    log.info(f"Total audio: {total_duration/60:.1f} minutes")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
