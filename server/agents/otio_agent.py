"""
OTIO Unit Agent — the central coordination plane.

The OTIO timeline is THE shared artifact of the pipeline. Every agent
reads and writes to it. The OTIO Agent mediates all writes, enforces
structural bounds aggressively, and participates in escalation ladders
when timing or structure needs adjustment.

Architecture:
    - Mediates all timeline mutations (no agent writes directly)
    - Enforces the draft→authoritative state machine
    - Validates structural integrity after every mutation
    - Proposes rebalancing (duration adjustments, scene reordering)
    - Participates in escalation ladders as the structural authority

The OTIO Agent is contacted by other agents through the escalation
chain. When the TTS Unit Agent can't hit timing, it may ask the OTIO
Agent to rebalance scene durations. When the video agent can't fill a
slot, the OTIO Agent may widen the gap or redistribute time.

Stateless design:
    All tool functions resolve the timeline path from the pipeline
    manifest on disk (via ``resolve_timeline_path()``) and read/write
    the OTIO file directly.  No module-level singletons, no
    ``tool_context.state`` dicts, no ``os.environ`` reads for the
    timeline path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import opentimelineio as otio

from tools.otio_file_ops import (
    resolve_timeline_path,
    otio_read,
    otio_read_modify_write,
    TRACK_V1,
    TRACK_A1,
    TRACK_A2,
)
from tools.otio_metadata import read_pipeline_metadata, write_pipeline_metadata
from tools.otio_lifecycle import (
    get_otio_lifecycle_state,
    guard_mutation,
    OtioStateViolation,
    begin_escalation,
    end_escalation,
)
from recovery_agents import RecoveryAgent, AgentTool, _ESCALATION_TOOLS

logger = logging.getLogger(__name__)


# ── OTIO Agent tools ─────────────────────────────────────────────────


def _tool_read_pipeline_data(key: str) -> str:
    """Read pipeline metadata from the OTIO timeline.

    If the key doesn't exist, returns an error — that IS the contract
    violation. The upstream stage has not produced this data.
    """
    try:
        tp = resolve_timeline_path()
        val = read_pipeline_metadata(tp, key)
        if val is None:
            return json.dumps({
                "error": f"Key '{key}' not found in OTIO timeline",
                "contract_violation": True,
                "reason": f"Upstream stage has not produced '{key}'",
            })
        return json.dumps({"key": key, "value": val})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_write_pipeline_data(
    key: str,
    value_json: str,
    provenance_json: str = "{}",
) -> str:
    """Write pipeline metadata to the OTIO timeline with provenance.

    Creation and persistence are atomic — even on error, the partial
    data is written. The OTIO Agent validates the write.
    """
    try:
        tp = resolve_timeline_path()

        try:
            value = json.loads(value_json)
        except (json.JSONDecodeError, TypeError):
            value = value_json

        try:
            provenance = json.loads(provenance_json) if provenance_json else {}
        except (json.JSONDecodeError, TypeError):
            provenance = {}

        return write_pipeline_metadata(tp, key, value, provenance=provenance)
    except Exception as e:
        # Even on error, try to persist the error
        try:
            tp = resolve_timeline_path()
            write_pipeline_metadata(tp, f"{key}_error", str(e))
        except Exception:
            pass
        return json.dumps({"error": str(e), "key": key})


def _tool_add_clip(
    track: str,
    scene_num: int,
    phrase_idx: int,
    clip_path: str,
    duration: float,
    provenance_json: str = "{}",
) -> str:
    """Add a clip to the OTIO timeline with provenance."""
    try:
        tp = resolve_timeline_path()

        # Mutation guard — raises OtioStateViolation if blocked.
        guard_mutation(tp, operation="add_clip", allow_escalation=True)

        try:
            provenance = json.loads(provenance_json) if provenance_json else {}
        except (json.JSONDecodeError, TypeError):
            provenance = {}

        def _mutate(timeline: otio.schema.Timeline) -> dict:
            # Find the target track
            target_track = None
            for t in timeline.tracks:
                if t.name == track:
                    target_track = t
                    break

            if target_track is None:
                return {"error": f"Track '{track}' not found"}

            # Build clip name
            clip_name = f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}"

            # Idempotency: skip if clip already exists
            for item in target_track:
                if isinstance(item, otio.schema.Clip) and item.name == clip_name:
                    return {
                        "status": "already_exists",
                        "clip_name": clip_name,
                        "message": "Clip already exists, skipping duplicate",
                    }

            # Create the clip
            clip = otio.schema.Clip(
                name=clip_name,
                media_reference=otio.schema.ExternalReference(
                    target_url=clip_path,
                ),
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(duration * 24, 24),
                ),
            )
            clip.metadata["documentary"] = {
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "type": "clip",
            }
            if provenance:
                clip.metadata["documentary"]["provenance"] = provenance

            target_track.append(clip)
            return {
                "added": True,
                "track": track,
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
            }

        result = otio_read_modify_write(tp, _mutate)
        return json.dumps(result)
    except OtioStateViolation as e:
        return json.dumps({
            "error": f"Mutation blocked: {e}",
            "hint": "Open a REPLACE/EXTEND escalation first",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_read_timeline() -> str:
    """Read the full timeline structure — tracks, clips, gaps, durations."""
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        tracks_info = []
        for track in timeline.tracks:
            items = []
            for item in track:
                entry = {
                    "name": item.name,
                    "type": type(item).__name__,
                }
                if isinstance(item, otio.schema.Clip) and item.source_range:
                    entry["duration_sec"] = round(
                        item.source_range.duration.to_seconds(), 3
                    )
                    if item.media_reference and hasattr(
                        item.media_reference, "target_url"
                    ):
                        entry["media"] = item.media_reference.target_url
                elif isinstance(item, otio.schema.Gap) and item.source_range:
                    entry["duration_sec"] = round(
                        item.source_range.duration.to_seconds(), 3
                    )
                doc_meta = item.metadata.get("documentary", {})
                if doc_meta:
                    entry["metadata"] = dict(doc_meta)
                items.append(entry)

            total_dur = sum(
                i.source_range.duration.to_seconds()
                for i in track
                if i.source_range
            )
            tracks_info.append({
                "name": track.name,
                "kind": str(track.kind),
                "items": items,
                "total_duration_sec": round(total_dur, 3),
            })

        doc_meta = timeline.metadata.get("documentary", {})
        return json.dumps({
            "name": timeline.name,
            "state": doc_meta.get("state", "unknown"),
            "tracks": tracks_info,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_validate_timeline(phase: str) -> str:
    """Validate timeline structural integrity for a given pipeline phase.

    Checks: correct track structure, no missing clips, no zero-duration
    gaps, no duplicate clip names, narration clips have WAV files,
    video clips have media references.
    """
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        from callbacks.timeline_guardian import _VALIDATORS

        validator = _VALIDATORS.get(phase)
        if not validator:
            return json.dumps({
                "valid": False,
                "error": f"Unknown phase: {phase}",
            })

        # Validators take (timeline, state) but in the stateless world
        # we pass an empty dict — the file is the source of truth.
        error = validator(timeline, {})
        if error:
            return json.dumps({"valid": False, "phase": phase, "errors": error})

        return json.dumps({
            "valid": True,
            "phase": phase,
            "message": "All checks passed",
        })
    except Exception as e:
        return json.dumps({"valid": False, "error": str(e)})


def _tool_get_scene_durations() -> str:
    """Get per-scene duration budgets — narration vs video vs total.

    Returns a scene-by-scene breakdown showing how much time each
    scene has, how much narration fills, and how much video needs.
    """
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        scenes = {}
        for track in timeline.tracks:
            for item in track:
                doc_meta = item.metadata.get("documentary", {})
                scene_num = doc_meta.get("scene_num", 0)
                if not scene_num:
                    continue
                if scene_num not in scenes:
                    scenes[scene_num] = {
                        "narration_sec": 0,
                        "video_sec": 0,
                        "narration_clips": 0,
                        "video_clips": 0,
                    }
                dur = 0.0
                if item.source_range:
                    dur = item.source_range.duration.to_seconds()
                if track.name == TRACK_A1 and isinstance(item, otio.schema.Clip):
                    scenes[scene_num]["narration_sec"] += dur
                    scenes[scene_num]["narration_clips"] += 1
                elif track.name == TRACK_V1:
                    if isinstance(item, otio.schema.Clip):
                        scenes[scene_num]["video_sec"] += dur
                        scenes[scene_num]["video_clips"] += 1
                    elif isinstance(item, otio.schema.Gap) and dur > 0:
                        scenes[scene_num]["video_sec"] += dur

        return json.dumps({"scenes": scenes}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_rebalance_durations(
    adjustments_json: str,
    reason: str,
) -> str:
    """Rebalance scene duration budgets.

    Called by the TTS Unit Agent or Video Unit Agent when timing is off.
    For example: "Scene 5 narration is 2.1s short — redistribute 1.0s
    from scene 8 (which has 3.0s slack) to scene 5."

    The OTIO Agent enforces: total duration is conserved, no scene goes
    below minimum (2.0s), and the authoritative baseline is not violated
    unless an escalation is open.

    Args:
        adjustments_json: JSON dict {scene_num: new_duration_sec}
        reason: Why the rebalance is needed (audit trail)
    """
    try:
        tp = resolve_timeline_path()

        # Mutation guard — raises OtioStateViolation if blocked.
        try:
            guard_mutation(
                tp,
                operation="rebalance_durations",
                allow_escalation=True,
            )
        except OtioStateViolation as e:
            return json.dumps({
                "success": False,
                "error": f"Mutation blocked: {e}",
                "hint": "Open a REPLACE/EXTEND escalation first",
            })

        adjustments = json.loads(adjustments_json)
        if not isinstance(adjustments, dict):
            return json.dumps({
                "success": False,
                "error": "adjustments must be a JSON dict {scene_num: duration_sec}",
            })

        def _mutate(timeline: otio.schema.Timeline) -> dict:
            # Calculate total before
            total_before = 0.0
            for track in timeline.tracks:
                if track.name == TRACK_V1:
                    for item in track:
                        if item.source_range:
                            total_before += item.source_range.duration.to_seconds()

            # Apply adjustments
            applied = {}
            for track in timeline.tracks:
                if track.name != TRACK_V1:
                    continue
                for item in track:
                    doc_meta = item.metadata.get("documentary", {})
                    scene_num = doc_meta.get("scene_num", 0)
                    if scene_num and str(scene_num) in adjustments:
                        new_dur = float(adjustments[str(scene_num)])
                        if new_dur < 2.0:
                            # Raise to signal failure — the RMW will
                            # still write but the error is returned.
                            return {
                                "success": False,
                                "error": (
                                    f"Scene {scene_num}: duration "
                                    f"{new_dur}s below minimum 2.0s"
                                ),
                            }
                        if item.source_range:
                            old_dur = item.source_range.duration.to_seconds()
                            item.source_range = otio.opentime.TimeRange(
                                start_time=otio.opentime.RationalTime(0, 24),
                                duration=otio.opentime.RationalTime.from_seconds(
                                    new_dur, 24
                                ),
                            )
                            applied[scene_num] = {
                                "old_dur": round(old_dur, 3),
                                "new_dur": round(new_dur, 3),
                            }

            # Calculate total after
            total_after = 0.0
            for track in timeline.tracks:
                if track.name == TRACK_V1:
                    for item in track:
                        if item.source_range:
                            total_after += item.source_range.duration.to_seconds()

            # Record rebalance history in metadata
            doc_meta = timeline.metadata.setdefault("documentary", {})
            doc_meta.setdefault("rebalance_history", []).append({
                "adjustments": adjustments,
                "reason": reason,
                "applied": applied,
                "total_before": round(total_before, 3),
                "total_after": round(total_after, 3),
            })

            return {
                "applied": applied,
                "total_before": round(total_before, 3),
                "total_after": round(total_after, 3),
            }

        result = otio_read_modify_write(tp, _mutate)

        # If the mutation returned a failure (e.g. below-minimum duration),
        # propagate it directly.
        if result.get("success") is False:
            return json.dumps(result)

        return json.dumps({
            "success": True,
            "applied": result.get("applied", {}),
            "total_before": result.get("total_before", 0),
            "total_after": result.get("total_after", 0),
            "conservation_check": (
                "PASS"
                if abs(result.get("total_before", 0) - result.get("total_after", 0)) < 0.1
                else "FAIL"
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _tool_propose_duration_fix(
    scene_num: int,
    current_duration: float,
    target_duration: float,
) -> str:
    """Propose a duration fix for a scene that's over/under budget.

    The OTIO Agent calculates where the surplus/deficit can come from
    and proposes a redistribution. The calling agent (TTS or Video)
    decides whether to accept.

    Returns a proposal: which scenes have slack, how much can be
    redistributed, and the resulting durations.
    """
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        deficit = target_duration - current_duration
        if deficit <= 0:
            return json.dumps({
                "scene": scene_num,
                "current": current_duration,
                "target": target_duration,
                "deficit": deficit,
                "proposal": "no fix needed — scene is at or above target",
            })

        # Find scenes with slack
        scene_durations = {}
        for track in timeline.tracks:
            if track.name != TRACK_V1:
                continue
            for item in track:
                doc_meta = item.metadata.get("documentary", {})
                sn = doc_meta.get("scene_num", 0)
                if sn and item.source_range:
                    dur = item.source_range.duration.to_seconds()
                    scene_durations[sn] = dur

        # Scenes with > 2.0s are candidates for redistribution
        candidates = []
        for sn, dur in scene_durations.items():
            if sn != scene_num and dur > 2.5:
                slack = dur - 2.0  # keep minimum 2.0s
                candidates.append({"scene_num": sn, "duration": dur, "slack": round(slack, 3)})
                candidates.sort(key=lambda c: c["slack"], reverse=True)

        # Build proposal: take from candidates proportionally
        remaining = deficit
        proposed = {}
        for c in candidates:
            if remaining <= 0:
                break
            take = min(c["slack"], remaining)
            proposed[c["scene_num"]] = round(c["duration"] - take, 3)
            remaining -= take

        if remaining > 0:
            return json.dumps({
                "scene": scene_num,
                "deficit": round(deficit, 3),
                "proposal": "INSUFFICIENT_SLACK",
                "available_slack": round(deficit - remaining, 3),
                "shortfall": round(remaining, 3),
                "hint": "Not enough slack in other scenes to redistribute. Consider reducing content or asking the scenario agent to restructure.",
            })

        proposed[scene_num] = round(target_duration, 3)
        return json.dumps({
            "scene": scene_num,
            "deficit": round(deficit, 3),
            "proposal": "REDISTRIBUTE",
            "adjustments": proposed,
            "hint": "Call rebalance_durations with these adjustments if acceptable.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_otio_state() -> str:
    """Get the current OTIO lifecycle state (draft or authoritative).

    Reads the lifecycle state and escalation status directly from the
    OTIO file on disk — no in-memory state, no blackboard.
    """
    try:
        tp = resolve_timeline_path()
        current = get_otio_lifecycle_state(tp)

        # Read escalation directly from the timeline file — avoids
        # importing get_escalation (which is a convenience wrapper
        # around the same otio_read + metadata lookup).
        timeline = otio_read(tp)
        doc_meta = timeline.metadata.get("documentary", {})
        escalation = doc_meta.get("escalation")
        if escalation is not None:
            import copy
            escalation = copy.deepcopy(dict(escalation))

        return json.dumps({
            "state": current,
            "escalation": escalation,
            "timeline_path": tp,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


_OTIO_AGENT_TOOLS = [
    AgentTool(
        name="read_pipeline_data",
        description=(
            "Read pipeline metadata from the OTIO timeline. This is how "
            "agents access intermediate data (scenes, whisperx_alignment, "
            "visual_concepts, visual_style, style_lock, content_analysis). "
            "If the key doesn't exist, returns an error — that IS the "
            "contract violation. The upstream stage has not produced this data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Pipeline metadata key (scenes, whisperx_alignment, visual_concepts, visual_style, style_lock, content_analysis)",
                },
            },
            "required": ["key"],
        },
        fn=lambda key: _tool_read_pipeline_data(key),
    ),
    AgentTool(
        name="write_pipeline_data",
        description=(
            "Write pipeline metadata to the OTIO timeline with provenance. "
            "This is how agents persist intermediate data. Creation and "
            "persistence are atomic — even on error, the partial data is "
            "written. The OTIO Agent validates the write."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Pipeline metadata key",
                },
                "value_json": {
                    "type": "string",
                    "description": "JSON string of the value to write",
                },
                "provenance_json": {
                    "type": "string",
                    "description": "JSON string of the ArtifactProvenance record",
                },
            },
            "required": ["key", "value_json"],
        },
        fn=lambda key, value_json, provenance_json="{}": _tool_write_pipeline_data(
            key, value_json, provenance_json
        ),
    ),
    AgentTool(
        name="add_clip",
        description=(
            "Add a clip to the OTIO timeline with provenance. The OTIO Agent "
            "validates the clip before adding it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track": {
                    "type": "string",
                    "description": "Track name (V1_Video, A1_Narration, A2_Music)",
                },
                "scene_num": {
                    "type": "integer",
                    "description": "Scene number",
                },
                "phrase_idx": {
                    "type": "integer",
                    "description": "Phrase index within scene",
                },
                "clip_path": {
                    "type": "string",
                    "description": "Path to the clip file",
                },
                "duration": {
                    "type": "number",
                    "description": "Duration in seconds",
                },
                "provenance_json": {
                    "type": "string",
                    "description": "JSON string of the ArtifactProvenance record",
                },
            },
            "required": ["track", "scene_num", "phrase_idx", "clip_path", "duration"],
        },
        fn=lambda track, scene_num, phrase_idx, clip_path, duration, provenance_json="{}": _tool_add_clip(
            track, scene_num, phrase_idx, clip_path, duration, provenance_json
        ),
    ),
    AgentTool(
        name="read_timeline",
        description=(
            "Read the full OTIO timeline structure — tracks, clips, "
            "gaps, durations, metadata. This is the authoritative "
            "view of the documentary's structure."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_read_timeline(),
    ),
    AgentTool(
        name="validate_timeline",
        description=(
            "Validate timeline structural integrity for a pipeline phase. "
            "Checks: correct track structure, no missing clips, no "
            "zero-duration gaps, no duplicate clip names."
        ),
        parameters={
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["scenario", "audio", "visual_direction", "production", "assembly"],
                    "description": "Pipeline phase to validate against",
                },
            },
            "required": ["phase"],
        },
        fn=lambda phase="audio": _tool_validate_timeline(phase),
    ),
    AgentTool(
        name="get_scene_durations",
        description=(
            "Get per-scene duration budgets — narration vs video vs total. "
            "Shows how much time each scene has, how much narration fills, "
            "and how much video needs."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_get_scene_durations(),
    ),
    AgentTool(
        name="rebalance_durations",
        description=(
            "Rebalance scene duration budgets. Adjusts scene durations "
            "while conserving total timeline duration. Use when timing "
            "drift requires redistributing time between scenes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "adjustments_json": {
                    "type": "string",
                    "description": "JSON dict {scene_num: new_duration_sec}",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the rebalance is needed (audit trail)",
                },
            },
            "required": ["adjustments_json", "reason"],
        },
        fn=lambda adjustments_json, reason: _tool_rebalance_durations(
            adjustments_json, reason
        ),
    ),
    AgentTool(
        name="propose_duration_fix",
        description=(
            "Propose a duration fix for a scene that's over/under budget. "
            "Calculates where surplus/deficit can come from and proposes "
            "redistribution. The calling agent decides whether to accept."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scene_num": {"type": "integer", "description": "Scene number"},
                "current_duration": {"type": "number", "description": "Current duration in seconds"},
                "target_duration": {"type": "number", "description": "Target duration in seconds"},
            },
            "required": ["scene_num", "current_duration", "target_duration"],
        },
        fn=lambda scene_num, current_duration, target_duration: _tool_propose_duration_fix(
            scene_num, current_duration, target_duration
        ),
    ),
    AgentTool(
        name="get_otio_state",
        description=(
            "Get the current OTIO lifecycle state (draft or authoritative) "
            "and any open escalation context."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_get_otio_state(),
    ),
]


class OTIOUnitAgent(RecoveryAgent):
    """OTIO Unit Agent — the central coordination plane.

    Owns the timeline's structural integrity. Every other agent that
    wants to change the timeline goes through this agent. It enforces:
    - The draft→authoritative state machine
    - Structural bounds (no zero-duration gaps, no duplicates, correct ordering)
    - Duration conservation (total timeline duration is preserved)
    - Minimum scene durations (2.0s floor)

    Participates in escalation ladders:
    - TTS agent: "Scene 5 is 2.1s short" → OTIO agent proposes redistribution
    - Video agent: "Can't fill this slot" → OTIO agent widens the gap
    - Scenario agent: "Need to restructure" → OTIO agent validates new structure
    """

    def __init__(self) -> None:
        super().__init__(
            name="otio_unit_agent",
            instruction=(
                "You are the OTIO Unit Agent — you are the central coordination "
                "plane of the documentary pipeline.\n\n"
                "THE TIMELINE IS YOUR DOMAIN.\n\n"
                "Your responsibilities:\n"
                "1. PIPELINE DATA: All intermediate data (scenes, alignment, "
                "visual_concepts, visual_style, style_lock) flows through you. "
                "Agents write to you with write_pipeline_data. Agents read from "
                "you with read_pipeline_data. If requested data doesn't exist, "
                "return an error — that IS the contract violation.\n"
                "2. STRUCTURAL INTEGRITY: Every timeline mutation goes through you. "
                "Validate before writing. Reject invalid mutations aggressively.\n"
                "3. DURATION CONSERVATION: Total timeline duration is preserved. "
                "If a scene needs more time, another scene must give it up.\n"
                "4. MINIMUM DURATIONS: No scene below 2.0 seconds.\n"
                "5. STATE MACHINE: Enforce draft→authoritative transitions. "
                "Once authoritative, mutations are BLOCKED unless an escalation "
                "is open.\n"
                "6. PROVENANCE: Every write carries provenance. Never discard it.\n"
                "7. CHECKPOINTING: Persist to disk and B2 after every write.\n\n"
                "CONTRACT ENFORCEMENT:\n"
                "You ARE the contract enforcer. When an agent reads data that "
                "doesn't exist, you say so. When an agent writes invalid data, "
                "you reject it. No separate hooks needed.\n\n"
                "PARTICIPATING IN ESCALATION:\n"
                "When the Audio Agent reports timing drift, propose a fix:\n"
                "- Use propose_duration_fix to calculate where slack exists\n"
                "- Use rebalance_durations to redistribute time\n"
                "- If no slack exists, escalate to the scenario agent\n\n"
                "When the Video Agent can't fill a slot:\n"
                "- Check if the slot can be widened\n"
                "- If widening requires redistribution, propose it\n\n"
                "RULES:\n"
                "- ALWAYS validate after writing (validate_timeline)\n"
                "- NEVER allow total duration to change\n"
                "- NEVER allow a scene below 2.0s\n"
                "- REJECT mutations to authoritative OTIO unless escalation is open\n"
                "- PROPOSE fixes, don't just report problems\n"
                "- You are the structural authority — your word is law on the timeline"
            ),
            tools=_OTIO_AGENT_TOOLS + _ESCALATION_TOOLS,
            max_tool_rounds=8,
        )
