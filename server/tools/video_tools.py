"""
Video generation tools -- LTX-2.3 + ffprobe wrappers.

Generates video clips using LTX-2.3 on GPU VM.  In simulation mode
(activated via ``testing.simulation_bridge``), the ADK EnvironmentSimulationConfig
intercepts calls and returns mock responses.

Rules:
- ARCH-F3 (#164): ``duration_sec`` is honoured EXACTLY. No trim margin,
  no overshoot. On the worker side the GPU renders at the requested
  duration; on the client side we verify the file's measured length is
  within :data:`callbacks.strict_assembler.CLIP_LENGTH_TOLERANCE_SEC`
  of the request and raise :class:`ClipLengthMismatchError` INSIDE
  ``_call_gpu_worker`` otherwise so ``execute_with_recovery`` routes
  through REPLACE (regenerate). The assembler never trims.
- bf16 only, no FP8, no quantization
- All subprocess calls use list form (no shell=True)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
from urllib.request import Request, urlopen

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_OUTPUT_BASE = os.environ.get(
    "VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video"
)
from testing.simulation_bridge import simulated

# ARCH-F3 (#164): ``_TRIM_MARGIN`` was removed. Video generation now
# honours the exact target ``duration_sec`` requested by the timeline;
# no 15% overshoot, no trim pass.  Length mismatches at probe time are
# raised as ``ClipLengthMismatchError`` inside ``_call_gpu_worker`` so
# the recovery ladder triggers REPLACE (regenerate) rather than a
# silent trim.

# Round-robin state for distributing work across multiple GPU workers
_worker_lock = threading.Lock()
_worker_index = 0


def _get_next_worker_url() -> str:
    """Get the next GPU worker URL using health-aware dispatch.

    Priority order:
    1. InfraAgent healthy workers — health-aware, no queue side effects
    2. Round-robin from VIDEO_WORKER_URLS — blind fallback
    3. Single worker from VIDEO_WORKER_URL / GPU_WORKER_URL
    """
    global _worker_index

    # 1. Try InfraAgent healthy workers (health-aware, no queue side effects)
    try:
        from infra_agent import WorkerRole, get_infra_agent
        agent = get_infra_agent()
        if agent:
            healthy = agent.get_healthy_workers(role=WorkerRole.VIDEO)
            if healthy:
                with _worker_lock:
                    url = healthy[_worker_index % len(healthy)]
                    _worker_index += 1
                return url
    except ImportError:
        pass
    except Exception as e:
        logger.warning("InfraAgent worker lookup failed, falling back to env vars: %s", e)

    # 2. Round-robin from env vars (blind fallback)
    urls_str = os.environ.get("VIDEO_WORKER_URLS", "")
    if urls_str:
        urls = [u.strip() for u in urls_str.split(",") if u.strip()]
        if urls:
            with _worker_lock:
                url = urls[_worker_index % len(urls)]
                _worker_index += 1
            return url

    # 3. Single worker fallback
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


@simulated("generate_video_clip")
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
        duration_sec: Target duration in seconds.  ARCH-F3 (#164): this
            is honoured EXACTLY -- the generator does not overshoot and
            the assembler does not trim.  A measured duration outside
            :data:`callbacks.strict_assembler.CLIP_LENGTH_TOLERANCE_SEC`
            of this value triggers REPLACE via the recovery ladder.
        lora_id: LoRA style identifier.
        lora_weight: LoRA weight (0.0-1.0).
        output_path: Path for the output MP4 file.
        negative_prompt: Per-clip negative prompt from visual_style.avoid.
        visual_style: Movie-level visual style description for QA enforcement.

    Returns:
        JSON string with generation results.
    """
    # ARCH-F3: exact target duration. No _TRIM_MARGIN overshoot.
    actual_duration = duration_sec

    # Production mode: call LTX-2.3 on GPU worker
    # ARCHITECTURE INVARIANT: Video generation MUST use a real GPU worker.
    # Never fall back to solid-color placeholder — that produces garbage that
    # wastes all downstream assembly time and is unwatchable.
    gpu_worker_url = _get_next_worker_url()
    if not gpu_worker_url:
        from recovery import escalate_pipeline_error
        _no_worker_msg = (
            "No video worker URL configured. Set VIDEO_WORKER_URLS or "
            "GPU_WORKER_URL to at least one LTX-dedicated GPU VM. "
            "The pipeline MUST NOT fall back to placeholder video."
        )
        response = escalate_pipeline_error(
            operation_name="video_worker_missing",
            error_msg=_no_worker_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="No GPU worker is provisioned or healthy.",
            agent_policy_type="production",
        )
        if response.get("action") != "skip":
            raise RuntimeError(_no_worker_msg)
        return json.dumps({
            "status": "error",
            "error": "Video generation skipped — no GPU worker available",
        })

    # Calculate frame count: LTX-2.3 works with 8k+1 frames at 24fps
    fps = 24
    raw_frames = int(actual_duration * fps)
    num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    # Deterministic seed derived from prompt — each clip gets a unique but reproducible seed
    seed = int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16) % (2**31)

    video_url = f"{gpu_worker_url.rstrip('/')}/video"

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

            # ── PERSIST FIRST ── save to disk + B2 before any QA gate ──
            # Every generated clip is persisted for inspection regardless
            # of QA outcome.  This happens INSIDE _call_gpu_worker so that
            # QA rejection errors are still caught by execute_with_recovery
            # and routed through the human escalation path (L4).
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(output_path, "wb") as _f:
                _f.write(result_bytes)

            # Decode QA reason for status + error messages
            import base64 as _b64
            try:
                _qa_reason = _b64.b64decode(qa_reason_raw).decode("utf-8") if qa_reason_raw else ""
            except Exception:
                _qa_reason = qa_reason_raw

            _qa_attempts = int(resp.headers.get("X-QA-Attempts", "1"))
            _qa_seed = int(resp.headers.get("X-QA-Seed", str(seed)))

            # Write per-clip status.json
            _clip_dir = os.path.dirname(output_path) or "."
            _clip_name = os.path.splitext(os.path.basename(output_path))[0]
            _status_path = os.path.join(_clip_dir, f"{_clip_name}_status.json")
            try:
                with open(_status_path, "w") as _sf:
                    json.dump({
                        "quality": qa_quality, "qa_reason": _qa_reason,
                        "attempts": _qa_attempts, "seed": _qa_seed,
                        "status": "completed", "prompt_preview": prompt[:200],
                    }, _sf, indent=2)
            except OSError:
                pass

            # Upload to B2 immediately
            try:
                from tools.b2_checkpoint import upload_video_clip
                upload_video_clip(output_path, _status_path)
            except Exception as _b2_err:
                logger.warning("B2 upload failed for %s: %s", output_path, _b2_err)

            # ── ARCH-F3 (#164) LENGTH GATE ──────────────────────────
            # Verify the generated clip honours the exact requested
            # duration within CLIP_LENGTH_TOLERANCE_SEC. A mismatch is
            # a generation-time bug, NOT a render-time fixup: we raise
            # ClipLengthMismatchError INSIDE the recovery context so
            # execute_with_recovery routes through REPLACE (regenerate)
            # rather than letting a wrong-length clip into the timeline.
            from callbacks.strict_assembler import ensure_clip_length_matches
            try:
                _probe_json = json.loads(probe_clip(output_path))
                _measured = float(_probe_json.get("duration", 0.0))
            except Exception as _probe_exc:  # noqa: BLE001 - probe is best-effort here
                logger.warning(
                    "probe_clip failed during length gate (non-fatal signal, "
                    "treating as zero): %s", _probe_exc,
                )
                _measured = 0.0
            ensure_clip_length_matches(
                clip_id=output_path,
                declared=duration_sec,
                actual=_measured,
            )

            # ── QA GATE ── raise INSIDE recovery context so
            # non_retryable_patterns routes to human escalation (L4).
            _is_quick_test = os.environ.get(
                "DOCUMENTARY_QUICK_TEST", ""
            ).strip().lower() in ("1", "true", "yes")

            _is_auto_approve = os.environ.get(
                "DOCUMENTARY_AUTO_APPROVE", ""
            ).strip().lower() in ("1", "true", "yes")

            if qa_quality in ("rejected", "poor"):
                if _is_quick_test or _is_auto_approve:
                    logger.warning(
                        "QA %s clip %s (%s — accepting): %s",
                        qa_quality.upper(),
                        output_path,
                        "quick-test" if _is_quick_test else "auto-approve",
                        _qa_reason[:200],
                    )
                    qa_quality = "rejected_accepted"
                    # Re-write status.json so persisted quality matches
                    # the pipeline's actual decision (rejected_accepted,
                    # not the raw quality written earlier).
                    try:
                        with open(_status_path, "w") as _sf2:
                            json.dump({
                                "quality": qa_quality, "qa_reason": _qa_reason,
                                "attempts": _qa_attempts, "seed": _qa_seed,
                                "status": "completed", "prompt_preview": prompt[:200],
                            }, _sf2, indent=2)
                    except OSError:
                        pass
                else:
                    # Raise a retryable error that the recovery middleware
                    # can catch. Include QA_HINTS so the creative amendment
                    # (_video_amend_prompt_with_qa_hints) can inject
                    # corrective guidance into the prompt for the next attempt.
                    #
                    # NOTE: Do NOT escalate here — execute_with_recovery
                    # wraps _call_gpu_worker and will try creative amendments
                    # (seed changes, prompt tweaks, step adjustments) first.
                    # Human escalation happens at L4 of the recovery ladder
                    # after all automated retries are exhausted.
                    raise RuntimeError(
                        f"QA {qa_quality.upper()}: visual quality below threshold. "
                        f"Clip saved at {output_path} for inspection. "
                        f"QA_HINTS: {_qa_reason}"
                    )

            result_meta = {
                "gen_time": float(resp.headers.get("X-Gen-Time", "0")),
                "qa_quality": qa_quality,
                "qa_reason": _qa_reason,
                "qa_attempts": _qa_attempts,
                "qa_seed": _qa_seed,
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

    # File already persisted to disk + B2 inside _call_gpu_worker.
    gen_time = gpu_result["gen_time"]
    qa_quality = gpu_result["qa_quality"]
    qa_reason = gpu_result["qa_reason"]
    qa_attempts = gpu_result["qa_attempts"]
    qa_seed = gpu_result["qa_seed"]

    # QA "unknown" — handled outside recovery context as a degraded signal.
    if qa_quality == "unknown":
        logger.warning(
            "Video clip QA returned 'unknown' for %s — treating as degraded.",
            output_path,
        )
        if os.environ.get("STRICT_QA", "").lower() in ("1", "true"):
            from recovery import escalate_pipeline_error
            _strict_msg = (
                f"Video clip QA unavailable (quality='unknown'): {qa_reason}. "
                f"STRICT_QA mode requires all clips to pass QA."
            )
            response = escalate_pipeline_error(
                operation_name="video_qa_unavailable",
                error_msg=_strict_msg,
                severity="warning",
                default_action="abort",
                diagnosis_hint="QA model could not evaluate clip quality.",
                agent_policy_type="video",
            )
            if response.get("action") != "skip":
                raise RuntimeError(_strict_msg)

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
