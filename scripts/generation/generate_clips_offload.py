#!/usr/bin/env python3
"""
V5 Clip Generation Script with CPU offloading for LTX-2.3.
Monkey-patches ModelLedger to offload models between GPU and CPU.
"""
import json
import os
import gc
import sys
import time

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import hashlib
import base64
import subprocess
import logging
import traceback

import torch

logging.getLogger().setLevel(logging.WARNING)

# B2 credentials
B2_KEY_ID = "${B2_KEY_ID}"
B2_APP_KEY = "${B2_APP_KEY}"
B2_BUCKET = "economy-vid-assets"
B2_BUCKET_ID = "8023e9c8f670ec4f9fc5051f"

MODELS_DIR = "/root/models"
OUTPUT_DIR = "/root/ltx_gen/outputs"
FRAMES_DIR = "/root/ltx_gen/frames"
PROGRESS_FILE = "/root/ltx_gen/progress.json"

FPS = 24
WIDTH = 768
HEIGHT = 512

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(FRAMES_DIR, exist_ok=True)

# ---- B2 Upload ----
_b2_auth_cache = {}

def b2_authorize():
    global _b2_auth_cache
    if _b2_auth_cache and time.time() - _b2_auth_cache.get("ts", 0) < 3600:
        return _b2_auth_cache
    auth = base64.b64encode(f"{B2_KEY_ID}:{B2_APP_KEY}".encode()).decode()
    result = subprocess.run(
        ["curl", "-s", "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
         "-H", f"Authorization: Basic {auth}"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    _b2_auth_cache = {
        "api_url": data["apiInfo"]["storageApi"]["apiUrl"],
        "auth_token": data["authorizationToken"],
        "ts": time.time()
    }
    return _b2_auth_cache

def b2_upload(local_path, b2_path):
    auth = b2_authorize()
    result = subprocess.run(
        ["curl", "-s", "-X", "POST",
         f"{auth['api_url']}/b2api/v3/b2_get_upload_url",
         "-H", f"Authorization: {auth['auth_token']}",
         "-d", json.dumps({"bucketId": B2_BUCKET_ID})],
        capture_output=True, text=True
    )
    upload_data = json.loads(result.stdout)
    sha1 = hashlib.sha1(open(local_path, "rb").read()).hexdigest()
    file_size = os.path.getsize(local_path)
    content_type = "video/mp4" if local_path.endswith(".mp4") else "image/jpeg"
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", upload_data["uploadUrl"],
         "-H", f"Authorization: {upload_data['authorizationToken']}",
         "-H", f"X-Bz-File-Name: {b2_path}",
         "-H", f"Content-Type: {content_type}",
         "-H", f"Content-Length: {file_size}",
         "-H", f"X-Bz-Content-Sha1: {sha1}",
         "--data-binary", f"@{local_path}"],
        capture_output=True, text=True, timeout=300
    )
    return json.loads(result.stdout)

def get_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip()) if result.stdout.strip() else 0

def extract_last_frame(video_path, output_path):
    subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path,
         "-frames:v", "1", "-q:v", "2", output_path],
        capture_output=True, text=True
    )
    return os.path.exists(output_path)


def cleanup():
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()


