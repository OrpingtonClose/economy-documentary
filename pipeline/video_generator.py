#!/usr/bin/env python3
"""
Video Generator — LTX-2.3 with OTIO Integration
==================================================
Generates video clips using LTX-2.3 based on prompts stored in OTIO metadata.
After generation, clips are trimmed, placed on the OTIO video track, and
quality/generation metadata is written back to OTIO.

Key constraints (non-negotiable):
  - LTX-2.3 22B-dev ONLY (bf16, no distillation, no fp8, no quantization)
  - Generate clips LONGER than needed, then TRIM (no stretching)
  - NO LOOPING — every clip is unique
  - NO text on screen
  - 80GB VRAM minimum
  - Model loads ONCE, generates all clips in-process (no subprocess-per-clip)
  - Frame-chaining for clips > 5.04s: extract last frame → image-to-video continuation

Pipeline integration:
  1. Reads prompts from OTIO export JSON (derived from OTIO metadata)
  2. Generates video clips with LTX-2.3
  3. Trims each clip to exact target duration
  4. Updates OTIO timeline video track with clips
  5. Writes quality scores and generation params to OTIO metadata
  6. Uploads to B2 (optional, inline after each clip)

Usage (standalone on Vast.ai VM):
  python3 video_generator.py --manifest prompts.json \\
                              --otio war_economy_v9.otio \\
                              --output-dir /workspace/outputs

Usage (from pipeline):
  from pipeline.video_generator import VideoGenerator
  gen = VideoGenerator(otio_path, output_dir)
  gen.generate_all(prompts)
"""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# LTX-2.3 Configuration — full quality, no compromise
# ------------------------------------------------------------------
LTX_CHECKPOINT = "/workspace/models/ltx-2.3-22b-dev.safetensors"
GEMMA_ROOT = "/workspace/models/gemma-3-12b-it-qat-q4_0-unquantized"

HEIGHT = 512
WIDTH = 768
NUM_FRAMES = 121        # ~5.04s at 24fps
FRAME_RATE = 24.0
NUM_INFERENCE_STEPS = 30  # Full quality

VIDEO_GUIDER_CFG = {
    "cfg_scale": 3.0,
    "stg_scale": 1.0,
    "rescale_scale": 0.7,
    "modality_scale": 3.0,
    "skip_step": 0,
    "stg_blocks": [28],
}
AUDIO_GUIDER_CFG = {
    "cfg_scale": 7.0,
    "stg_scale": 1.0,
    "rescale_scale": 0.7,
    "modality_scale": 3.0,
    "skip_step": 0,
    "stg_blocks": [28],
}

NEGATIVE_PROMPT = (
    "blurry, low quality, text, watermark, letters, words, subtitles, logo, "
    "static, frozen, looping, repeated frames, shaky, glitchy, worst quality, "
    "deformed, distorted, motion smear, motion artifacts"
)

# B2 upload config
B2_BUCKET = "economy-vid-assets"
B2_PREFIX = "v9_war_economy"


def extract_last_frame_ffmpeg(video_path, frame_path):
    """Extract last frame from video file using ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-sseof", "-0.1",
        "-i", video_path,
        "-frames:v", "1", "-q:v", "2",
        frame_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
        return os.path.exists(frame_path)
    except Exception:
        return False


def trim_clip(input_path, output_path, target_duration):
    """Trim a video clip to exact target duration. No stretching."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-t", str(target_duration), "-c", "copy",
        output_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
        return os.path.exists(output_path)
    except Exception:
        return False


def concat_subclips(sub_paths, output_path, target_duration):
    """Concatenate sub-clips and trim to target duration."""
    if len(sub_paths) == 1:
        return trim_clip(sub_paths[0], output_path, target_duration)

    concat_file = output_path.replace(".mp4", "_concat.txt")
    with open(concat_file, "w") as f:
        for p in sub_paths:
            f.write(f"file '{p}'\n")

    raw_path = output_path.replace(".mp4", "_raw.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", concat_file, "-c", "copy", raw_path],
        capture_output=True, timeout=60,
    )

    result = trim_clip(raw_path, output_path, target_duration)

    # Cleanup
    for f in [concat_file, raw_path]:
        if os.path.exists(f):
            os.remove(f)

    return result


def probe_duration(path):
    """Get video duration via ffprobe."""
    cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{path}"'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 5.0


