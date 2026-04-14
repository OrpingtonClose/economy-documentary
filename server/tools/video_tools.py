"""
Video generation tools -- LTX-2.3 + ffprobe wrappers.

For production: generates video clips using LTX-2.3 on GPU VM.
For test run: generates solid-color MP4 files with correct duration using ffmpeg.

Rules:
- Duration should be target_duration * 1.15 (15% longer for trim margin)
- bf16 only, no FP8, no quantization
- All subprocess calls use list form (no shell=True)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_OUTPUT_BASE = os.environ.get(
    "VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video"
)
_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")
_TRIM_MARGIN = 1.15  # 15% longer for trim margin

# Round-robin state for distributing work across multiple GPU workers
_worker_lock = threading.Lock()
_worker_index = 0


def _get_next_worker_url() -> str:
    """Get the next GPU worker URL using round-robin distribution.

    Reads VIDEO_WORKER_URLS (comma-separated) for multiple workers,
    falls back to VIDEO_WORKER_URL or GPU_WORKER_URL for single worker.
    """
    global _worker_index

    # Check for multiple workers first
    urls_str = os.environ.get("VIDEO_WORKER_URLS", "")
    if urls_str:
        urls = [u.strip() for u in urls_str.split(",") if u.strip()]
        if urls:
            with _worker_lock:
                url = urls[_worker_index % len(urls)]
                _worker_index += 1
            return url

    # Single worker fallback
    return os.environ.get("VIDEO_WORKER_URL", "") or os.environ.get("GPU_WORKER_URL", "")


def _generate_solid_color_mp4(
    output_path: str,
    duration: float,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    color: str = "0x336699",
) -> bool:
    """Generate a solid-color MP4 file using ffmpeg (for testing)."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"color=c={color}:s={width}x{height}:d={duration:.2f}:r={fps}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-t", f"{duration:.2f}",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error("ffmpeg failed: %s", e)
        return False