# ---- Offloading Pipeline Wrapper ----
class OffloadedPipeline:
    """Wraps DistilledPipeline with CPU offloading between stages."""

    def __init__(self):
        from ltx_core.quantization.policy import QuantizationPolicy
        from ltx_pipelines.distilled import DistilledPipeline
        from ltx_pipelines.utils.model_ledger import ModelLedger

        self.device = torch.device("cuda")
        self.dtype = torch.bfloat16

        # Create model ledger (doesn't load models yet)
        self.model_ledger = ModelLedger(
            dtype=self.dtype,
            device=self.device,
            checkpoint_path=f"{MODELS_DIR}/ltx-2.3-22b-distilled.safetensors",
            spatial_upsampler_path=f"{MODELS_DIR}/ltx-2.3-spatial-upscaler-x2-1.0.safetensors",
            gemma_root_path=f"{MODELS_DIR}/gemma",
            loras=[],
            quantization=QuantizationPolicy.fp8_cast(),
        )

        from ltx_pipelines.distilled import PipelineComponents
        self.pipeline_components = PipelineComponents(
            dtype=self.dtype,
            device=self.device,
        )
        print("OffloadedPipeline initialized", flush=True)

    def __call__(self, prompt, seed, height, width, num_frames, frame_rate, images=None, tiling_config=None, enhance_prompt=False):
        from ltx_pipelines.distilled import (
            DISTILLED_SIGMA_VALUES, STAGE_2_DISTILLED_SIGMA_VALUES,
            denoise_audio_video, euler_denoising_loop, simple_denoising_func,
            upsample_video,
        )
        from ltx_pipelines.utils.helpers import encode_prompts, combined_image_conditionings
        from ltx_core.model.video_vae import VideoPixelShape
        from ltx_core.model.latent_state import LatentState
        from ltx_core.model.noiser import GaussianNoiser
        from ltx_core.model.diffusion import EulerDiffusionStep

        if images is None:
            images = []

        generator = torch.Generator(device=self.device).manual_seed(seed)
        noiser = GaussianNoiser(generator=generator)
        stepper = EulerDiffusionStep()

        # Phase 1: Encode prompts (loads text encoder + embeddings processor)
        print("  Phase 1: Encoding prompts...", flush=True)
        (ctx_p,) = encode_prompts(
            [prompt],
            self.model_ledger,
            enhance_first_prompt=enhance_prompt,
            enhance_prompt_image=images[0][0] if len(images) > 0 else None,
        )
        video_context, audio_context = ctx_p.video_encoding, ctx_p.audio_encoding
        # Move contexts to CPU to free GPU for transformer
        video_context_cpu = video_context.to("cpu") if hasattr(video_context, 'to') else video_context
        audio_context_cpu = audio_context.to("cpu") if hasattr(audio_context, 'to') else audio_context
        del ctx_p
        cleanup()
        print(f"  Prompts encoded. GPU free: {torch.cuda.mem_get_info()[0]/1024**3:.1f}GB", flush=True)

        # Phase 2: Stage 1 - generate at half resolution
        print("  Phase 2: Stage 1 generation...", flush=True)
        # Move contexts back to GPU
        video_context = video_context_cpu.to(self.device) if hasattr(video_context_cpu, 'to') else video_context_cpu
        audio_context = audio_context_cpu.to(self.device) if hasattr(audio_context_cpu, 'to') else audio_context_cpu
        del video_context_cpu, audio_context_cpu

        video_encoder = self.model_ledger.video_encoder()
        transformer = self.model_ledger.transformer()
        stage_1_sigmas = torch.Tensor(DISTILLED_SIGMA_VALUES).to(self.device)

        def denoising_loop(sigmas, video_state, audio_state, stepper):
            return euler_denoising_loop(
                sigmas=sigmas,
                video_state=video_state,
                audio_state=audio_state,
                stepper=stepper,
                denoise_fn=simple_denoising_func(
                    video_context=video_context,
                    audio_context=audio_context,
                    transformer=transformer,
                ),
            )

        stage_1_output_shape = VideoPixelShape(
            batch=1, frames=num_frames, width=width // 2, height=height // 2, fps=frame_rate,
        )
        stage_1_conditionings = combined_image_conditionings(
            images=images,
            height=stage_1_output_shape.height,
            width=stage_1_output_shape.width,
            video_encoder=video_encoder,
            dtype=self.dtype,
            device=self.device,
        )

        video_state, audio_state = denoise_audio_video(
            output_shape=stage_1_output_shape,
            conditionings=stage_1_conditionings,
            noiser=noiser,
            sigmas=stage_1_sigmas,
            stepper=stepper,
            denoising_loop_fn=denoising_loop,
            components=self.pipeline_components,
            dtype=self.dtype,
            device=self.device,
        )
        print("  Stage 1 complete.", flush=True)

        # Phase 3: Upsample
        print("  Phase 3: Upsampling...", flush=True)
        upscaled_video_latent = upsample_video(
            latent=video_state.latent[:1],
            video_encoder=video_encoder,
            upsampler=self.model_ledger.spatial_upsampler(),
        )
        torch.cuda.synchronize()
        cleanup()

        # Phase 4: Stage 2 - refine at full resolution
        print("  Phase 4: Stage 2 refinement...", flush=True)
        stage_2_sigmas = torch.Tensor(STAGE_2_DISTILLED_SIGMA_VALUES).to(self.device)
        stage_2_output_shape = VideoPixelShape(
            batch=1, frames=num_frames, width=width, height=height, fps=frame_rate,
        )
        stage_2_conditionings = combined_image_conditionings(
            images=images,
            height=stage_2_output_shape.height,
            width=stage_2_output_shape.width,
            video_encoder=video_encoder,
            dtype=self.dtype,
            device=self.device,
        )

        video_state2, audio_state2 = denoise_audio_video(
            output_shape=stage_2_output_shape,
            conditionings=stage_2_conditionings,
            noiser=noiser,
            sigmas=stage_2_sigmas,
            stepper=stepper,
            denoising_loop_fn=denoising_loop,
            components=self.pipeline_components,
            dtype=self.dtype,
            device=self.device,
            initial_video_latent=upscaled_video_latent,
            initial_audio_latent=audio_state.latent,
            noise_scale=stage_2_sigmas[0],
        )

        # Phase 5: Decode
        print("  Phase 5: Decoding...", flush=True)
        del transformer, video_encoder
        cleanup()

        video_decoder = self.model_ledger.video_decoder()
        from ltx_core.model.video_vae import TilingConfig, get_video_chunks_number
        tc = tiling_config or TilingConfig.default()
        decoded_video = video_decoder.decode(video_state2.latent, tiling_config=tc)

        audio_decoder = self.model_ledger.audio_decoder()
        decoded_audio = audio_decoder.decode(audio_state2.latent)

        del video_decoder, audio_decoder
        cleanup()

        return decoded_video, decoded_audio


