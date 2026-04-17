"""
Reusable clip helpers extracted from deterministic_steps.py.

Shared by both the new ProductionOrchestrator and the fallback
deterministic_production_callback, so both paths use identical
low-level logic for clip generation, OTIO timeline writes,
gatekeeper validation, and B2 uploads.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _MockToolContext — minimal mock of ADK tool_context for direct calls
# ---------------------------------------------------------------------------

class _MockToolContext:
    """Minimal mock of ADK tool_context for direct function calls."""

    def __init__(self, state: dict):
        self.state = state


# ---------------------------------------------------------------------------
# Single clip generation (thread-safe)
# ---------------------------------------------------------------------------

def generate_one_clip(
    concept: dict,
    video_dir: str,
    default_negative: str,
    visual_style_str: str,
    feedback_store: object,
) -> dict:
    """Generate a single video clip (thread-safe for parallel execution).

    This is extracted from deterministic_steps.py lines 1041-1116.
    Uses generate_video_clip() from tools/video_tools.py which already
    handles recovery middleware (VIDEO_POLICY), B2 upload, QA rejection.

    Args:
        concept: Visual concept dict with scene_num, phrase_idx, duration, prompt, etc.
        video_dir: Output directory for video files.
        default_negative: Default negative prompt from visual_style.avoid.
        visual_style_str: Movie-level visual style JSON string.
        feedback_store: AG-UI FeedbackStore for emitting artifact events.

    Returns:
        Dict with generation results (status, output_path, qa_quality, etc.).
    """
    from agui import ArtifactEvent, ArtifactStatus, ArtifactType
    from tools.video_tools import generate_video_clip

    scene_num = concept.get("scene_num", 0)
    phrase_idx = concept.get("phrase_idx", 0)
    duration = min(concept.get("duration", 5.0), 10.0)
    prompt = concept.get("prompt", "")
    lora_id = concept.get("lora_id", "documentary-realism")
    lora_weight = concept.get("lora_weight", 0.75)
    clip_negative = concept.get("negative_prompt", default_negative)
    output_path = os.path.join(
        video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}.mp4"
    )

    # Skip already-generated clips (resume support)
    status_path = os.path.join(
        video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}_status.json"
    )
    if os.path.exists(output_path) and os.path.exists(status_path):
        try:
            with open(status_path) as sf:
                prev_status = json.load(sf)
            prev_quality = prev_status.get("quality", "unknown")
            if prev_quality in ("good", "excellent", "acceptable", "rejected_accepted"):
                logger.info(
                    "Skipping scene_%03d_phrase_%03d (already generated, quality=%s)",
                    scene_num, phrase_idx, prev_quality,
                )
                return {
                    "skipped": True,
                    "output_path": output_path,
                    "scene_num": scene_num,
                    "phrase_idx": phrase_idx,
                    "duration": duration,
                    "lora_id": lora_id,
                }
        except (json.JSONDecodeError, OSError):
            pass  # re-generate if status file is corrupt

    # AG-UI: emit "generating" artifact event
    artifact_id = f"video-s{scene_num:03d}-p{phrase_idx:03d}"
    if feedback_store is None:
        logger.debug("feedback_store is None — skipping AG-UI events for %s", artifact_id)
    else:
        feedback_store.register_artifact(ArtifactEvent(
            id=artifact_id,
            artifact_type=ArtifactType.VIDEO_CLIP,
            status=ArtifactStatus.GENERATING,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
            duration_sec=duration,
            metadata={"prompt": prompt, "lora_id": lora_id},
            timestamp=time.time(),
        ))

    gen_result_json = generate_video_clip(
        prompt=prompt,
        duration_sec=duration,
        lora_id=lora_id,
        lora_weight=lora_weight,
        output_path=output_path,
        negative_prompt=clip_negative,
        visual_style=visual_style_str,
    )
    gen_result = json.loads(gen_result_json)
    gen_result["scene_num"] = scene_num
    gen_result["phrase_idx"] = phrase_idx
    gen_result["duration"] = duration
    gen_result["lora_id"] = lora_id
    gen_result["_output_path"] = output_path

    # AG-UI: update artifact with result
    if feedback_store is not None:
        qa_scores: dict = {}
        if gen_result.get("qa_quality"):
            qa_scores["quality"] = gen_result["qa_quality"]
            qa_scores["reason"] = gen_result.get("qa_reason", "")
        feedback_store.register_artifact(ArtifactEvent(
            id=artifact_id,
            artifact_type=ArtifactType.VIDEO_CLIP,
            status=ArtifactStatus.PENDING_REVIEW,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
            duration_sec=gen_result.get("actual_duration", duration),
            preview_url=output_path,
            qa_scores=qa_scores,
            metadata={"prompt": prompt, "lora_id": lora_id},
            timestamp=time.time(),
        ))

    # Per-clip B2 resume tracking (R5 from deep audit):
    # Record clip completion in a progress manifest so recovery knows
    # exactly which clips are done.  The manifest is uploaded to B2
    # after each clip, enabling resume from exact point of failure.
    try:
        _manifest_path = os.path.join(video_dir, "_clip_progress.json")
        _manifest: dict = {}
        if os.path.exists(_manifest_path):
            with open(_manifest_path) as _mf:
                _manifest = json.load(_mf)
        clip_id = f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}"
        _manifest[clip_id] = {
            "status": "completed" if not gen_result.get("error") else "failed",
            "quality": gen_result.get("qa_quality", "unknown"),
            "output_path": output_path,
            "timestamp": time.time(),
        }
        with open(_manifest_path, "w") as _mf:
            json.dump(_manifest, _mf, indent=2)
        # Upload manifest to B2 for cross-machine resume
        from tools.b2_checkpoint import upload_file
        upload_file(_manifest_path, "video/_clip_progress.json")
    except Exception as _manifest_err:
        logger.debug("Clip progress manifest update failed: %s", _manifest_err)

    return gen_result


# ---------------------------------------------------------------------------
# OTIO timeline writing (must be sequential — uses _otio_lock)
# ---------------------------------------------------------------------------

def process_results_to_otio(
    results: list[dict],
    state: dict,
    narr_durations: dict,
) -> tuple[int, int, list[str], list[dict]]:
    """Probe results and add them to the OTIO timeline.

    Extracted from deterministic_steps.py lines 1148-1231.

    Args:
        results: List of clip generation result dicts.
        state: Pipeline state dict.
        narr_durations: Narration durations by scene from OTIO.

    Returns:
        Tuple of (total_clips, skipped_clips, errors, deferred_gk_clips).
    """
    from tools.otio_tools import add_video_clip
    from tools.video_tools import probe_clip

    total_clips = 0
    skipped_clips = 0
    errors: list[str] = []
    deferred_gk_clips: list[dict] = []
    mock_ctx = _MockToolContext(state)

    for result in sorted(results, key=lambda r: (r.get("scene_num", 0), r.get("phrase_idx", 0))):
        scene_num = result.get("scene_num", 0)
        phrase_idx = result.get("phrase_idx", 0)
        duration = result.get("duration", 5.0)
        lora_id = result.get("lora_id", "documentary-realism")
        output_path = result.get("_output_path") or result.get("output_path", "")

        if result.get("skipped"):
            # Still need to add skipped clips to OTIO timeline
            try:
                probe_result_json = probe_clip(mp4_path=output_path)
                probe_result = json.loads(probe_result_json)
                actual_duration = probe_result.get("duration", duration * 1.15)
                clip_result_json = add_video_clip(
                    scene_num=scene_num,
                    phrase_idx=phrase_idx,
                    mp4_path=output_path,
                    duration=duration,
                    source_range=duration,
                    available_range=actual_duration,
                    lora_id=lora_id,
                    tool_context=mock_ctx,
                )
                clip_result = json.loads(clip_result_json)
                if "error" in clip_result:
                    raise RuntimeError(
                        f"OTIO VIOLATION: failed to add video clip "
                        f"scene {scene_num} phrase {phrase_idx}: {clip_result['error']}"
                    )
                skipped_clips += 1
                total_clips += 1
            except RuntimeError:
                raise  # OTIO violations are fatal — never swallow
            except Exception as e:
                err_msg = f"Error adding skipped scene {scene_num} phrase {phrase_idx} to timeline: {e}"
                logger.error(err_msg)
                errors.append(err_msg)
            continue

        if result.get("status") == "error":
            errors.append(
                f"scene_{scene_num}_phrase_{phrase_idx}: {result.get('error')}"
            )
            continue

        try:
            probe_result_json = probe_clip(mp4_path=output_path)
            probe_result = json.loads(probe_result_json)
            actual_duration = probe_result.get("duration", duration * 1.15)

            # B2 upload already happened inside generate_video_clip().
            # Gatekeeper runs AFTER all clips are in B2 + OTIO (audit trail).
            scene_phrases = narr_durations.get(scene_num, [])
            expected_dur = (
                scene_phrases[phrase_idx][1]
                if phrase_idx < len(scene_phrases)
                else duration
            )
            deferred_gk_clips.append({
                "mp4_path": output_path,
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "source_range": duration,
                "expected_duration": expected_dur,
            })

            clip_result_json = add_video_clip(
                scene_num=scene_num,
                phrase_idx=phrase_idx,
                mp4_path=output_path,
                duration=duration,
                source_range=duration,
                available_range=actual_duration,
                lora_id=lora_id,
                tool_context=mock_ctx,
            )
            clip_result = json.loads(clip_result_json)
            if "error" in clip_result:
                raise RuntimeError(
                    f"OTIO VIOLATION: failed to add video clip "
                    f"scene {scene_num} phrase {phrase_idx}: {clip_result['error']}"
                )
            total_clips += 1
        except RuntimeError:
            raise  # OTIO violations are fatal — never swallow
        except Exception as e:
            err_msg = f"Error adding scene {scene_num} phrase {phrase_idx} to timeline: {e}"
            logger.error(err_msg)
            errors.append(err_msg)

    return total_clips, skipped_clips, errors, deferred_gk_clips


# ---------------------------------------------------------------------------
# Gatekeeper batch validation + B2 upload
# ---------------------------------------------------------------------------

def run_gatekeeper_and_upload(
    state: dict,
    deferred_gk_clips: list[dict],
) -> None:
    """Run gatekeeper validation on all clips and upload results to B2.

    Extracted from deterministic_steps.py lines 1233-1274.

    Raises:
        RuntimeError: If gatekeeper rejects any clips.
    """
    from gatekeeper import (
        check_video_clip,
        format_audit_report,
        has_rejects,
    )
    from tools.b2_checkpoint import (
        upload_gatekeeper_report,
        upload_pipeline_state,
        upload_stage_marker,
        upload_timeline,
    )

    # Upload production artifacts to B2 — artifacts FIRST, then gatekeeper,
    # then stage marker.
    from callbacks.state_manager import safe_state_dict
    b2_ok = upload_pipeline_state(safe_state_dict(state))
    tp = state.get("_timeline_path", "")
    if tp and os.path.exists(tp):
        upload_timeline(tp)
    # NOTE: stage marker is uploaded AFTER gatekeeper validation below.

    # GATEKEEPER: batch validation AFTER all artifacts are in B2.
    all_gk_checks = []
    for clip_info in deferred_gk_clips:
        gk_checks = check_video_clip(
            mp4_path=clip_info["mp4_path"],
            scene_num=clip_info["scene_num"],
            phrase_idx=clip_info["phrase_idx"],
            source_range=clip_info["source_range"],
            expected_duration=clip_info["expected_duration"],
            stage="production",
        )
        all_gk_checks.extend(gk_checks)

    # Upload gatekeeper audit report to B2 (audit trail)
    if all_gk_checks:
        audit_report = format_audit_report(all_gk_checks, "production")
        upload_gatekeeper_report(audit_report, "production")

    # NOW evaluate rejects — everything is safely in B2
    if has_rejects(all_gk_checks):
        rejects = [c for c in all_gk_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        raise RuntimeError(
            f"GATEKEEPER REJECT (production stage, {len(rejects)} reject(s) — "
            f"audit report uploaded to B2): {reject_msgs}"
        )

    # Stage marker AFTER gatekeeper passes
    if b2_ok:
        upload_stage_marker("production")


# ---------------------------------------------------------------------------
# Post-production steps (timeline guardian, infra, approval gate)
# ---------------------------------------------------------------------------

def run_post_production(
    callback_context: object,
    num_workers: int,
    total_clips: int,
    skipped_clips: int,
    errors: list[str],
) -> str:
    """Run post-production steps: timeline guardian, infra notify, approval gate.

    Extracted from deterministic_steps.py lines 1276-1318.

    Returns:
        Summary string for the pipeline log.
    """
    from callbacks.approval_gate import mark_stage_ready
    from callbacks.timeline_guardian import timeline_guardian_callback
    from infra_agent import get_infra_agent

    summary_parts = [
        f"Production complete: {total_clips} video clips generated and added to timeline.",
    ]
    if skipped_clips:
        summary_parts.append(f"Skipped {skipped_clips} already-generated clips (resume).")
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:3])}")
    summary_parts.append(f"Workers used: {num_workers}.")

    logger.info("Production: %d clips generated", total_clips)

    # TIMELINE GUARDIAN: run explicitly after production.
    try:
        timeline_guardian_callback(callback_context)
        logger.info("Timeline Guardian passed after production")
    except RuntimeError as e:
        logger.error("Timeline Guardian FAILED after production: %s", e)
        # Route through escalation system instead of crashing
        try:
            from recovery import escalate_pipeline_error
            from callbacks.state_manager import safe_state_dict
            _state_d = safe_state_dict(callback_context.state)
            action = escalate_pipeline_error(
                operation_name="timeline_guardian_production",
                error_msg=str(e),
                severity="critical",
                pipeline_state=_state_d,
                agent_policy_type="otio",
            )
            if isinstance(action, dict) and action.get("action") == "abort":
                raise
            logger.warning(
                "Timeline Guardian production failure escalated and resolved "
                "with action=%s — continuing pipeline",
                action,
            )
        except ImportError:
            raise e

    # INFRA: notify stage complete
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_complete("production")

    # APPROVAL GATE: mark clips ready for human review.
    mark_stage_ready("clips")
    logger.info("Production: marked clips stage ready for approval")

    return "\n".join(summary_parts)
