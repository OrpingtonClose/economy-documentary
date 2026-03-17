#!/usr/bin/env python3
"""
LTX-2.3 video generation with explicit memory management.
Manually sequences: text encode → cleanup → VAE encode → cleanup → denoise → cleanup → decode.
This avoids the OOM issue where text encoder memory isn't fully freed.

Usage: python3 generate_video_v2.py --prompts /workspace/scene_prompts.json --output-dir /workspace/video_output \
       --start-scene 29 --end-scene 35
"""

import argparse
import json
import os
import time
import subprocess
import gc
import torch

os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"

def cleanup():
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()

def encode_text_on_gpu(gemma_root, checkpoint_path, prompts):
    """Load text encoder, encode prompts, move results to CPU, completely free GPU."""
    from ltx_pipelines.utils import ModelLedger, cleanup_memory
    
    # Create a temporary ledger just for text encoding
    ledger = ModelLedger(
        dtype=torch.bfloat16,
        device=torch.device("cuda"),
        checkpoint_path=checkpoint_path,
        gemma_root_path=gemma_root,
        loras=[],
        quantization=None,
    )
    
    # Load text encoder
    text_encoder = ledger.text_encoder()
    
    # Encode prompts
    raw_outputs = []
    for p in prompts:
        hs, mask = text_encoder.encode(p)
        # Move to CPU immediately
        raw_outputs.append((hs.cpu(), mask.cpu()))
    
    # Free text encoder completely
    torch.cuda.synchronize()
    del text_encoder
    del ledger
    cleanup()
    
    print(f"  After text cleanup: {torch.cuda.memory_allocated()/1e9:.1f} GB VRAM")
    
    # Now load ONLY the embeddings processor (much smaller)
    # We need to create a new ledger
    ledger2 = ModelLedger(
        dtype=torch.bfloat16,
        device=torch.device("cuda"),
        checkpoint_path=checkpoint_path,
        gemma_root_path=gemma_root,
        loras=[],
        quantization=None,
    )
    embeddings_processor = ledger2.gemma_embeddings_processor()
    
    results = []
    for hs, mask in raw_outputs:
        result = embeddings_processor.process_hidden_states(hs.cuda(), mask.cuda())
        # Move result to CPU
        results.append(result.to_cpu() if hasattr(result, 'to_cpu') else result)
    
    del embeddings_processor
    del ledger2
    cleanup()
    
    return results