def generate_one_subclip(pipeline, prompt, num_frames, seed, input_image=None):
    """Generate a single sub-clip."""
    from ltx_core.model.video_vae import TilingConfig, get_video_chunks_number
    from ltx_pipelines.utils.media_io import encode_video
    from ltx_pipelines.utils.args import ImageConditioningInput

    tiling_config = TilingConfig.default()

    images = []
    if input_image is not None:
        images = [ImageConditioningInput(path=input_image, frame_idx=0, strength=1.0)]

    decoded_video, decoded_audio = pipeline(
        prompt=prompt,
        seed=seed,
        height=HEIGHT,
        width=WIDTH,
        num_frames=num_frames,
        frame_rate=FPS,
        images=images,
        tiling_config=tiling_config,
        enhance_prompt=False,
    )

    temp_path = f"/tmp/gen_{seed}.mp4"
    video_chunks_number = get_video_chunks_number(num_frames, tiling_config)
    encode_video(
        video=decoded_video,
        fps=FPS,
        audio=decoded_audio,
        output_path=temp_path,
        video_chunks_number=video_chunks_number,
    )

    return temp_path


def process_clip(pipeline, clip_data, clip_index):
    """Process a single clip, potentially with frame chaining."""
    clip_id = clip_data["id"]
    prompt = clip_data["prompt"]
    sub_clips = clip_data["sub_clips"]
    narr_duration = clip_data["narr_duration"]

    print(f"\n{'='*60}")
    print(f"[{clip_index}] {clip_id}: {len(sub_clips)} sub-clips, {narr_duration:.1f}s narration")
    print(f"Prompt: {prompt[:100]}...")
    sys.stdout.flush()

    t0 = time.time()
    sub_clip_paths = []

    for si, sub in enumerate(sub_clips):
        num_frames = sub["frames"]
        seed = 42 + clip_index * 10 + si
        sub_type = sub["type"]

        print(f"  Sub-clip {si}: {num_frames}f ({num_frames/FPS:.1f}s), type={sub_type}", flush=True)

        input_image = None
        if sub_type in ("middle", "last") and sub_clip_paths:
            prev_path = sub_clip_paths[-1]
            frame_path = f"{FRAMES_DIR}/{clip_id}_sub{si-1}_last.jpg"
            if extract_last_frame(prev_path, frame_path):
                input_image = frame_path
                print(f"    Frame chaining from {frame_path}", flush=True)

        sub_t0 = time.time()
        sub_path = generate_one_subclip(pipeline, prompt, num_frames, seed, input_image)
        sub_elapsed = time.time() - sub_t0

        actual_dur = get_duration(sub_path)
        print(f"    Generated: {actual_dur:.2f}s in {sub_elapsed:.0f}s", flush=True)

        final_sub_path = f"{OUTPUT_DIR}/{clip_id}_sub{si}.mp4"
        os.rename(sub_path, final_sub_path)
        sub_clip_paths.append(final_sub_path)

        # Upload sub-clip to B2
        b2_path = f"v5_clips_v2/{clip_id}_sub{si}.mp4"
        try:
            b2_upload(final_sub_path, b2_path)
            print(f"    Uploaded to B2: {b2_path}", flush=True)
        except Exception as e:
            print(f"    B2 upload failed: {e}", flush=True)

        cleanup()

    # Concatenate sub-clips if multiple
    if len(sub_clip_paths) == 1:
        final_path = sub_clip_paths[0]
    else:
        ts_paths = []
        for sp in sub_clip_paths:
            ts_path = sp.replace(".mp4", ".ts")
            subprocess.run(
                ["ffmpeg", "-y", "-i", sp, "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                 "-f", "mpegts", ts_path],
                capture_output=True, text=True
            )
            ts_paths.append(ts_path)
        concat_input = "concat:" + "|".join(ts_paths)
        final_path = f"{OUTPUT_DIR}/{clip_id}_full.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", concat_input, "-c", "copy",
             "-bsf:a", "aac_adtstoasc", final_path],
            capture_output=True, text=True
        )

    # Trim to narration duration
    full_duration = get_duration(final_path)
    trimmed_path = f"{OUTPUT_DIR}/{clip_id}.mp4"

    if full_duration >= narr_duration:
        subprocess.run(
            ["ffmpeg", "-y", "-i", final_path, "-t", str(narr_duration),
             "-c", "copy", trimmed_path],
            capture_output=True, text=True
        )
        trimmed_dur = get_duration(trimmed_path)
        print(f"  Trimmed: {full_duration:.2f}s -> {trimmed_dur:.2f}s (target: {narr_duration:.1f}s)", flush=True)
    else:
        print(f"  WARNING: Video too short! {full_duration:.2f}s < {narr_duration:.1f}s target", flush=True)
        print(f"  Attempting regen with different seed...", flush=True)
        deficit = narr_duration - full_duration
        extra_frames = max(129, min(257, int(deficit * FPS) + 48))
        retry_seed = 9999 + clip_index * 10
        regen_input = None
        if sub_clip_paths:
            regen_frame = f"{FRAMES_DIR}/{clip_id}_regen_last.jpg"
            if extract_last_frame(sub_clip_paths[-1], regen_frame):
                regen_input = regen_frame
        try:
            extra_path = generate_one_subclip(pipeline, prompt, extra_frames, retry_seed, regen_input)
            extra_dur = get_duration(extra_path)
            print(f"  Extra clip: {extra_dur:.2f}s ({extra_frames} frames)", flush=True)
            extra_final = f"{OUTPUT_DIR}/{clip_id}_extra.mp4"
            os.rename(extra_path, extra_final)
            ts1 = final_path.replace('.mp4', '.ts')
            ts2 = extra_final.replace('.mp4', '.ts')
            for src, dst in [(final_path, ts1), (extra_final, ts2)]:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", src, "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                     "-f", "mpegts", dst], capture_output=True, text=True
                )
            concat_in = f"concat:{ts1}|{ts2}"
            extended_path = f"{OUTPUT_DIR}/{clip_id}_extended.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", concat_in, "-c", "copy",
                 "-bsf:a", "aac_adtstoasc", extended_path], capture_output=True, text=True
            )
            new_dur = get_duration(extended_path)
            if new_dur >= narr_duration:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", extended_path, "-t", str(narr_duration),
                     "-c", "copy", trimmed_path], capture_output=True, text=True
                )
                print(f"  Fixed! Extended to {new_dur:.2f}s, trimmed to {narr_duration:.1f}s", flush=True)
            else:
                os.rename(extended_path, trimmed_path)
                print(f"  Still short after regen: {new_dur:.2f}s < {narr_duration:.1f}s (using as-is)", flush=True)
            cleanup()
        except Exception as e:
            print(f"  Regen failed: {e}, using short clip as-is", flush=True)
            if final_path != trimmed_path and os.path.exists(final_path):
                os.rename(final_path, trimmed_path)

    # Upload final clip to B2
    b2_path = f"v5_clips_v2/{clip_id}.mp4"
    try:
        b2_upload(trimmed_path, b2_path)
        print(f"  Final uploaded to B2: {b2_path}", flush=True)
    except Exception as e:
        print(f"  B2 upload failed: {e}", flush=True)

    elapsed = time.time() - t0
    print(f"  Total time: {elapsed:.0f}s", flush=True)

    return {
        "clip_id": clip_id,
        "status": "completed",
        "elapsed": elapsed,
        "full_duration": full_duration,
        "trimmed_duration": get_duration(trimmed_path) if os.path.exists(trimmed_path) else 0,
        "target_duration": narr_duration,
        "b2_path": b2_path,
    }


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 generate_clips_offload.py <assignments.json> [--start N] [--count M]")
        sys.exit(1)

    assignments_path = sys.argv[1]
    start = 0
    count = None

    for i, arg in enumerate(sys.argv):
        if arg == "--start" and i + 1 < len(sys.argv):
            start = int(sys.argv[i + 1])
        if arg == "--count" and i + 1 < len(sys.argv):
            count = int(sys.argv[i + 1])

    with open(assignments_path) as f:
        assignments = json.load(f)

    clips = assignments["clips"]
    if count:
        clips = clips[start:start + count]
    else:
        clips = clips[start:]

    print(f"Assigned clips: {len(clips)}")
    print(f"Starting from: {start}")

    done = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            progress = json.load(f)
        for r in progress.get("results", []):
            if r.get("status") == "completed":
                done.add(r["clip_id"])
    else:
        progress = {"results": [], "started": time.time()}

    remaining = [c for c in clips if c["id"] not in done]
    print(f"Already done: {len(done)}")
    print(f"Remaining: {len(remaining)}")

    if not remaining:
        print("All clips already generated!")
        return

    # Load pipeline with offloading
    print("Loading OffloadedPipeline...")
    pipeline = OffloadedPipeline()

    for i, clip in enumerate(remaining):
        try:
            result = process_clip(pipeline, clip, i + 1)
            progress["results"].append(result)
            save_progress(progress)
        except Exception as e:
            print(f"ERROR processing {clip['id']}: {e}", flush=True)
            traceback.print_exc()
            progress["results"].append({
                "clip_id": clip["id"],
                "status": "failed",
                "error": str(e),
            })
            save_progress(progress)
            cleanup()

    progress["completed"] = time.time()
    progress["total_elapsed"] = time.time() - progress["started"]
    save_progress(progress)
    print(f"\nAll done! Total time: {progress['total_elapsed']:.0f}s")


if __name__ == "__main__":
    main()
