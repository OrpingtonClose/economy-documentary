"""OTIO-based orchestrator — the OTIO is the state machine.

Reads the OTIO timeline and event log, determines what's missing,
and generates the next effects needed to complete the documentary.

This is a graph of dependencies:
  Script exists? → need UpdateScript
  Script has narration? → need GenerateNarrationAudio per voice
  Script has visual notes? → need RenderVideoSegment per scene
  Audio jobs completed? → need MergeIntoOTIO
  Video jobs completed? → need MergeIntoOTIO
  Clips merged? → need ExecuteRawBash (ffmpeg render)
"""

from __future__ import annotations

import os
from typing import Any

import opentimelineio as otio

from effects import (
    Effect,
    ExecuteRawBash,
    GenerateNarrationAudio,
    JobCompleted,
    JobQueued,
    MergeIntoOTIO,
    NoOp,
    RenderVideoSegment,
    UpdateScript,
)
from event_store import EventStore


def _get_script_meta(timeline: otio.schema.Timeline) -> dict[str, Any]:
    """Extract documentary script metadata from timeline."""
    return timeline.metadata.get("documentary", {})


def _get_audio_jobs_completed(event_store: EventStore) -> dict[str, JobCompleted]:
    """Get all completed audio jobs from event log."""
    completed: dict[str, JobCompleted] = {}
    for record in event_store.read_all():
        if isinstance(record.effect, JobCompleted) and record.effect.stage == "audio":
            # Key by job_id
            completed[record.effect.job_id] = record.effect
    return completed


def _get_video_jobs_completed(event_store: EventStore) -> dict[str, JobCompleted]:
    """Get all completed video jobs from event log."""
    completed: dict[str, JobCompleted] = {}
    for record in event_store.read_all():
        if isinstance(record.effect, JobCompleted) and record.effect.stage == "video":
            completed[record.effect.job_id] = record.effect
    return completed


def _get_queued_jobs(event_store: EventStore) -> dict[str, set[str]]:
    """Get all queued jobs by stage."""
    queued: dict[str, set[str]] = {"audio": set(), "video": set()}
    for record in event_store.read_all():
        if isinstance(record.effect, JobQueued):
            queued[record.effect.stage].add(record.effect.job_id)
    return queued


def _count_clips(timeline: otio.schema.Timeline) -> dict[str, int]:
    """Count clips on each track."""
    counts: dict[str, int] = {}
    for track in timeline.tracks:
        counts[track.name] = len(list(track))
    return counts


def compute_next_effects(
    timeline_path: str,
    event_log_path: str,
    brief: str = "",
) -> list[Effect]:
    """Compute what effects are needed next based on OTIO + event log.

    Returns a list of effects. The orchestrator executes them.
    """
    effects: list[Effect] = []

    # Load state
    if os.path.exists(timeline_path):
        timeline = otio.schema.Timeline.from_json_file(timeline_path)
    else:
        timeline = otio.schema.Timeline(name="documentary")

    event_store = EventStore(event_log_path)
    meta = _get_script_meta(timeline)
    queued = _get_queued_jobs(event_store)
    audio_completed = _get_audio_jobs_completed(event_store)
    video_completed = _get_video_jobs_completed(event_store)
    clips = _count_clips(timeline)

    # 1. SCRIPT — does script metadata exist?
    if not meta.get("narration_v1"):
        effects.append(UpdateScript(
            agent_id="orchestrator",
            justification="Script missing narration_v1",
            scene_num=1,
        ))
        return effects  # Can't do anything else without script

    # 2. AUDIO JOBS — does every voice have a queued job?
    voices = ["V1", "V2", "V3"]
    narration_fields = {
        "V1": meta.get("narration_v1", ""),
        "V2": meta.get("narration_v2", ""),
        "V3": meta.get("narration_v3", ""),
    }

    for voice, text in narration_fields.items():
        if not text:
            continue
        # Check if this voice already has a queued/completed job
        # ( We'd need to match by payload content, but for now check if ANY jobs exist )
        if not queued["audio"] and not audio_completed:
            effects.append(GenerateNarrationAudio(
                agent_id="orchestrator",
                justification=f"Audio job needed for {voice}",
                scene_num=1,
                voice=voice,
                text=text,
            ))

    # 3. VIDEO JOBS — does every scene have a queued job?
    visual_notes = meta.get("visual_notes", "")
    if visual_notes and not queued["video"] and not video_completed:
        effects.append(RenderVideoSegment(
            agent_id="orchestrator",
            justification="Video job needed for scene 1",
            scene_num=1,
            prompt=visual_notes,
            duration_sec=meta.get("duration_sec", 5),
        ))

    # 4. ASSEMBLY — are all jobs completed but not merged?
    audio_done = len(audio_completed) >= sum(1 for v in narration_fields.values() if v)
    video_done = bool(video_completed) or not visual_notes

    if audio_done and video_done:
        audio_clips = []
        video_clips = []
        for job in audio_completed.values():
            audio_clips.append({
                "scene_num": 1,
                "voice": "V1",  # TODO: extract from payload
                "wav_path": job.artifact_path,
            })
        for job in video_completed.values():
            video_clips.append({
                "scene_num": 1,
                "mp4_path": job.artifact_path,
            })

        if audio_clips or video_clips:
            effects.append(MergeIntoOTIO(
                agent_id="orchestrator",
                justification="Merge completed clips into timeline",
                audio_clips=audio_clips,
                video_clips=video_clips,
            ))

    # 5. RENDER — are clips merged but no output file?
    has_clips = clips.get("A1_Narration", 0) > 0 or clips.get("V1_Video", 0) > 0
    output_dir = os.path.join(os.path.dirname(timeline_path), "output")
    has_output = os.path.exists(os.path.join(output_dir, "documentary.mp4"))

    if has_clips and not has_output:
        effects.append(ExecuteRawBash(
            agent_id="orchestrator",
            justification="Render final output",
            command=f"ffmpeg -f lavfi -i color=c=black:s=1920x1080:d=30 -i {timeline_path} -c copy {output_dir}/documentary.mp4",
            reason="Produce final MP4 from timeline",
        ))

    if not effects:
        effects.append(NoOp(
            agent_id="orchestrator",
            justification="All stages complete or blocked",
            reason="Nothing to do",
        ))

    return effects
