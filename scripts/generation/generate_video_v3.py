#!/usr/bin/env python3
"""
LTX-2.3 video generation with subprocess-based text encoding.
Text encoding runs in a subprocess to guarantee full GPU memory release.
Then the main process loads only the transformer (44GB) which fits in 80GB.

Usage: python3 generate_video_v3.py --prompts /workspace/scene_prompts.json --output-dir /workspace/video_output \
       --start-scene 1 --end-scene 7
"""

import argparse
import json
import os
import subprocess
import time
import gc
import sys
import tempfile

import torch

os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"

CHECKPOINT = "/workspace/models/ltx23/ltx-2.3-22b-dev.safetensors"
GEMMA_ROOT = "/workspace/models/gemma3"

NEG_PROMPT = (
    "blurry, out of focus, overexposed, low contrast, noise, grainy, flickering, "
    "distorted, deformed, artifacts, text, letters, subtitles, watermark, "
    "cartoonish, CGI, unrealistic, jittery, unnatural, AI artifacts."
)

def cleanup():
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()

def encode_text_subprocess(prompt, neg_prompt, output_path):
    """Run text encoding in a subprocess to guarantee full GPU memory cleanup."""
    cmd = [
        sys.executable, "/workspace/scripts/encode_text.py",
        "--checkpoint", CHECKPOINT,
        "--gemma-root", GEMMA_ROOT,
        "--prompt", prompt,
        "--neg-prompt", neg_prompt,
        "--output", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if "ENCODED_OK" in result.stdout:
        return True
    else:
        print(f"    Text encoding failed: {result.stderr[-500:]}")
        return False

def denoise_and_decode(encoded_path, seed, output_path, height=512, width=768, 
                       num_frames=121, frame_rate=24.0, num_steps=30):
    """Load encoded text, run transformer denoising and decode to video."""
    from ltx_core.components.diffusion_steps import EulerDiffusionStep
    from ltx_core.components.guiders import MultiModalGuiderParams, create_multimodal_guider_factory
    from ltx_core.components.noisers import GaussianNoiser
    from ltx_core.components.schedulers import LTX2Scheduler
    from ltx_core.model.audio_vae import decode_audio as vae_decode_audio
    from ltx_core.model.video_vae import decode_video as vae_decode_video
    from ltx_core.text_encoders.gemma.embeddings_processor import EmbeddingsProcessorOutput
    from ltx_core.types import VideoPixelShape
    from ltx_pipelines.utils import (
        ModelLedger, cleanup_memory, denoise_audio_video, euler_denoising_loop,
        multi_modal_guider_factory_denoising_func, assert_resolution
    )
    from ltx_pipelines.utils.media_io import encode_video
    from ltx_pipelines.utils.types import PipelineComponents
    
    device = torch.device("cuda")
    dtype = torch.bfloat16
    
    assert_resolution(height=height, width=width, is_two_stage=False)
    
    # Load encoded text from file
    data = torch.load(encoded_path, map_location="cuda", weights_only=True)
    
    v_context_p = data["v_context_p"]
    a_context_p = data["a_context_p"]
    v_context_n = data["v_context_n"]
    a_context_n = data["a_context_n"]
    
    del data
    cleanup()
    print(f"      Text loaded: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
    # Load transformer
    ledger = ModelLedger(
        dtype=dtype, device=device,
        checkpoint_path=CHECKPOINT,
        gemma_root_path=GEMMA_ROOT,
        loras=[], quantization=None,
    )
    
    generator = torch.Generator(device=device).manual_seed(seed)
    noiser = GaussianNoiser(generator=generator)
    stepper = EulerDiffusionStep()
    
    output_shape = VideoPixelShape(batch=1, frames=num_frames, width=width, height=height, fps=frame_rate)
    
    video_guider = MultiModalGuiderParams(
        cfg_scale=3.0, stg_scale=1.0, rescale_scale=0.7,
        modality_scale=3.0, skip_step=0, stg_blocks=[28],
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0, stg_scale=1.0, rescale_scale=0.7,
        modality_scale=3.0, skip_step=0, stg_blocks=[28],
    )
    
    transformer = ledger.transformer()
    print(f"      Transformer loaded: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
    sigmas = LTX2Scheduler().execute(steps=num_steps).to(dtype=torch.float32, device=device)
    
    video_guider_factory = create_multimodal_guider_factory(params=video_guider, negative_context=v_context_n)
    audio_guider_factory = create_multimodal_guider_factory(params=audio_guider, negative_context=a_context_n)
    
    components = PipelineComponents(dtype=dtype, device=device)
    
    def denoising_loop(sigmas, video_state, audio_state, stepper):
        return euler_denoising_loop(
            sigmas=sigmas, video_state=video_state, audio_state=audio_state, stepper=stepper,
            denoise_fn=multi_modal_guider_factory_denoising_func(
                video_guider_factory=video_guider_factory,
                audio_guider_factory=audio_guider_factory,
                v_context=v_context_p, a_context=a_context_p,
                transformer=transformer,
            ),
        )
    
    video_state, audio_state = denoise_audio_video(
        output_shape=output_shape,
        conditionings=[],
        noiser=noiser,
        sigmas=sigmas,
        stepper=stepper,
        denoising_loop_fn=denoising_loop,
        components=components,
        dtype=dtype,
        device=device,
    )
    
    torch.cuda.synchronize()
    del transformer
    cleanup()
    print(f"      After denoise: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
    # Decode
    decoded_video = vae_decode_video(video_state.latent, ledger.video_decoder(), generator=generator)
    decoded_audio = vae_decode_audio(audio_state.latent, ledger.audio_decoder(), ledger.vocoder())
    
    encode_video(
        video=decoded_video,
        audio=decoded_audio,
        output_path=output_path,
        fps=int(frame_rate),
        video_chunks_number=1,
    )
    
    del decoded_video, decoded_audio, video_state, audio_state, ledger
    cleanup()

def get_clip_count(narration_duration_sec):
    clip_duration = 5.0
    target = narration_duration_sec + 2.0
    return max(1, int(target / clip_duration) + (1 if target % clip_duration > 1.0 else 0))

def get_narration_duration(tts_dir, scene_num):
    f = os.path.join(tts_dir, f"scene_{scene_num:02d}", f"scene_{scene_num:02d}_narration.wav")
    if os.path.exists(f):
        r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", f],
                          capture_output=True, text=True)
        if r.stdout.strip():
            return float(r.stdout.strip())
    return None

def stitch_clips(clip_paths, output_path):
    list_file = output_path.replace(".mp4", "_list.txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_path],
                  check=True, capture_output=True)
    os.remove(list_file)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tts-dir", default="/workspace/tts_output")
    parser.add_argument("--start-scene", type=int, default=1)
    parser.add_argument("--end-scene", type=int, default=42)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--num-steps", type=int, default=30)
    parser.add_argument("--seed-base", type=int, default=42)
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    with open(args.prompts) as f:
        scene_prompts = json.load(f)
    
    print(f"Loaded {len(scene_prompts)} scenes")
    
    for sp in scene_prompts:
        scene_num = sp["scene_num"]
        if scene_num < args.start_scene or scene_num > args.end_scene:
            continue
        
        scene_dir = os.path.join(args.output_dir, f"scene_{scene_num:02d}")
        os.makedirs(scene_dir, exist_ok=True)
        
        scene_file = os.path.join(scene_dir, f"scene_{scene_num:02d}_video.mp4")
        if os.path.exists(scene_file):
            print(f"Scene {scene_num}: exists, skipping")
            continue
        
        print(f"\n{'='*60}")
        print(f"Scene {scene_num}: {sp.get('title', '')}")
        print(f"{'='*60}")
        
        # Use the pre-computed clip prompts from scene_prompts.json directly.
        # Each scene has unique clips (typically 4-8). During mixing, video will be
        # extended/repeated to match narration length. This avoids generating
        # hundreds of duplicate-prompt clips.
        clips = sp.get("clips", [])
        if not clips:
            clips = [{"prompt": sp["prompt"], "clip_idx": 1}]
        n_clips = len(clips)
        narr_dur = get_narration_duration(args.tts_dir, scene_num)
        print(f"  Generating {n_clips} unique clips (narration: {narr_dur or 'N/A'}s)")
        
        clip_paths = []
        for clip in clips:
            clip_idx = clip.get("clip_idx", len(clip_paths) + 1)
            clip_file = os.path.join(scene_dir, f"clip_{clip_idx:02d}.mp4")
            
            if os.path.exists(clip_file):
                clip_paths.append(clip_file)
                continue
            
            seed = args.seed_base + scene_num * 100 + clip_idx
            prompt = clip["prompt"]
            words = prompt.split()
            if len(words) > 180:
                prompt = " ".join(words[:180]) + "."
            
            print(f"  Clip {clip_idx}/{n_clips} (seed={seed}):")
            start = time.time()
            
            try:
                # Step 1: Encode text in subprocess (frees all GPU memory on exit)
                encoded_file = os.path.join(scene_dir, f"encoded_{clip_idx:02d}.pt")
                print(f"    Encoding text (subprocess)...")
                if not encode_text_subprocess(prompt, NEG_PROMPT, encoded_file):
                    print(f"    FAILED text encoding")
                    continue
                
                # Step 2: Denoise and decode in main process (loads transformer ~44GB)
                print(f"    Denoising and decoding...")
                denoise_and_decode(
                    encoded_path=encoded_file,
                    seed=seed,
                    output_path=clip_file,
                    height=args.height,
                    width=args.width,
                    num_frames=121,
                    frame_rate=24.0,
                    num_steps=args.num_steps,
                )
                
                # Clean up encoded file
                os.remove(encoded_file)
                
                elapsed = time.time() - start
                r = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", clip_file],
                    capture_output=True, text=True
                )
                dur = float(r.stdout.strip()) if r.stdout.strip() else 0
                print(f"    => {dur:.1f}s clip in {elapsed:.0f}s")
                clip_paths.append(clip_file)
                
            except Exception as e:
                print(f"    ERROR: {e}")
                import traceback
                traceback.print_exc()
            
            cleanup()
        
        if len(clip_paths) > 1:
            stitch_clips(clip_paths, scene_file)
        elif len(clip_paths) == 1:
            subprocess.run(["cp", clip_paths[0], scene_file], check=True)
        
        if os.path.exists(scene_file):
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", scene_file],
                capture_output=True, text=True
            )
            total = float(r.stdout.strip()) if r.stdout.strip() else 0
            print(f"  => Scene {scene_num}: {total:.1f}s total")
            
            json.dump({
                "scene_num": scene_num, "title": sp.get("title", ""),
                "n_clips": len(clip_paths), "video_duration": total,
                "narration_duration": narr_dur,
                "model": "LTX-2.3-22B-dev BF16 full",
                "resolution": f"{args.width}x{args.height}", "steps": args.num_steps,
            }, open(os.path.join(scene_dir, "metadata.json"), "w"), indent=2)
    
    print("\nDone!")

if __name__ == "__main__":
    with torch.inference_mode():
        main()