def generate_clip_manual(checkpoint_path, gemma_root, prompt, neg_prompt, seed, output_path,
                         height=512, width=768, num_frames=121, frame_rate=24.0, num_steps=30):
    """Generate a single clip with manual memory management."""
    from ltx_core.components.diffusion_steps import EulerDiffusionStep
    from ltx_core.components.guiders import MultiModalGuiderParams, create_multimodal_guider_factory
    from ltx_core.components.noisers import GaussianNoiser
    from ltx_core.components.schedulers import LTX2Scheduler
    from ltx_core.model.audio_vae import decode_audio as vae_decode_audio
    from ltx_core.model.video_vae import decode_video as vae_decode_video
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
    
    # Step 1: Encode text (loads 24GB Gemma, encodes, frees)
    print(f"    Step 1: Encoding text...")
    from ltx_pipelines.utils import ModelLedger as ML2
    from ltx_core.text_encoders.gemma.embeddings_processor import EmbeddingsProcessorOutput
    
    ledger = ML2(
        dtype=dtype, device=device,
        checkpoint_path=checkpoint_path,
        gemma_root_path=gemma_root,
        loras=[], quantization=None,
    )
    
    text_encoder = ledger.text_encoder()
    raw_p = text_encoder.encode(prompt)
    raw_n = text_encoder.encode(neg_prompt)
    torch.cuda.synchronize()
    del text_encoder
    cleanup()
    print(f"      After text encode cleanup: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
    embeddings_processor = ledger.gemma_embeddings_processor()
    ctx_p = embeddings_processor.process_hidden_states(*raw_p)
    ctx_n = embeddings_processor.process_hidden_states(*raw_n)
    del embeddings_processor
    cleanup()
    
    v_context_p, a_context_p = ctx_p.video_encoding, ctx_p.audio_encoding
    v_context_n, a_context_n = ctx_n.video_encoding, ctx_n.audio_encoding
    
    print(f"      After embeddings cleanup: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
    # Step 2: Load transformer and denoise
    print(f"    Step 2: Loading transformer and denoising...")
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
    print(f"      After transformer load: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
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
    print(f"      After denoise cleanup: {torch.cuda.memory_allocated()/1e9:.1f} GB")
    
    # Step 3: Decode video and audio
    print(f"    Step 3: Decoding video and audio...")
    decoded_video = vae_decode_video(video_state.latent, ledger.video_decoder(), generator=generator)
    decoded_audio = vae_decode_audio(audio_state.latent, ledger.audio_decoder(), ledger.vocoder())
    
    # Step 4: Encode to file
    encode_video(
        video=decoded_video,
        audio=decoded_audio,
        output_path=output_path,
        fps=int(frame_rate),
        video_chunks_number=1,
    )
    
    # Full cleanup
    del decoded_video, decoded_audio, video_state, audio_state
    del ledger
    cleanup()
    
    return output_path

def get_clip_count(narration_duration_sec):
    clip_duration = 5.0
    target = narration_duration_sec + 2.0
    return max(1, int(target / clip_duration) + (1 if target % clip_duration > 1.0 else 0))

def get_narration_duration(tts_dir, scene_num):
    narration_file = os.path.join(tts_dir, f"scene_{scene_num:02d}", f"scene_{scene_num:02d}_narration.wav")
    if os.path.exists(narration_file):
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", narration_file],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            return float(result.stdout.strip())
    return None

def stitch_clips(clip_paths, output_path):
    list_file = output_path.replace(".mp4", "_list.txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_path],
                   check=True, capture_output=True)
    os.remove(list_file)

NEG_PROMPT = (
    "blurry, out of focus, overexposed, low contrast, noise, grainy, flickering, "
    "distorted, deformed, artifacts, text, letters, subtitles, watermark, "
    "cartoonish, CGI, unrealistic, jittery, unnatural, AI artifacts."
)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tts-dir", default="/workspace/tts_output")
    parser.add_argument("--checkpoint", default="/workspace/models/ltx23/ltx-2.3-22b-dev.safetensors")
    parser.add_argument("--gemma-root", default="/workspace/models/gemma3")
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
    
    print(f"Loaded prompts for {len(scene_prompts)} scenes")
    
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
        
        narr_dur = get_narration_duration(args.tts_dir, scene_num)
        if narr_dur:
            n_clips = get_clip_count(narr_dur)
            print(f"  Narration: {narr_dur:.1f}s => {n_clips} clips needed")
        else:
            est_dur = sp.get("duration_sec", 60)
            n_clips = get_clip_count(est_dur)
            print(f"  Estimated: {est_dur}s => {n_clips} clips")
        
        clips = sp.get("clips", [])
        if not clips:
            clips = [{"prompt": sp["prompt"], "clip_idx": i+1} for i in range(n_clips)]
        
        while len(clips) < n_clips:
            clips.append(clips[-1].copy())
            clips[-1]["clip_idx"] = len(clips)
        
        clip_paths = []
        for clip in clips[:n_clips]:
            clip_idx = clip.get("clip_idx", len(clip_paths) + 1)
            clip_file = os.path.join(scene_dir, f"clip_{clip_idx:02d}.mp4")
            
            if os.path.exists(clip_file):
                print(f"  Clip {clip_idx}: exists")
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
                generate_clip_manual(
                    checkpoint_path=args.checkpoint,
                    gemma_root=args.gemma_root,
                    prompt=prompt,
                    neg_prompt=NEG_PROMPT,
                    seed=seed,
                    output_path=clip_file,
                    height=args.height,
                    width=args.width,
                    num_frames=121,
                    frame_rate=24.0,
                    num_steps=args.num_steps,
                )
                elapsed = time.time() - start
                result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", clip_file],
                    capture_output=True, text=True
                )
                clip_dur = float(result.stdout.strip()) if result.stdout.strip() else 0
                print(f"    => {clip_dur:.1f}s clip in {elapsed:.0f}s")
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
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", scene_file],
                capture_output=True, text=True
            )
            total_dur = float(result.stdout.strip()) if result.stdout.strip() else 0
            print(f"  => Scene {scene_num}: {total_dur:.1f}s")
            
            meta = {
                "scene_num": scene_num, "title": sp.get("title", ""),
                "n_clips": len(clip_paths), "video_duration": total_dur,
                "narration_duration": narr_dur,
                "model": "LTX-2.3-22B-dev (BF16, no quantization, no upscalers)",
                "resolution": f"{args.width}x{args.height}", "steps": args.num_steps,
            }
            with open(os.path.join(scene_dir, "metadata.json"), "w") as f:
                json.dump(meta, f, indent=2)
    
    print("\n\nDone!")

if __name__ == "__main__":
    with torch.inference_mode():
        main()
