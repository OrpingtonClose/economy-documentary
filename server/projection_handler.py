"""Apply effects to OTIO timeline. OTIO is a read model rebuilt from events.

This is the ONLY file that writes to OTIO. Agents never touch OTIO directly.
Each effect type has exactly one handler function.
"""

from __future__ import annotations

import subprocess

import opentimelineio as otio

from effects import (
    Effect,
    ExecuteRawBash,
    GenerateNarrationAudio,
    MergeIntoOTIO,
    NoOp,
    RenderVideoSegment,
    UpdateScript,
)


def apply_event(timeline: otio.schema.Timeline, effect: Effect) -> otio.schema.Timeline:
    """Apply a single effect to the timeline. Returns a new timeline.

    This is a pure function (except ExecuteRawBash which runs subprocess).
    The timeline is rebuilt from events, not modified in place.
    """
    handlers: dict[str, callable] = {
        "UpdateScript": _handle_update_script,
        "GenerateNarrationAudio": _handle_generate_audio,
        "RenderVideoSegment": _handle_render_video,
        "MergeIntoOTIO": _handle_merge_into_otio,
        "ExecuteRawBash": _handle_execute_bash,
        "NoOp": _handle_noop,
    }
    handler = handlers.get(effect.effect_type)
    if handler is None:
        return timeline
    return handler(timeline, effect)


def _handle_update_script(timeline: otio.schema.Timeline, effect: UpdateScript) -> otio.schema.Timeline:
    """Write script changes to pipeline metadata directly on the timeline object."""
    meta = timeline.metadata.setdefault("documentary", {})
    prov = meta.setdefault("_provenance", {})

    if effect.narration_v1:
        meta["narration_v1"] = effect.narration_v1
        prov["narration_v1"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    if effect.narration_v2:
        meta["narration_v2"] = effect.narration_v2
        prov["narration_v2"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    if effect.narration_v3:
        meta["narration_v3"] = effect.narration_v3
        prov["narration_v3"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    if effect.visual_notes:
        meta["visual_notes"] = effect.visual_notes
        prov["visual_notes"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    if effect.dopamine_hook:
        meta["dopamine_hook"] = effect.dopamine_hook
        prov["dopamine_hook"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    if effect.pronunciation_hints:
        meta["pronunciation_hints"] = effect.pronunciation_hints
        prov["pronunciation_hints"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    if effect.duration_sec:
        meta["duration_sec"] = effect.duration_sec
        prov["duration_sec"] = {"agent": effect.agent_id, "scene": effect.scene_num}
    return timeline


def _handle_generate_audio(timeline: otio.schema.Timeline, effect: GenerateNarrationAudio) -> otio.schema.Timeline:
    """Create a job in the audio queue."""
    from job_queue import create_job
    from models.job import JobType

    create_job(
        job_type=JobType.NARRATION,
        stage="audio",
        scene_num=effect.scene_num,
        payload={"voice": effect.voice, "text": effect.text},
    )
    return timeline


def _handle_render_video(timeline: otio.schema.Timeline, effect: RenderVideoSegment) -> otio.schema.Timeline:
    """Create a job in the video queue."""
    from job_queue import create_job
    from models.job import JobType

    create_job(
        job_type=JobType.VIDEO_RENDER,
        stage="video",
        scene_num=effect.scene_num,
        payload={"prompt": effect.prompt, "lora_id": effect.lora_id},
    )
    return timeline


def _handle_merge_into_otio(timeline: otio.schema.Timeline, effect: MergeIntoOTIO) -> otio.schema.Timeline:
    """Add clips to OTIO tracks."""
    from tools.otio_tools import add_narration_to_timeline, add_video_clip_simple

    for clip in effect.audio_clips:
        add_narration_to_timeline(
            scene_num=clip.get("scene_num", 0),
            voice=clip.get("voice", "V1"),
            wav_path=clip.get("wav_path", ""),
            duration=clip.get("duration_sec", 5.0),
        )
    for clip in effect.video_clips:
        add_video_clip_simple(
            scene_num=clip.get("scene_num", 0),
            phrase_idx=0,
            mp4_path=clip.get("mp4_path", ""),
            duration=clip.get("duration_sec", 5.0),
            lora_id=clip.get("lora_id", ""),
        )
    return timeline


def _handle_execute_bash(timeline: otio.schema.Timeline, effect: ExecuteRawBash) -> otio.schema.Timeline:
    """Execute a bash command. This is the only impure handler."""
    subprocess.run(
        effect.command,
        shell=True,
        capture_output=True,
        timeout=300,
    )
    return timeline


def _handle_noop(timeline: otio.schema.Timeline, effect: NoOp) -> otio.schema.Timeline:
    """No-op handler. Returns timeline unchanged."""
    return timeline