def upload_to_b2(clip_path, clip_id, b2_key_id=None, b2_app_key=None):
    """Upload a clip to B2 storage. Non-blocking on failure."""
    if not b2_key_id or not b2_app_key:
        return False

    try:
        # Embed metadata
        meta_path = clip_path.replace(".mp4", "_b2.mp4")
        meta_comment = (
            f"LTX-2.3 | {WIDTH}x{HEIGHT} | {NUM_FRAMES}frames | "
            f"{NUM_INFERENCE_STEPS}steps | cfg{VIDEO_GUIDER_CFG['cfg_scale']} | "
            f"bf16 full quality | v9_otio_pipeline"
        )
        subprocess.run([
            "ffmpeg", "-y", "-i", clip_path,
            "-metadata", f"title={clip_id}",
            "-metadata", f"comment={meta_comment}",
            "-c", "copy", meta_path,
        ], capture_output=True, timeout=30)

        upload_src = meta_path if os.path.exists(meta_path) else clip_path
        remote_path = f"{B2_PREFIX}/{clip_id}.mp4"

        result = subprocess.run(
            ["b2", "upload-file", B2_BUCKET, upload_src, remote_path],
            capture_output=True, text=True, timeout=120,
        )

        if os.path.exists(meta_path):
            os.remove(meta_path)

        if result.returncode == 0:
            log.info(f"  -> Uploaded {clip_id} to B2")
            return True
    except Exception as e:
        log.warning(f"  -> B2 upload failed for {clip_id}: {e}")

    return False


