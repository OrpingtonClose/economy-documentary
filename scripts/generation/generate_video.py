#!/usr/bin/env python3
"""
LTX-2.3 video generation script for documentary scenes.
Uses TI2VidOneStagePipeline with full BF16 model (no distillation, no FP8, no upscalers).

Each scene is broken into clips based on narration duration.
Clips are ~5s each (121 frames at 24fps), then stitched per-scene.

Usage: python3 generate_video.py --prompts /workspace/scene_prompts.json --output-dir /workspace/video_output \
       --start-scene 1 --end-scene 7
"""

import argparse
import json
import os
import time
import subprocess
import random
import torch

def get_clip_count(narration_duration_sec):
    """Calculate number of ~5s clips needed to cover narration duration + small buffer."""
    clip_duration = 5.0  # 121 frames at 24fps
    # Add 2s buffer so video is slightly longer than audio
    target = narration_duration_sec + 2.0
    n_clips = max(1, int(target / clip_duration) + (1 if target % clip_duration > 1.0 else 0))
    return n_clips

def generate_clip(pipeline, prompt, seed, output_path, height=512, width=768, num_frames=121, frame_rate=24.0, num_steps=30):
    """Generate a single video clip using LTX-2.3 one-stage pipeline."""
    from ltx_core.components.guiders import MultiModalGuiderParams
    
    video_guider = MultiModalGuiderParams(
        cfg_scale=3.0,
        stg_scale=1.0,
        rescale_scale=0.7,
        modality_scale=3.0,
        skip_step=0,
        stg_blocks=[28],
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0,
        stg_scale=1.0,
        rescale_scale=0.7,
        modality_scale=3.0,
        skip_step=0,
        stg_blocks=[28],
    )
    
    negative_prompt = (
        "blurry, out of focus, overexposed, underexposed, low contrast, washed out colors, "
        "excessive noise, grainy texture, poor lighting, flickering, motion blur, distorted proportions, "
        "unnatural skin tones, deformed facial features, extra limbs, disfigured hands, "
        "artifacts around text, inconsistent perspective, camera shake, "
        "cartoonish rendering, 3D CGI look, unrealistic materials, uncanny valley effect, "
        "jittery movement, awkward pauses, incorrect timing, unnatural transitions, "
        "cinematic oversaturation, stylized filters, AI artifacts, letters, text on screen, subtitles, watermark."
    )
    
    decoded_video, decoded_audio = pipeline(
        prompt=prompt,
        negative_prompt=negative_prompt,
        seed=seed,
        height=height,
        width=width,
        num_frames=num_frames,
        frame_rate=frame_rate,
        num_inference_steps=num_steps,
        video_guider_params=video_guider,
        audio_guider_params=audio_guider,
        images=[],
        enhance_prompt=False,
    )
    
    # Encode and save
    from ltx_pipelines.utils.media_io import encode_video
    
    encode_video(
        video=decoded_video,
        audio=decoded_audio,
        output_path=output_path,
        fps=int(frame_rate),
        video_chunks_number=1,
    )
    
    return output_path

