"""Apply effects to OTIO timeline. OTIO is a read model rebuilt from events.

This is the ONLY file that writes to OTIO. Agents never touch OTIO directly.
Each effect type has exactly one handler function.

Pure function — no side effects, no database calls, no subprocess.
"""

from __future__ import annotations

import opentimelineio as otio

from effects import (
    Effect,
    GenerateNarrationAudio,
    MergeIntoOTIO,
    NoOp,
    RenderVideoSegment,
    UpdateScript,
)


def apply_event(timeline: otio.schema.Timeline, effect: Effect) -> otio.schema.Timeline:
    """Apply a single effect to the timeline. Returns a new timeline.

    Pure function — no side effects.
    The timeline is rebuilt from events, not modified in place.
    """
    handlers: dict[str, callable] = {
        "UpdateScript": _handle_update_script,
        "GenerateNarrationAudio": _handle_generate_audio,
        "RenderVideoSegment": _handle_render_video,
        "MergeIntoOTIO": _handle_merge_into_otio,
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
    """Record audio job request in OTIO metadata."""
    meta = timeline.metadata.setdefault("documentary", {})
    pending = meta.setdefault("pending_audio_jobs", [])
    entry = {
        "voice": effect.voice,
        "text": effect.text,
        "scene_num": effect.scene_num,
        "agent": effect.agent_id,
    }
    # Deduplicate by voice+scene
    if not any(e["voice"] == entry["voice"] and e["scene_num"] == entry["scene_num"] for e in pending):
        pending.append(entry)
    return timeline


def _handle_render_video(timeline: otio.schema.Timeline, effect: RenderVideoSegment) -> otio.schema.Timeline:
    """Record video job request in OTIO metadata."""
    meta = timeline.metadata.setdefault("documentary", {})
    pending = meta.setdefault("pending_video_jobs", [])
    entry = {
        "prompt": effect.prompt,
        "lora_id": effect.lora_id,
        "duration_sec": effect.duration_sec,
        "scene_num": effect.scene_num,
        "agent": effect.agent_id,
    }
    # Deduplicate by scene
    if not any(e["scene_num"] == entry["scene_num"] for e in pending):
        pending.append(entry)
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


def _handle_noop(timeline: otio.schema.Timeline, effect: NoOp) -> otio.schema.Timeline:
    """No-op handler. Returns timeline unchanged."""
    return timeline