def generate_video_clip(
    prompt: str,
    duration_sec: float,
    lora_id: str,
    lora_weight: float,
    output_path: str,
    negative_prompt: str = "",
    visual_style: str = "",
    tool_context=None,
) -> str:
    """Generate a video clip using LTX-2.3.

    Args:
        prompt: Visual description prompt for video generation.
        duration_sec: Target duration in seconds (will be extended by 15%).
        lora_id: LoRA style identifier.
        lora_weight: LoRA weight (0.0-1.0).
        output_path: Path for the output MP4 file.
        negative_prompt: Per-clip negative prompt from visual_style.avoid.
        visual_style: Movie-level visual style description for QA enforcement.

    Returns:
        JSON string with generation results.
    """
    actual_duration = duration_sec * _TRIM_MARGIN

    if _TEST_MODE:
        success = _generate_solid_color_mp4(output_path, actual_duration)
        if not success:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Failed to generate test video via ffmpeg",
                }
            )

        logger.info(
            "Test mode: generated solid-color MP4 %s (%.2fs)",
            output_path,
            actual_duration,
        )
        return json.dumps(
            {
                "status": "generated",
                "mode": "test",
                "output_path": output_path,
                "target_duration": round(duration_sec, 2),
                "actual_duration": round(actual_duration, 2),
                "lora_id": lora_id,
                "lora_weight": lora_weight,
                "resolution": "1280x720",
                "fps": 24,
            }
        )

    # Production mode: call LTX-2.3 on GPU worker
    # ARCHITECTURE INVARIANT: Video generation MUST use a real GPU worker.
    # Never fall back to solid-color placeholder — that produces garbage that
    # wastes all downstream assembly time and is unwatchable.
    gpu_worker_url = _get_next_worker_url()
    if not gpu_worker_url:
        raise RuntimeError(
            "No video worker URL configured. Set VIDEO_WORKER_URLS or "
            "GPU_WORKER_URL to at least one LTX-dedicated GPU VM. "
            "The pipeline MUST NOT fall back to placeholder video."
        )

    # Calculate frame count: LTX-2.3 works with 8k+1 frames at 24fps
    fps = 24
    raw_frames = int(actual_duration * fps)
    num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    # Deterministic seed derived from prompt — each clip gets a unique but reproducible seed
    seed = int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16) % (2**31)

    payload = json.dumps({
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "visual_style": visual_style,
        "duration_sec": actual_duration,
        "width": 512,
        "height": 320,
        "num_frames": num_frames,
        "seed": seed,
        # LTX-2.3 official parameters (from dg845/LTX-2.3-Diffusers example):
        "num_inference_steps": 30,   # LTX-2.3 dev: 30 steps
        "guidance_scale": 3.0,       # LTX-2.3 dev: CFG=3.0
        "stg_scale": 1.0,            # spatio-temporal guidance
        "modality_scale": 3.0,       # modality (video vs audio) guidance
        "guidance_rescale": 0.7,     # guidance rescale factor
        "stg_blocks": [28],          # STG block indices
    }).encode("utf-8")

    video_url = f"{gpu_worker_url.rstrip('/')}/video"
    req = Request(video_url, data=payload, headers={"Content-Type": "application/json"})

    # Use graduated recovery middleware instead of ad-hoc retry loops.
    # The middleware handles: retry → creative amendment → env assessment → human escalation.
    # Build payload from logical params inside the function so creative amendments
    # (e.g. _video_amend_seed, _video_amend_steps) actually reach the GPU worker.
    def _call_gpu_worker(
        url=video_url,
        prompt=prompt,
        negative_prompt=negative_prompt,
        visual_style=visual_style,
        duration_sec=actual_duration,
        width=512,
        height=320,
        num_frames=num_frames,
        seed=seed,
        num_inference_steps=30,
        guidance_scale=3.0,
        stg_scale=1.0,
        modality_scale=3.0,
        guidance_rescale=0.7,
        stg_blocks=None,
    ):
        inner_payload = json.dumps({
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "visual_style": visual_style,
            "duration_sec": duration_sec,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "seed": seed,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "stg_scale": stg_scale,
            "modality_scale": modality_scale,
            "guidance_rescale": guidance_rescale,
            "stg_blocks": stg_blocks if stg_blocks is not None else [28],
        }).encode("utf-8")
        req_inner = Request(url, data=inner_payload, headers={"Content-Type": "application/json"})
        with urlopen(req_inner, timeout=3600) as resp:  # 60 min: 3 QA retries × 30 steps + Qwen-Omni
            result_bytes = resp.read()
            qa_quality = resp.headers.get("X-QA-Quality", "unknown")
            qa_reason_raw = resp.headers.get("X-QA-Reason", "")

            # REJECTED = fundamentally broken output (grid artifacts, corrupted
            # data, body horror, overt AI wonk).  Raise INSIDE the recovery
            # context so non_retryable_patterns can route to human escalation.
            if qa_quality == "rejected":
                import base64 as _b64
                try:
                    reason = _b64.b64decode(qa_reason_raw).decode("utf-8")
                except Exception:
                    reason = qa_reason_raw
                raise RuntimeError(
                    f"QA REJECTED: clip is fundamentally broken and cannot be used. "
                    f"Reason: {reason}"
                )

            result_meta = {
                "mp4_bytes": result_bytes,
                "gen_time": float(resp.headers.get("X-Gen-Time", "0")),
                "qa_quality": qa_quality,
                "qa_reason_raw": qa_reason_raw,
                "qa_attempts": int(resp.headers.get("X-QA-Attempts", "1")),
                "qa_seed": int(resp.headers.get("X-QA-Seed", str(seed))),
            }
            return result_meta

    from recovery import execute_with_recovery, VIDEO_POLICY
    gpu_result = execute_with_recovery(
        operation=_call_gpu_worker,
        operation_name=f"video_gen_scene{prompt}",
        kwargs={
            "url": video_url,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "visual_style": visual_style,
            "duration_sec": actual_duration,
            "width": 512,
            "height": 320,
            "num_frames": num_frames,
            "seed": seed,
            "num_inference_steps": 30,
            "guidance_scale": 3.0,
            "stg_scale": 1.0,
            "modality_scale": 3.0,
            "guidance_rescale": 0.7,
            "stg_blocks": [28],
        },
        policy=VIDEO_POLICY,
        context={"prompt": prompt, "duration": actual_duration},
    )

    # If recovery returned None (human chose "skip"), return error status
    if gpu_result is None:
        return json.dumps({
            "status": "error",
            "error": "Video generation skipped by human decision during recovery",
        })

    mp4_bytes = gpu_result["mp4_bytes"]
    gen_time = gpu_result["gen_time"]
    qa_quality = gpu_result["qa_quality"]
    _raw_reason = gpu_result["qa_reason_raw"]
    try:
        qa_reason = base64.b64decode(_raw_reason).decode("utf-8") if _raw_reason else ""
    except Exception:
        qa_reason = _raw_reason  # fallback: use raw value
    qa_attempts = gpu_result["qa_attempts"]
    qa_seed = gpu_result["qa_seed"]

    # GAP 3.1: Client-side QA rejection gate — never accept poor-quality clips
    if qa_quality == "poor":
        raise RuntimeError(
            f"Video clip REJECTED by QA (quality='poor'): {qa_reason}. "
            f"Clip: {output_path}. The pipeline MUST NOT accept poor-quality "
            f"clips — they waste all downstream assembly time."
        )
    if qa_quality == "unknown":
        logger.warning(
            "Video clip QA returned 'unknown' for %s — "
            "this means QA failed to evaluate. Treating as degraded.",
            output_path,
        )
        if os.environ.get("STRICT_QA", "").lower() in ("1", "true"):
            raise RuntimeError(
                f"Video clip QA unavailable (quality='unknown'): {qa_reason}. "
                f"STRICT_QA mode requires all clips to pass QA."
            )

    # Video downloaded successfully — write to disk
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(mp4_bytes)

    # Write per-clip status.json (bearnaise pattern)
    clip_dir = os.path.dirname(output_path) or "."
    clip_name = os.path.splitext(os.path.basename(output_path))[0]
    status_path = os.path.join(clip_dir, f"{clip_name}_status.json")
    clip_status = {
        "quality": qa_quality,
        "qa_reason": qa_reason,
        "attempts": qa_attempts,
        "seed": qa_seed,
        "status": "completed",
        "prompt_preview": prompt[:200],
        "prompt_full": prompt,
    }
    try:
        with open(status_path, "w") as sf:
            json.dump(clip_status, sf, indent=2)
        logger.info("Wrote clip status: %s (quality=%s)", status_path, qa_quality)
    except OSError as e:
        logger.warning("Failed to write clip status %s: %s", status_path, e)

    # Upload video clip + QA status to B2 immediately after creation
    try:
        from tools.b2_checkpoint import upload_video_clip
        upload_video_clip(output_path, status_path)
    except Exception as b2_err:
        logger.warning("B2 upload failed for video clip %s: %s", output_path, b2_err)

    # Probe the generated clip for actual duration (best-effort, never overwrites video)
    actual_dur = actual_duration
    try:
        probe_result = json.loads(probe_clip(output_path))
        actual_dur = probe_result.get("duration", actual_duration)
    except Exception as probe_exc:
        logger.warning("probe_clip failed (non-fatal): %s", probe_exc)

    logger.info(
        "Generated video clip %s (%.2fs, gen=%.1fs, lora=%s@%.2f, qa=%s)",
        output_path, actual_dur, gen_time, lora_id, lora_weight, qa_quality,
    )
    return json.dumps(
        {
            "status": "generated",
            "mode": "production",
            "output_path": output_path,
            "target_duration": round(duration_sec, 2),
            "actual_duration": round(actual_dur, 2),
            "lora_id": lora_id,
            "lora_weight": lora_weight,
            "prompt_preview": prompt[:200],
            "prompt_full": prompt,
            "gen_time": round(gen_time, 2),
            "num_frames": num_frames,
            "resolution": "512x320",
            "qa_quality": qa_quality,
            "qa_reason": qa_reason,
            "qa_attempts": qa_attempts,
            "qa_seed": qa_seed,
        }
    )