def stitch_clips(clip_paths, output_path):
    """Concatenate clips into a single video using ffmpeg."""
    list_file = output_path.replace(".mp4", "_list.txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    os.remove(list_file)
    return output_path

def get_narration_duration(tts_dir, scene_num):
    """Get the duration of the narration audio for a scene."""
    narration_file = os.path.join(tts_dir, f"scene_{scene_num:02d}", f"scene_{scene_num:02d}_narration.wav")
    if os.path.exists(narration_file):
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", narration_file],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            return float(result.stdout.strip())
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", required=True, help="Path to scene_prompts.json")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--tts-dir", default="/workspace/tts_output", help="TTS output directory to read durations")
    parser.add_argument("--checkpoint", default="/workspace/models/ltx23/ltx-2.3-22b-dev.safetensors")
    parser.add_argument("--gemma-root", default="/workspace/models/gemma3")
    parser.add_argument("--start-scene", type=int, default=1)
    parser.add_argument("--end-scene", type=int, default=42)
    parser.add_argument("--height", type=int, default=512, help="Video height (divisible by 64)")
    parser.add_argument("--width", type=int, default=768, help="Video width (divisible by 64)")
    parser.add_argument("--num-steps", type=int, default=30, help="Denoising steps")
    parser.add_argument("--seed-base", type=int, default=42)
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Load prompts
    with open(args.prompts) as f:
        scene_prompts = json.load(f)
    
    print(f"Loaded prompts for {len(scene_prompts)} scenes")
    
    # Initialize pipeline with PYTORCH_CUDA_ALLOC_CONF for better memory management
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    
    print("Loading LTX-2.3 pipeline (BF16, full model, no quantization)...")
    from ltx_pipelines import TI2VidOneStagePipeline
    
    pipeline = TI2VidOneStagePipeline(
        checkpoint_path=args.checkpoint,
        gemma_root=args.gemma_root,
        loras=[],
        quantization=None,
    )
    print("Pipeline loaded!")
    
    # Process scenes
    for sp in scene_prompts:
        scene_num = sp["scene_num"]
        if scene_num < args.start_scene or scene_num > args.end_scene:
            continue
        
        scene_dir = os.path.join(args.output_dir, f"scene_{scene_num:02d}")
        os.makedirs(scene_dir, exist_ok=True)
        
        scene_file = os.path.join(scene_dir, f"scene_{scene_num:02d}_video.mp4")
        if os.path.exists(scene_file):
            print(f"Scene {scene_num}: already exists, skipping")
            continue
        
        print(f"\n{'='*60}")
        print(f"Scene {scene_num}: {sp.get('title', '')}")
        print(f"{'='*60}")
        
        # Get narration duration to determine clip count
        narr_dur = get_narration_duration(args.tts_dir, scene_num)
        if narr_dur:
            n_clips = get_clip_count(narr_dur)
            print(f"  Narration: {narr_dur:.1f}s => {n_clips} clips needed")
        else:
            # Estimate from scene duration_sec
            est_dur = sp.get("duration_sec", 60)
            n_clips = get_clip_count(est_dur)
            print(f"  No narration found, estimating from scenario: {est_dur}s => {n_clips} clips")
        
        clips = sp.get("clips", [])
        if not clips:
            # Use the single prompt for all clips
            clips = [{"prompt": sp["prompt"], "clip_idx": i+1} for i in range(n_clips)]
        
        # Ensure we have enough clips
        while len(clips) < n_clips:
            clips.append(clips[-1].copy())
            clips[-1]["clip_idx"] = len(clips)
        
        clip_paths = []
        for clip in clips[:n_clips]:
            clip_idx = clip.get("clip_idx", len(clip_paths) + 1)
            clip_file = os.path.join(scene_dir, f"clip_{clip_idx:02d}.mp4")
            
            if os.path.exists(clip_file):
                print(f"  Clip {clip_idx}: already exists")
                clip_paths.append(clip_file)
                continue
            
            seed = args.seed_base + scene_num * 100 + clip_idx
            prompt = clip["prompt"]
            # Truncate to ~200 words to stay within token limits
            words = prompt.split()
            if len(words) > 200:
                prompt = " ".join(words[:200]) + "."
            
            print(f"  Clip {clip_idx}/{n_clips}: generating (seed={seed})...")
            print(f"    Prompt: {prompt[:120]}...")
            
            start = time.time()
            try:
                generate_clip(
                    pipeline=pipeline,
                    prompt=prompt,
                    seed=seed,
                    output_path=clip_file,
                    height=args.height,
                    width=args.width,
                    num_frames=121,
                    frame_rate=24.0,
                    num_steps=args.num_steps,
                )
                elapsed = time.time() - start
                
                # Verify output
                result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", clip_file],
                    capture_output=True, text=True
                )
                clip_dur = float(result.stdout.strip()) if result.stdout.strip() else 0
                print(f"    => {clip_dur:.1f}s clip in {elapsed:.1f}s")
                clip_paths.append(clip_file)
                
            except Exception as e:
                print(f"    ERROR: {e}")
                import traceback
                traceback.print_exc()
            
            torch.cuda.empty_cache()
        
        # Stitch clips for this scene
        if len(clip_paths) > 1:
            print(f"  Stitching {len(clip_paths)} clips...")
            stitch_clips(clip_paths, scene_file)
        elif len(clip_paths) == 1:
            subprocess.run(["cp", clip_paths[0], scene_file], check=True)
        
        if os.path.exists(scene_file):
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", scene_file],
                capture_output=True, text=True
            )
            total_dur = float(result.stdout.strip()) if result.stdout.strip() else 0
            print(f"  => Scene {scene_num} video: {total_dur:.1f}s total")
            
            # Save scene metadata
            meta = {
                "scene_num": scene_num,
                "title": sp.get("title", ""),
                "n_clips": len(clip_paths),
                "video_duration": total_dur,
                "narration_duration": narr_dur,
                "height": args.height,
                "width": args.width,
                "model": "LTX-2.3-22B-dev (BF16, full quality)",
                "steps": args.num_steps,
                "pipeline": "TI2VidOneStagePipeline",
            }
            with open(os.path.join(scene_dir, "metadata.json"), "w") as f:
                json.dump(meta, f, indent=2)
    
    print("\n\nDone! All video clips generated.")

if __name__ == "__main__":
    main()
