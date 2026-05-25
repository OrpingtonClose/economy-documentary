"""Integration test for v2 architecture: per-unit state machines + instructor.

Mocks HTTP agent calls and Instructor to avoid needing DeepSeek API keys.
Verifies the full architecture: orchestrator → HTTP → instructor → event store.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

from run_pipeline_v2 import run_pipeline, run_unit
from instructor import Instructor, Feedback
from effects import NoOp, UpdateScript, GenerateNarrationAudio


async def test_run_unit():
    """Test a single unit cycle."""
    call_count = {"n": 0}

    async def mock_call_agent(url: str, text: str, timeout: float = 300.0) -> str:
        call_count["n"] += 1
        # First call: return script content. Second call: NoOp (after feedback)
        if call_count["n"] == 1:
            return (
                "Scene 1 — Test Scene (30s)\n"
                "V1 Hook: Test narration hook.\n"
                "V2 Expert: Test expert narration.\n"
                "V3 Storyteller: Test storyteller narration.\n"
                "Visual notes: Test shot description.\n"
            )
        return "NoOp: nothing to do"

    def mock_process(self, agent_output: str):
        if "Scene 1" in agent_output:
            effect = UpdateScript(
                effect_type="UpdateScript",
                agent_id=self.unit_id,
                narration_v1="Test narration hook.",
                narration_v2="Test expert narration.",
                narration_v3="Test storyteller narration.",
                visual_notes="Test shot description.",
            )
        else:
            effect = NoOp(effect_type="NoOp", agent_id=self.unit_id, reason="mock")
        
        # Store the effect in the event store (like real process does)
        self.event_store.append(effect, otio_hash_before="")
        
        feedback = Feedback(
            parsed_as=effect.effect_type,
            status="accepted",
            reason="mock",
            world_state="mock",
            suggestion="mock",
            valid_actions=["NoOp"],
        )
        return effect, feedback

    with patch("run_pipeline_v2._call_agent", side_effect=mock_call_agent):
        with patch.object(Instructor, "process", mock_process):
            with tempfile.TemporaryDirectory() as tmpdir:
                timeline_path = os.path.join(tmpdir, "timelines", "doc.otio")
                event_log_path = os.path.join(tmpdir, "events.jsonl")
                os.makedirs(os.path.dirname(timeline_path), exist_ok=True)

                instructor = Instructor("scenario", event_log_path, timeline_path)
                result = await run_unit("scenario", "Write a test script.", instructor)

                print(f"Result: {result}")
                print(f"Calls: {call_count['n']}")

                # Verify events were recorded
                from event_store import EventStore
                store = EventStore(event_log_path)
                events = store.read_all()
                print(f"Events recorded: {len(events)}")

                effect_types = [e.effect.effect_type for e in events]
                print(f"Effect types: {effect_types}")

                assert "UpdateScript" in effect_types
                print("test_run_unit PASS")


async def test_pipeline_v2_dry_run():
    """Test full v2 pipeline with mocked agents."""
    call_count = {"n": 0}
    _state_flags = {"audio": False, "video": False, "output": False}

    async def mock_call_agent(url: str, text: str, timeout: float = 300.0) -> str:
        call_count["n"] += 1
        if "9001" in url:  # scenario
            return (
                "Scene 1 — Test Scene (30s)\n"
                "V1 Hook: Test narration hook.\n"
            )
        elif "9002" in url:  # audio
            return "Generate narration audio for V1: Test narration hook."
        elif "9003" in url:  # video
            return "Render video segment for scene 1: cinematic shot of rainbow."
        elif "9005" in url:  # assembly
            return "Merge into OTIO: scene 1 audio=/tmp/test.wav video=/tmp/test.mp4"
        elif "9006" in url:  # provisioner
            return "No pending jobs. All complete."
        return "NoOp: nothing to do"

    def mock_process(self, agent_output: str):
        if "Scene 1" in agent_output and self.unit_id == "scenario":
            effect = UpdateScript(
                effect_type="UpdateScript",
                agent_id=self.unit_id,
                narration_v1="Test narration hook.",
                narration_v2="Test expert narration.",
                narration_v3="Test storyteller narration.",
                visual_notes="Test shot description.",
            )
        elif "audio" in agent_output.lower() and self.unit_id == "audio":
            effect = GenerateNarrationAudio(
                effect_type="GenerateNarrationAudio",
                agent_id=self.unit_id,
                voice="V1",
                text="Test narration hook.",
            )
            _state_flags["audio"] = True
        elif "video" in agent_output.lower() and self.unit_id == "video":
            from effects import RenderVideoSegment
            effect = RenderVideoSegment(
                effect_type="RenderVideoSegment",
                agent_id=self.unit_id,
                prompt="cinematic shot of rainbow",
                lora_id="",
            )
            _state_flags["video"] = True
        elif "merge" in agent_output.lower() and self.unit_id == "assembly":
            from effects import MergeIntoOTIO
            effect = MergeIntoOTIO(
                effect_type="MergeIntoOTIO",
                agent_id=self.unit_id,
                audio_clips=[{"path": "/tmp/test.wav", "scene": 1}],
                video_clips=[{"path": "/tmp/test.mp4", "scene": 1}],
            )
            _state_flags["output"] = True
        else:
            effect = NoOp(effect_type="NoOp", agent_id=self.unit_id, reason="mock")

        # Store the effect
        self.event_store.append(effect, otio_hash_before="")

        feedback = Feedback(
            parsed_as=effect.effect_type,
            status="accepted",
            reason="mock",
            world_state="mock",
            suggestion="mock",
            valid_actions=["NoOp"],
        )
        return effect, feedback

    def mock_destroy_all_vms():
        pass

    def mock_launch_all():
        return []

    def mock_wait_for_agents(processes, timeout):
        return True

    def mock_terminate_all(processes):
        pass

    def mock_get_queue_summary(stage):
        return {"pending": 0, "assigned": 0, "running": 0, "completed": 0, "failed": 0, "needs_retry": 0}

    def mock_has_audio(timeline_path):
        return _state_flags["audio"]

    def mock_has_video(timeline_path):
        return _state_flags["video"]

    def mock_has_output(timeline_path):
        return _state_flags["output"]

    with patch("run_pipeline_v2._call_agent", side_effect=mock_call_agent):
        with patch.object(Instructor, "process", mock_process):
            with patch("strands_agents.run_strands._destroy_all_vms", mock_destroy_all_vms):
                with patch("pydantic_deep_agents.launcher.launch_all", mock_launch_all):
                    with patch("pydantic_deep_agents.launcher.wait_for_agents", mock_wait_for_agents):
                        with patch("pydantic_deep_agents.launcher.terminate_all", mock_terminate_all):
                            with patch("run_pipeline_v2._check_has_audio", mock_has_audio):
                                with patch("run_pipeline_v2._check_has_video", mock_has_video):
                                    with patch("run_pipeline_v2._check_has_output", mock_has_output):
                                        with patch("run_pipeline_v2._get_pending_jobs", return_value=0):
                                            with tempfile.TemporaryDirectory() as tmpdir:
                                                result = await run_pipeline(
                                                    brief="Test documentary",
                                                    output_dir=tmpdir,
                                                )

                                                print(f"Result: {result}")
                                                print(f"Total calls: {call_count['n']}")

                                                assert "complete" in result.lower()
                                                print("test_pipeline_v2_dry_run PASS")


if __name__ == "__main__":
    asyncio.run(test_run_unit())
    asyncio.run(test_pipeline_v2_dry_run())
    print("\nAll tests passed!")
