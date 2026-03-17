#!/usr/bin/env python3
"""
V6 Clip Generator v3 — Optimized for throughput.

Key optimizations over v2:
1. Persistent transformer: loaded once, reused across all sub-clips in a clip
2. Persistent ModelLedger + video encoder/decoder: no repeated construction
3. Async upload pipeline: B2 + Frame.io uploads run in background thread
4. RAMDisk for temporary sub-clip files (/dev/shm)
5. torch.compile on transformer (if supported)
"""

import argparse
import gc
import json
import hashlib
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import torch

# Frame.io upload (optional, fails silently if not configured)
try:
    from frameio_upload import upload_to_frameio
    FRAMEIO_ENABLED = True
except ImportError:
    FRAMEIO_ENABLED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/root/v6_generation.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
MODEL_DIR = Path("/root/models")
CHECKPOINT_PATH = MODEL_DIR / "ltx-2.3-22b-dev.safetensors"
GEMMA_ROOT = MODEL_DIR / "text_encoder"
CLIP_PLAN = Path("/root/v5_clip_plan.json")
OUTPUT_DIR = Path("/root/clips_out")
EMBEDDINGS_DIR = Path("/root/embeddings_cache")
PROGRESS_FILE = Path("/root/v6_progress.json")

# Use RAMDisk for sub-clip temp files if available
TEMP_DIR = Path("/dev/shm/ltx_clips") if Path("/dev/shm").exists() else OUTPUT_DIR

B2_BUCKET = "economy-vid-assets"
B2_PREFIX = "v5_clips_v2"

DEFAULT_NEGATIVE_PROMPT = (
    "blurry, out of focus, overexposed, underexposed, low contrast, washed out colors, excessive noise, "
    "grainy texture, poor lighting, flickering, motion blur, distorted proportions, unnatural skin tones, "
    "deformed facial features, asymmetrical face, missing facial features, extra limbs, disfigured hands, "
    "wrong hand count, artifacts around text, inconsistent perspective, camera shake, incorrect depth of "
    "field, background too sharp, background clutter, distracting reflections, harsh shadows, inconsistent "
    "lighting direction, color banding, cartoonish rendering, 3D CGI look, unrealistic materials, uncanny "
    "valley effect, incorrect ethnicity, wrong gender, exaggerated expressions, wrong gaze direction, "
    "mismatched lip sync, silent or muted audio, distorted voice, robotic voice, echo, background noise, "
    "off-sync audio, incorrect dialogue, added dialogue, repetitive speech, jittery movement, awkward "
    "pauses, incorrect timing, unnatural transitions, inconsistent framing, tilted camera, flat lighting, "
    "inconsistent tone, cinematic oversaturation, stylized filters, or AI artifacts."
)


# ── Async Upload Queue ────────────────────────────────────────────────────
upload_q = queue.Queue()

def upload_worker():
    """Background thread: processes upload jobs without blocking generation."""
    while True:
        job = upload_q.get()
        if job is None:
            break
        try:
            upload_file, clip_id, metadata, meta_path = job
            b2_upload(Path(upload_file), f"{B2_PREFIX}/{clip_id}.mp4")
            if meta_path:
                b2_upload(Path(meta_path), f"{B2_PREFIX}/{clip_id}_meta.json")
            if FRAMEIO_ENABLED:
                try:
                    upload_to_frameio(upload_file, f"{clip_id}.mp4", metadata)
                except Exception as e:
                    log.warning(f"  Frame.io upload failed (non-fatal): {e}")
        except Exception as e:
            log.warning(f"  Upload worker error: {e}")
        finally:
            upload_q.task_done()

# Start upload worker thread
_upload_thread = threading.Thread(target=upload_worker, daemon=True)
_upload_thread.start()