def probe_clip(mp4_path: str, tool_context=None) -> str:
    """Probe an MP4 file for duration, resolution, and FPS using ffprobe.

    Args:
        mp4_path: Path to the MP4 file.

    Returns:
        JSON string with clip metadata.
    """
    if not os.path.exists(mp4_path):
        return json.dumps({"error": f"File not found: {mp4_path}"})

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        mp4_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return json.dumps(
                {
                    "error": f"ffprobe failed (rc={result.returncode})",
                    "stderr": result.stderr[:500],
                }
            )

        probe_data = json.loads(result.stdout)

        # Extract info from first video stream
        video_stream = None
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_stream = stream
                break

        duration = float(probe_data.get("format", {}).get("duration", 0))
        width = int(video_stream.get("width", 0)) if video_stream else 0
        height = int(video_stream.get("height", 0)) if video_stream else 0
        fps_str = video_stream.get("r_frame_rate", "0/1") if video_stream else "0/1"

        # Parse fractional FPS
        fps_parts = fps_str.split("/")
        if len(fps_parts) == 2 and int(fps_parts[1]) > 0:
            fps = round(int(fps_parts[0]) / int(fps_parts[1]), 2)
        else:
            fps = float(fps_parts[0]) if fps_parts[0] else 0.0

        return json.dumps(
            {
                "mp4_path": mp4_path,
                "duration": round(duration, 3),
                "width": width,
                "height": height,
                "fps": fps,
                "resolution": f"{width}x{height}",
            }
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ffprobe timed out"})
    except (json.JSONDecodeError, ValueError) as e:
        return json.dumps({"error": f"ffprobe output parse error: {e}"})


# -- ADK FunctionTool wrappers -------------------------------------------------
generate_video_clip_tool = FunctionTool(generate_video_clip)
probe_clip_tool = FunctionTool(probe_clip)

video_tools = [generate_video_clip_tool, probe_clip_tool]