class VideoGenerator:
    """
    Generates video clips with LTX-2.3 and places them on the OTIO timeline.
    Writes quality scores and generation parameters back to OTIO metadata.

    Usage:
        gen = VideoGenerator(otio_path="timeline.otio", output_dir="./clips")
        gen.generate_all(prompts_list)
    """

    def __init__(self, otio_path, output_dir,
                 b2_key_id=None, b2_app_key=None,
                 ltx_checkpoint=None, gemma_root=None):
        self.otio_path = str(otio_path)
        self.output_dir = str(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

        self.b2_key_id = b2_key_id
        self.b2_app_key = b2_app_key
        self.b2_authorized = False

        self.ltx_checkpoint = ltx_checkpoint or LTX_CHECKPOINT
        self.gemma_root = gemma_root or GEMMA_ROOT

        self.pipeline = None

    def _load_pipeline(self):
        """Load LTX-2.3 pipeline ONCE."""
        if self.pipeline is not None:
            return

        import torch

        log.info("Loading LTX-2.3 pipeline (one-time)...")
        t0 = time.time()

        from ltx_core.components.guiders import MultiModalGuiderParams
        from ltx_pipelines.ti2vid_one_stage import TI2VidOneStagePipeline

        self.pipeline = TI2VidOneStagePipeline(
            checkpoint_path=self.ltx_checkpoint,
            gemma_root=self.gemma_root,
            loras=(),
            quantization=None,  # CRITICAL: no quantization, no fp8
        )

        self.video_guider = MultiModalGuiderParams(**VIDEO_GUIDER_CFG)
        self.audio_guider = MultiModalGuiderParams(**AUDIO_GUIDER_CFG)

        log.info(f"Pipeline loaded in {time.time() - t0:.1f}s")

    def _authorize_b2(self):
        """Authorize B2 CLI."""
        if self.b2_authorized or not self.b2_key_id:
            return
        try:
            result = subprocess.run(
                ["b2", "authorize-account", self.b2_key_id, self.b2_app_key],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.b2_authorized = True
        except Exception:
            pass

    def _generate_subclip(self, prompt, seed, images=None):
        """Generate a single 5.04s sub-clip."""
        import torch
        from ltx_pipelines.utils.args import ImageConditioningInput
        from ltx_pipelines.utils.media_io import encode_video

        video, audio = self.pipeline(
            prompt=prompt,
            negative_prompt=NEGATIVE_PROMPT,
            seed=seed,
            height=HEIGHT,
            width=WIDTH,
            num_frames=NUM_FRAMES,
            frame_rate=FRAME_RATE,
            num_inference_steps=NUM_INFERENCE_STEPS,
            video_guider_params=self.video_guider,
            audio_guider_params=self.audio_guider,
            images=images or [],
        )

        return video, audio

    def generate_clip(self, prompt_data):
        """Generate a single video clip (possibly with frame-chaining for longer clips).

        prompt_data: dict with clip_id, prompt, target_duration_sec, ltx_clips_needed, etc.
        Returns: dict with status, path, duration, seed, generation_time, etc.
        """
        import torch
        from ltx_pipelines.utils.args import ImageConditioningInput
        from ltx_pipelines.utils.media_io import encode_video

        clip_id = prompt_data["clip_id"]
        prompt = prompt_data["prompt"]
        target_dur = prompt_data["target_duration_sec"]
        ltx_count = prompt_data.get("ltx_clips_needed", 1)
        seed_base = hash(clip_id) % 2**31

        final_path = os.path.join(self.output_dir, f"{clip_id}.mp4")

        # Skip if already generated
        if os.path.exists(final_path) and os.path.getsize(final_path) > 1000:
            actual_dur = probe_duration(final_path)
            log.info(f"  {clip_id} — already exists ({actual_dur:.1f}s), skipping")
            return {
                "clip_id": clip_id, "status": "skipped",
                "path": final_path, "duration": actual_dur,
                "seed": seed_base,
            }

        sub_paths = []
        clip_start = time.time()

        for sub_idx in range(ltx_count):
            sub_path = os.path.join(self.output_dir, f"{clip_id}_sub{sub_idx:02d}.mp4")
            seed = seed_base + sub_idx

            # Frame-chaining: use last frame of previous sub-clip
            images = []
            if sub_idx > 0 and sub_paths:
                prev_path = sub_paths[-1]
                frame_path = os.path.join(self.output_dir, f"{clip_id}_sub{sub_idx - 1:02d}_lastframe.jpg")
                if extract_last_frame_ffmpeg(prev_path, frame_path):
                    images = [ImageConditioningInput(
                        path=frame_path,
                        frame_idx=0,
                        strength=1.0,
                        crf=33,
                    )]
                    gen_prompt = f"Camera continues, scene continues naturally. {prompt}"
                else:
                    log.error(f"  Could not extract last frame from {prev_path}")
                    return {"clip_id": clip_id, "status": "failed", "error": "frame extraction failed"}
            else:
                gen_prompt = prompt

            log.info(f"  Sub {sub_idx}/{ltx_count - 1} | seed={seed} | "
                     f"img_cond={'yes' if images else 'no'}")
            t0 = time.time()

            try:
                video, audio = self._generate_subclip(gen_prompt, seed, images)
                encode_video(
                    video=video,
                    fps=FRAME_RATE,
                    audio=audio,
                    output_path=sub_path,
                    video_chunks_number=1,
                )

                elapsed = time.time() - t0
                size_mb = os.path.getsize(sub_path) / 1e6
                log.info(f"  Sub {sub_idx} done in {elapsed:.1f}s ({size_mb:.1f} MB)")
                sub_paths.append(sub_path)

            except Exception as e:
                elapsed = time.time() - t0
                log.error(f"  Sub {sub_idx} failed after {elapsed:.1f}s: {e}")
                return {"clip_id": clip_id, "status": "failed", "error": str(e)}

        # Concatenate and TRIM to exact target duration (no stretching!)
        if not concat_subclips(sub_paths, final_path, target_dur):
            return {"clip_id": clip_id, "status": "failed", "error": "concat/trim failed"}

        actual_dur = probe_duration(final_path)
        clip_elapsed = time.time() - clip_start

        # Cleanup sub-clips
        for sp in sub_paths:
            if os.path.exists(sp):
                os.remove(sp)
        for si in range(ltx_count):
            lf = os.path.join(self.output_dir, f"{clip_id}_sub{si:02d}_lastframe.jpg")
            if os.path.exists(lf):
                os.remove(lf)

        # Upload to B2 (non-blocking on failure)
        if self.b2_key_id:
            self._authorize_b2()
            upload_to_b2(final_path, clip_id, self.b2_key_id, self.b2_app_key)

        return {
            "clip_id": clip_id,
            "status": "complete",
            "path": final_path,
            "target_duration": target_dur,
            "actual_duration": actual_dur,
            "sub_clips": ltx_count,
            "generation_time": clip_elapsed,
            "seed": seed_base,
        }

    def generate_all(self, prompts, start_at=0):
        """Generate all video clips, update OTIO timeline with clips and quality metadata.

        prompts: list of prompt dicts (from OTIO export or prompt_generator)
        start_at: resume from this clip index
        """
        self._load_pipeline()

        import torch

        log.info(f"\n{'='*60}")
        log.info(f"VIDEO GENERATION — LTX-2.3 Single-Process")
        log.info(f"{'='*60}")
        log.info(f"Clips: {len(prompts)} | Output: {self.output_dir}")

        results = []
        gen_count = 0
        start_time = time.time()

        for idx, prompt_data in enumerate(prompts):
            if idx < start_at:
                results.append({"clip_id": prompt_data["clip_id"], "status": "skipped"})
                continue

            clip_id = prompt_data["clip_id"]
            log.info(f"\n[{idx + 1}/{len(prompts)}] {clip_id} | "
                     f"{prompt_data['target_duration_sec']}s | "
                     f"{prompt_data.get('ltx_clips_needed', 1)} sub-clips")

            result = self.generate_clip(prompt_data)
            results.append(result)

            if result["status"] == "complete":
                gen_count += 1

            # Save progress
            progress = {
                "total": len(prompts),
                "processed": idx + 1,
                "completed": sum(1 for r in results if r["status"] == "complete"),
                "failed": sum(1 for r in results if r["status"] == "failed"),
                "skipped": sum(1 for r in results if r["status"] == "skipped"),
                "elapsed_sec": time.time() - start_time,
                "results": results,
            }
            with open(os.path.join(self.output_dir, "generation_progress.json"), "w") as f:
                json.dump(progress, f, indent=2)

        # Update OTIO timeline with generated clips and quality metadata
        self._update_otio(prompts, results)

        elapsed = time.time() - start_time
        completed = sum(1 for r in results if r["status"] == "complete")
        log.info(f"\n{'='*60}")
        log.info(f"GENERATION COMPLETE: {completed}/{len(prompts)} clips")
        log.info(f"Time: {elapsed:.0f}s ({elapsed / 60:.1f} min)")
        log.info(f"{'='*60}")

        return results

    def _update_otio(self, prompts, results):
        """Update OTIO timeline with generated video clips and quality/generation metadata."""
        from pipeline.otio_timeline import OTIOTimeline

        otio_tl = OTIOTimeline(self.otio_path)
        otio_tl.load()

        # Group results by scene
        scene_clips = {}
        for prompt_data, result in zip(prompts, results):
            if result["status"] != "complete":
                continue

            scene_num = prompt_data["scene_number"]
            if scene_num not in scene_clips:
                scene_clips[scene_num] = []

            scene_clips[scene_num].append({
                "clip_id": result["clip_id"],
                "video_path": result["path"],
                "available_duration_sec": result.get("actual_duration", 5.0),
                "trimmed_duration_sec": prompt_data["target_duration_sec"],
                "prompt": prompt_data["prompt"][:500],
                "prompt_metadata": {
                    "shot_type": prompt_data.get("shot_type", ""),
                    "environment": prompt_data.get("environment", ""),
                    "camera_movement": prompt_data.get("camera_movement", ""),
                },
            })

        # Replace video gaps with actual clips
        for scene_num, clips in sorted(scene_clips.items()):
            otio_tl.replace_video_gap_with_clips(scene_num, clips)
            log.info(f"OTIO: Scene {scene_num} — placed {len(clips)} video clips")

        # Write quality and generation metadata onto each clip
        for prompt_data, result in zip(prompts, results):
            clip_id = result.get("clip_id", prompt_data.get("clip_id"))

            if result["status"] == "complete":
                # Write generation parameters for reproducibility
                otio_tl.set_clip_generation_metadata(
                    clip_id=clip_id,
                    seed=result.get("seed"),
                    inference_steps=NUM_INFERENCE_STEPS,
                    cfg_scale=VIDEO_GUIDER_CFG["cfg_scale"],
                    generation_time=result.get("generation_time"),
                )
                # Set initial quality score (1.0 = generated successfully)
                otio_tl.set_clip_quality(clip_id, quality_score=1.0)

            elif result["status"] == "failed":
                # Mark failed clips for regeneration
                otio_tl.mark_clip_for_regeneration(
                    clip_id, reason=result.get("error", "generation failed")
                )

        otio_tl.save()
        log.info(f"OTIO timeline saved with quality metadata: {self.otio_path}")


def main():
    """CLI entry point for video generation on Vast.ai."""
    import argparse

    parser = argparse.ArgumentParser(description="LTX-2.3 video generation with OTIO integration")
    parser.add_argument("--manifest", required=True, help="Prompts JSON file (exported from OTIO)")
    parser.add_argument("--otio", required=True, help="Path to .otio timeline")
    parser.add_argument("--output-dir", default="/workspace/outputs", help="Output directory")
    parser.add_argument("--gpu", type=int, default=None, help="GPU index")
    parser.add_argument("--start-at", type=int, default=0, help="Resume from clip index")
    parser.add_argument("--b2-key-id", default=None, help="B2 key ID for upload")
    parser.add_argument("--b2-app-key", default=None, help="B2 app key for upload")
    parser.add_argument("--ltx-checkpoint", default=None, help="LTX model path")
    parser.add_argument("--gemma-root", default=None, help="Gemma model path")
    args = parser.parse_args()

    if args.gpu is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)

    with open(args.manifest) as f:
        prompts = json.load(f)

    gen = VideoGenerator(
        otio_path=args.otio,
        output_dir=args.output_dir,
        b2_key_id=args.b2_key_id,
        b2_app_key=args.b2_app_key,
        ltx_checkpoint=args.ltx_checkpoint,
        gemma_root=args.gemma_root,
    )
    gen.generate_all(prompts, start_at=args.start_at)


if __name__ == "__main__":
    main()