# ── B2 Upload ─────────────────────────────────────────────────────────────
def b2_upload(local_path: Path, remote_key: str) -> bool:
    try:
        cmd = ["b2", "file", "upload", "--no-progress", B2_BUCKET, str(local_path), remote_key]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            log.info(f"  ✓ B2: {remote_key}")
            return True
        log.error(f"  ✗ B2 fail: {result.stderr[:200]}")
        return False
    except Exception as e:
        log.error(f"  ✗ B2 exception: {e}")
        return False

# ── Progress ──────────────────────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"completed": [], "failed": []}

def save_progress(progress: dict):
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


# ── Phase 1: Encode all prompts ──────────────────────────────────────────
def prompt_hash(prompt: str) -> str:
    return hashlib.md5(prompt.encode()).hexdigest()[:12]


# ── Persistent Model Context ─────────────────────────────────────────────
class PersistentModels:
    """Holds models that persist across sub-clips within a clip."""
    
    def __init__(self, checkpoint_path: str, use_compile: bool = True):
        from ltx_pipelines.utils import ModelLedger
        
        self.device = torch.device("cuda")
        self.dtype = torch.bfloat16
        
        self.ledger = ModelLedger(
            dtype=self.dtype,
            device=self.device,
            checkpoint_path=checkpoint_path,
            gemma_root_path=None,
            loras=(),
            quantization=None,
        )
        
        self.transformer = None
        self._transformer_cpu = None  # CPU offload cache
        self.use_compile = use_compile
        self._compiled = False
        
        # Pre-load negative embeddings (constant across all clips)
        self.v_context_n, self.a_context_n = self._load_embeddings(DEFAULT_NEGATIVE_PROMPT)
        log.info("PersistentModels initialized")
    
    def _load_embeddings(self, prompt: str):
        h = prompt_hash(prompt)
        v = torch.load(EMBEDDINGS_DIR / f"{h}_v.pt", map_location="cuda")
        a = torch.load(EMBEDDINGS_DIR / f"{h}_a.pt", map_location="cuda")
        return v, a
    
    def load_embeddings(self, prompt: str):
        return self._load_embeddings(prompt)
    
    def ensure_transformer(self):
        """Load transformer to GPU — from CPU cache or from checkpoint."""
        if self.transformer is not None:
            # Already on GPU
            return
        if self._transformer_cpu is not None:
            # Fast path: move from CPU cache to GPU (~2-3s vs ~5s from disk)
            t0 = time.time()
            self.transformer = self._transformer_cpu.to(self.device)
            self._transformer_cpu = None  # Free CPU copy
            log.info(f"    Transformer moved to GPU from CPU: {torch.cuda.memory_allocated()/1e9:.1f}GB ({time.time()-t0:.1f}s)")
        else:
            # Cold start: build from checkpoint
            self.transformer = self.ledger.transformer()
            log.info(f"    Transformer loaded from checkpoint: {torch.cuda.memory_allocated()/1e9:.1f}GB")
            
            if self.use_compile and not self._compiled:
                try:
                    log.info("    Compiling transformer (first run only)...")
                    self.transformer = torch.compile(self.transformer, mode="reduce-overhead", dynamic=False)
                    self._compiled = True
                    log.info("    Compiled successfully")
                except Exception as e:
                    log.warning(f"    torch.compile failed (non-fatal, using eager): {e}")
                    self._compiled = True  # Don't retry
    
    def offload_transformer_to_cpu(self):
        """Move transformer to CPU RAM (fast to bring back vs rebuilding from checkpoint).
        nn.Module.to() is in-place, so after .to('cpu') the same object is on CPU.
        """
        if self.transformer is not None:
            t0 = time.time()
            self.transformer.to("cpu")  # In-place move
            self._transformer_cpu = self.transformer
            self.transformer = None
            gc.collect()
            torch.cuda.empty_cache()
            log.info(f"    Transformer offloaded to CPU ({time.time()-t0:.1f}s), VRAM: {torch.cuda.memory_allocated()/1e9:.1f}GB")
    
    def release_transformer(self):
        """Fully release transformer (both GPU and CPU)."""
        if self.transformer is not None:
            del self.transformer
            self.transformer = None
        if self._transformer_cpu is not None:
            del self._transformer_cpu
            self._transformer_cpu = None
        gc.collect()
        torch.cuda.empty_cache()
    
    def get_video_encoder(self):
        """Create a fresh video encoder (lightweight, ~200MB)."""
        return self.ledger.video_encoder()
    
    def get_video_decoder(self):
        return self.ledger.video_decoder()
    
    def get_audio_decoder(self):
        return self.ledger.audio_decoder()
    
    def get_vocoder(self):
        return self.ledger.vocoder()


