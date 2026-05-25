"""The Instructor — bridge between free-thinking agents and typed effects.

The instructor:
1. Reads the agent's text output
2. Reads the current world state (OTIO)
3. Checks the agent's state machine
4. Parses text into an Effect (model shifts by state)
5. Validates the effect
6. Appends to event store
7. Triggers projection handler
8. Sends feedback to the agent

The agent never knows the instructor exists.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from effects import Effect, NoOp
from effect_parser import parse_agent_text
from event_store import EventStore
from unit_state_machines import get_machine


class Feedback(BaseModel):
    """Feedback sent back to the agent after each turn."""

    parsed_as: str = Field(default="", description="What effect type was extracted")
    status: str = Field(default="", description="ACCEPTED or REJECTED")
    reason: str = Field(default="", description="Why it was accepted or rejected")
    world_state: str = Field(default="", description="Summary of current OTIO state")
    suggestion: str = Field(default="", description="What the agent should do next")
    valid_actions: list[str] = Field(default_factory=list, description="Valid effect types in current state")

    def to_text(self) -> str:
        """Convert feedback to plain text for the agent."""
        lines = [
            "--- FEEDBACK ---",
            f"Parsed as: {self.parsed_as}",
            f"Status: {self.status}",
        ]
        if self.reason:
            lines.append(f"Reason: {self.reason}")
        if self.world_state:
            lines.append(f"World state: {self.world_state}")
        if self.suggestion:
            lines.append(f"Suggestion: {self.suggestion}")
        if self.valid_actions:
            lines.append(f"Valid actions now: {', '.join(self.valid_actions)}")
        lines.append("--- END FEEDBACK ---")
        return "\n".join(lines)


class Instructor:
    """The instructor bridge. One instance per agent invocation."""

    def __init__(
        self,
        unit_id: str,
        event_log_path: str,
        timeline_path: str,
    ):
        self.unit_id = unit_id
        self.machine = get_machine(unit_id)
        self.event_store = EventStore(event_log_path)
        self.timeline_path = timeline_path
        self.current_state = self.machine.initial_state

    def _read_world_state(self) -> str:
        """Read OTIO and summarize for feedback."""
        import glob
        import os

        import opentimelineio as otio

        summary = []

        if os.path.exists(self.timeline_path):
            try:
                timeline = otio.schema.Timeline.from_json_file(self.timeline_path)
                for track in timeline.tracks:
                    clips = len(list(track))
                    summary.append(f"{track.name}: {clips} clips")
            except Exception:
                summary.append("OTIO: unreadable")
        else:
            summary.append("OTIO: not found")

        # Check output
        output_dir = os.path.join(os.path.dirname(self.timeline_path), "output")
        mp4s = glob.glob(os.path.join(output_dir, "*.mp4"))
        summary.append(f"Output MP4s: {len(mp4s)}")

        return "; ".join(summary)

    def process(self, agent_text: str) -> tuple[Effect, Feedback]:
        """Process agent text: parse, validate, store, project, feedback.

        Returns:
            (effect, feedback) — feedback is sent back to agent
        """
        # 1. Parse text into effect
        effect = parse_agent_text(self.unit_id, agent_text)

        # 2. Check state machine
        valid_effects = self.machine.get_valid_effects(self.current_state)

        if effect.effect_type not in valid_effects and effect.effect_type != "NoOp":
            # Invalid effect for current state
            feedback = Feedback(
                parsed_as=effect.effect_type,
                status="REJECTED",
                reason=f"Effect '{effect.effect_type}' is not valid in state '{self.current_state}'. "
                       f"Valid actions: {', '.join(valid_effects)}",
                world_state=self._read_world_state(),
                suggestion="Check the world state and choose a valid action.",
                valid_actions=list(valid_effects),
            )
            return effect, feedback

        # 3. Append to event store
        record = self.event_store.append(effect, otio_hash_before="")

        # 4. Trigger projection handler (rebuild OTIO)
        self._project(effect)

        # 5. Transition state machine
        next_state = self.machine.transition(self.current_state, effect.effect_type)
        self.current_state = next_state

        # 6. Build feedback
        feedback = Feedback(
            parsed_as=effect.effect_type,
            status="ACCEPTED",
            reason=f"Effect accepted. Transitioned from {self.current_state} to {next_state}.",
            world_state=self._read_world_state(),
            suggestion=self._suggest_next(),
            valid_actions=list(self.machine.get_valid_effects(next_state)),
        )

        return effect, feedback

    def _project(self, effect: Effect) -> None:
        """Apply effect to OTIO via projection handler."""
        import opentimelineio as otio

        from projection_handler import apply_event

        if os.path.exists(self.timeline_path):
            try:
                timeline = otio.schema.Timeline.from_json_file(self.timeline_path)
            except Exception:
                timeline = otio.schema.Timeline(name="documentary")
        else:
            timeline = otio.schema.Timeline(name="documentary")

        new_timeline = apply_event(timeline, effect)

        # Write updated timeline
        os.makedirs(os.path.dirname(self.timeline_path), exist_ok=True)
        new_timeline.to_json_file(self.timeline_path)

    def _suggest_next(self) -> str:
        """Suggest what the agent should do next based on state."""
        suggestions = {
            ("scenario", "IDLE"): "Write the documentary script with narration and visual notes.",
            ("scenario", "HAS_SCENARIO"): "Script accepted. Wait for other agents or refine if needed.",
            ("audio", "IDLE"): "Generate narration audio for all voices (V1, V2, V3).",
            ("audio", "PENDING_AUDIO"): "Jobs submitted. Wait for provisioner or submit more clips.",
            ("audio", "HAS_AUDIO"): "Audio complete. Wait for video agent.",
            ("video", "IDLE"): "Generate video clips based on visual notes.",
            ("video", "PENDING_VIDEO"): "Jobs submitted. Wait for provisioner or submit more clips.",
            ("video", "HAS_VIDEO"): "Video complete. Wait for assembly agent.",
            ("assembly", "IDLE"): "Merge audio and video clips into the timeline.",
            ("assembly", "MERGING"): "Continue merging or run ffmpeg to produce final output.",
            ("assembly", "COMPLETE"): "Pipeline complete. Well done.",
            ("provisioner", "IDLE"): "Check job queue and provision VMs as needed.",
            ("provisioner", "PROVISIONING"): "VM provisioning in progress. Check status via bash.",
            ("provisioner", "EXECUTING"): "Jobs running. Check completion via bash.",
        }
        return suggestions.get(
            (self.unit_id, self.current_state),
            "Continue working.",
        )