# ── Phase 2: Generate clips ──────────────────────────────────────────────
def generate_single_subclip(
    models: PersistentModels,
    prompt: str,
    num_frames: int,
    seed: int,
    output_path: str,
    image_path: str = None,
):
    """Generate a single sub-clip reusing persistent model context."""
    from ltx_pipelines.utils import cleanup_memory, combined_image_conditionings, denoise_audio_video
    from ltx_pipelines.utils import euler_denoising_loop, multi_modal_guider_factory_denoising_func
    from ltx_core.components.diffusion_steps import EulerDiffusionStep
    from ltx_core.components.guiders import MultiModalGuiderParams, create_multimodal_guider_factory
    from ltx_core.components.noisers import GaussianNoiser
    from ltx_core.components.schedulers import LTX2Scheduler
    from ltx_core.types import VideoPixelShape
    from ltx_pipelines.utils.args import ImageConditioningInput
    from ltx_pipelines.utils.types import PipelineComponents
    from ltx_pipelines.utils.media_io import encode_video
    
    device = models.device
    dtype = models.dtype
    frame_rate = 24.0
    
    pipeline_components = PipelineComponents(dtype=dtype, device=device)
    generator = torch.Generator(device=device).manual_seed(seed)
    noiser = GaussianNoiser(generator=generator)
    stepper = EulerDiffusionStep()
    
    # Load prompt embeddings
    v_context_p, a_context_p = models.load_embeddings(prompt)
    
    # Step 1: Video encoder for image conditioning
    output_shape = VideoPixelShape(batch=1, frames=num_frames, width=768, height=512, fps=frame_rate)
    
    images = []
    if image_path and Path(image_path).exists():
        images = [ImageConditioningInput(path=image_path, frame_idx=0, strength=1.0)]
    
    # Release transformer to make room for video encoder if needed
    # Actually, video encoder is small (~200MB), transformer can stay loaded
    video_encoder = models.get_video_encoder()
    conditionings = combined_image_conditionings(
        images=images,
        height=output_shape.height,
        width=output_shape.width,
        video_encoder=video_encoder,
        dtype=dtype,
        device=device,
    )
    torch.cuda.synchronize()
    del video_encoder
    cleanup_memory()
    log.info(f"    After video_encoder: {torch.cuda.memory_allocated()/1e9:.1f}GB")
    
    # Step 2: Ensure transformer is loaded (persistent)
    models.ensure_transformer()
    
    sigmas = LTX2Scheduler().execute(steps=30).to(dtype=torch.float32, device=device)
    
    video_guider = MultiModalGuiderParams(
        cfg_scale=3.0, stg_scale=1.0, rescale_scale=0.7,
        modality_scale=3.0, skip_step=0, stg_blocks=[28],
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0, stg_scale=1.0, rescale_scale=0.7,
        modality_scale=3.0, skip_step=0, stg_blocks=[28],
    )
    
    video_guider_factory = create_multimodal_guider_factory(params=video_guider, negative_context=models.v_context_n)
    audio_guider_factory = create_multimodal_guider_factory(params=audio_guider, negative_context=models.a_context_n)
    
    def denoising_loop(sigmas, video_state, audio_state, stepper):
        return euler_denoising_loop(
            sigmas=sigmas,
            video_state=video_state,
            audio_state=audio_state,
            stepper=stepper,
            denoise_fn=multi_modal_guider_factory_denoising_func(
                video_guider_factory=video_guider_factory,
                audio_guider_factory=audio_guider_factory,
                v_context=v_context_p,
                a_context=a_context_p,
                transformer=models.transformer,
            ),
        )
    
    video_state, audio_state = denoise_audio_video(
        output_shape=output_shape,
        conditionings=conditionings,
        noiser=noiser,
        sigmas=sigmas,
        stepper=stepper,
        denoising_loop_fn=denoising_loop,
        components=pipeline_components,
        dtype=dtype,
        device=device,
    )
    
    torch.cuda.synchronize()
    # Offload transformer to CPU before VAE decode to free VRAM
    # (transformer 38GB + latents + decoders can exceed VRAM)
    models.offload_transformer_to_cpu()
    
    from ltx_core.model.video_vae import decode_video as vae_decode_video
    from ltx_core.model.audio_vae import decode_audio as vae_decode_audio
    
    video_decoder = models.get_video_decoder()
    decoded_video = vae_decode_video(video_state.latent, video_decoder, generator=generator)
    del video_decoder
    
    audio_decoder = models.get_audio_decoder()
    vocoder = models.get_vocoder()
    decoded_audio = vae_decode_audio(audio_state.latent, audio_decoder, vocoder)
    del audio_decoder, vocoder
    
    # Free latent states before encoding
    del video_state, audio_state
    gc.collect()
    torch.cuda.empty_cache()
    
    encode_video(video=decoded_video, fps=frame_rate, audio=decoded_audio, output_path=output_path, video_chunks_number=1)
    del decoded_video, decoded_audio
    gc.collect()
    torch.cuda.empty_cache()
    log.info(f"    Encoded to {output_path}, VRAM: {torch.cuda.memory_allocated()/1e9:.1f}GB")


def extract_last_frame(video_path: str, output_path: str) -> bool:
    try:
        cmd = ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path, "-frames:v", "1", "-q:v", "2", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and os.path.exists(output_path)
    except:
        return False

def get_duration(video_path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except:
        return 0.0


def generate_clip(models: PersistentModels, clip: dict, clip_idx: int, last_frame: str = None):
    """Generate all sub-clips for one clip, concat, trim, upload to B2."""
    clip_id = clip["id"]
    prompt = clip["prompt"]
    required_dur = clip["required_duration"]
    sub_clips = clip.get("sub_clips", [])
    seed = int(hashlib.md5(clip_id.encode()).hexdigest()[:8], 16) % (2**31)
    
    log.info(f"[{clip_idx+1}/334] {clip_id}: {len(sub_clips)} sub-clips, target {required_dur:.1f}s")
    
    clip_dir = OUTPUT_DIR / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    
    # Use RAMDisk for sub-clip temp files
    temp_clip_dir = TEMP_DIR / clip_id
    temp_clip_dir.mkdir(parents=True, exist_ok=True)
    
    sub_paths = []
    prev_frame = last_frame
    
    for si, sub in enumerate(sub_clips):
        sub_frames = sub["frames"]
        sub_type = sub["type"]
        sub_path = str(temp_clip_dir / f"sub_{si:02d}.mp4")
        
        # Image conditioning for continuity
        img_cond = None
        if sub_type in ("middle", "last") and prev_frame:
            img_cond = prev_frame
        elif sub_type == "first" and last_frame:
            img_cond = last_frame
        
        # Ensure frame count is 8k+1
        k = max(1, (sub_frames - 1) // 8)
        gen_frames = 8 * k + 1
        
        log.info(f"  sub{si}: {gen_frames} frames, type={sub_type}")
        
        try:
            generate_single_subclip(
                models=models,
                prompt=prompt,
                num_frames=gen_frames,
                seed=seed + si,
                output_path=sub_path,
                image_path=img_cond,
            )
        except torch.cuda.OutOfMemoryError:
            log.error(f"  OOM on sub{si}, clearing and retrying...")
            models.release_transformer()
            torch.cuda.empty_cache()
            gc.collect()
            gen_frames = max(9, 8 * (k // 2) + 1)
            try:
                generate_single_subclip(
                    models=models,
                    prompt=prompt,
                    num_frames=gen_frames,
                    seed=seed + si,
                    output_path=sub_path,
                    image_path=img_cond,
                )
            except Exception as e2:
                log.error(f"  FAILED: {e2}")
                return None
        
        if os.path.exists(sub_path):
            sub_paths.append(sub_path)
            frame_out = str(clip_dir / f"last_frame_{si:02d}.jpg")
            if extract_last_frame(sub_path, frame_out):
                prev_frame = frame_out
        else:
            log.error(f"  sub{si} missing!")
            return None
        
        # Light cleanup between sub-clips (but keep transformer intent)
        gc.collect()
        torch.cuda.empty_cache()
    
    if not sub_paths:
        return None
    
    # Release transformer before heavy I/O
    models.release_transformer()
    
    # Concatenate
    final_path = str(clip_dir / f"{clip_id}_raw.mp4")
    if len(sub_paths) == 1:
        import shutil
        shutil.move(sub_paths[0], final_path)
    else:
        concat_list = str(temp_clip_dir / "concat.txt")
        with open(concat_list, "w") as f:
            for sp in sub_paths:
                f.write(f"file '{sp}'\n")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", final_path],
                       capture_output=True, timeout=60)
    
    if not os.path.exists(final_path):
        return None
    
    # Trim (never stretch)
    actual_dur = get_duration(final_path)
    trimmed = str(clip_dir / f"{clip_id}.mp4")
    
    if actual_dur < required_dur * 0.95:
        log.warning(f"  ⚠ {clip_id} short: {actual_dur:.1f}s vs {required_dur:.1f}s")
        os.rename(final_path, trimmed)
    elif actual_dur > required_dur + 0.5:
        subprocess.run(["ffmpeg", "-y", "-i", final_path, "-t", str(required_dur), "-c", "copy", trimmed],
                       capture_output=True, timeout=60)
    else:
        os.rename(final_path, trimmed)
    
    if not os.path.exists(trimmed):
        return None
    
    # Strip audio (we have our own narration)
    noaudio = str(clip_dir / f"{clip_id}_noaudio.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", trimmed, "-an", "-c:v", "copy", noaudio],
                   capture_output=True, timeout=60)
    
    upload_file = noaudio if os.path.exists(noaudio) else trimmed
    
    # Build production metadata
    actual_final_dur = get_duration(upload_file)
    metadata = {
        "clip_id": clip_id,
        "act": clip.get("act", ""),
        "narration": clip.get("narration", ""),
        "prompt": prompt,
        "negative_prompt": DEFAULT_NEGATIVE_PROMPT,
        "narr_start": clip.get("narr_start", 0),
        "narr_end": clip.get("narr_end", 0),
        "narr_duration": clip.get("narr_duration", 0),
        "required_duration": required_dur,
        "actual_duration": actual_final_dur,
        "generation": {
            "model": "LTX-2.3-22B-dev (bf16)",
            "checkpoint": str(CHECKPOINT_PATH.name),
            "resolution": "768x512",
            "fps": 24,
            "denoising_steps": 30,
            "cfg_scale_video": 3.0,
            "cfg_scale_audio": 7.0,
            "stg_scale": 1.0,
            "rescale": 0.7,
            "scheduler": "LTX2Scheduler (Euler)",
            "seed": seed,
            "sub_clips": len(sub_clips),
            "frames_per_sub": [s["frames"] for s in sub_clips],
            "image_conditioning": bool(last_frame),
            "dtype": "bfloat16",
            "quantization": None,
            "upscaler": None,
        },
        "narration_word_count": clip.get("narration_word_count", 0),
        "generation_strategy": clip.get("generation_strategy", ""),
    }
    meta_path = str(clip_dir / f"{clip_id}_meta.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    
    # Async upload — don't block generation of next clip
    upload_q.put((upload_file, clip_id, metadata, meta_path))
    
    # Last frame for next clip
    clip_last = str(clip_dir / "last_frame_final.jpg")
    extract_last_frame(trimmed, clip_last)
    
    # Cleanup temp sub-clips from RAMDisk
    for sp in sub_paths:
        if os.path.exists(sp):
            try:
                os.unlink(sp)
            except:
                pass
    
    return clip_last if os.path.exists(clip_last) else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=334)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-compile", action="store_true", help="Disable torch.compile")

    args = parser.parse_args()
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(CLIP_PLAN) as f:
        plan = json.load(f)
    clips = plan["clips"]
    
    log.info(f"V6 Generation v3: clips {args.start}-{args.end} of {len(clips)}")
    log.info(f"GPU: {torch.cuda.get_device_name(0)}, VRAM: {torch.cuda.get_device_properties(0).total_memory/1e9:.1f}GB")
    log.info(f"Optimizations: persistent_transformer=True, async_upload=True, ramdisk={TEMP_DIR}, compile={not args.no_compile}")
    
    # Phase 1: Encode prompts in a SUBPROCESS to avoid VRAM leak
    log.info("Phase 1: Encoding prompts in subprocess...")
    enc_result = subprocess.run(
        [sys.executable, "/root/v6_encode_prompts.py"],
        capture_output=True, text=True, timeout=1800,
    )
    if enc_result.returncode != 0:
        log.error(f"Prompt encoding failed: {enc_result.stderr[-500:]}")
        sys.exit(1)
    log.info("Phase 1 complete: all prompts encoded")
    
    # Verify embeddings exist
    neg_h = prompt_hash(DEFAULT_NEGATIVE_PROMPT)
    if not (EMBEDDINGS_DIR / f"{neg_h}_v.pt").exists():
        log.error("Negative prompt embeddings missing!")
        sys.exit(1)
    
    # Initialize persistent models
    models = PersistentModels(
        checkpoint_path=str(CHECKPOINT_PATH),
        use_compile=not args.no_compile,
    )
    
    # Phase 2: Generate clips
    progress = load_progress()
    completed_set = set(progress["completed"])
    
    last_frame = None
    total_time = 0
    generated = 0
    
    for i in range(args.start, min(args.end, len(clips))):
        clip = clips[i]
        clip_id = clip["id"]
        
        if args.resume and clip_id in completed_set:
            log.info(f"[{i+1}] Skipping {clip_id} (done)")
            lf = OUTPUT_DIR / clip_id / "last_frame_final.jpg"
            if lf.exists():
                last_frame = str(lf)
            continue
        
        t0 = time.time()
        try:
            last_frame = generate_clip(models, clip, i, last_frame)
            elapsed = time.time() - t0
            total_time += elapsed
            generated += 1
            
            if last_frame:
                progress["completed"].append(clip_id)
                log.info(f"  ✓ {clip_id} in {elapsed:.0f}s (avg {total_time/generated:.0f}s)")
            else:
                progress["failed"].append(clip_id)
                log.error(f"  ✗ {clip_id} FAILED")
            
            save_progress(progress)
            
        except Exception as e:
            log.error(f"  ✗ {clip_id}: {e}")
            import traceback
            traceback.print_exc()
            progress["failed"].append(clip_id)
            save_progress(progress)
            models.release_transformer()
            torch.cuda.empty_cache()
    
    # Wait for all uploads to finish
    log.info("Waiting for pending uploads...")
    upload_q.join()
    
    log.info(f"\nDone: {generated} clips in {total_time/3600:.1f}h")
    log.info(f"Completed: {len(progress['completed'])}, Failed: {len(progress['failed'])}")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
