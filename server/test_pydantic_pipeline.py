"""Integration test for pydantic-graph + pydantic-deep pipeline.

Mocks HTTP agent calls to avoid needing DeepSeek API keys or GPU workers.
Verifies the full architecture: graph → HTTP → effect parser → event store.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

from pydantic_graph_pipeline import AgentURLs, PipelineState, run_pipeline


async def test_pipeline_dry_run():
    """Test full pipeline with mocked agents."""

    call_count = {"n": 0}

    async def mock_call_agent(url: str, text: str, timeout: float = 300.0) -> str:
        call_count["n"] += 1
        if "9001" in url:  # scenario
            return (
                "Scene 1 — Test Scene (30s)\n"
                "V1 Hook: Test narration hook.\n"
                "V2 Expert: Test expert narration.\n"
                "V3 Storyteller: Test storyteller narration.\n"
                "Visual notes: Test shot description.\n"
            )
        elif "9002" in url:  # audio
            return "Generate narration audio for V1: Test narration hook."
        elif "9003" in url:  # video
            return "Render video segment for scene 1: cinematic shot of rainbow."
        elif "9005" in url:  # assembly
            return "Merge into OTIO: scene 1 audio=/tmp/test.wav video=/tmp/test.mp4"
        elif "9006" in url:  # provisioner
            return "No pending jobs. All complete."
        return "OK"

    # OTIO state progresses with each iteration
    def mock_has_audio(timeline):
        return call_count["n"] > 1

    def mock_has_video(timeline):
        return call_count["n"] > 2

    def mock_output_exists(path):
        return call_count["n"] > 3

    def mock_queue_summary(stage):
        return {"pending": 0, "assigned": 0, "running": 0, "completed": 0, "failed": 0, "needs_retry": 0}

    with patch("pydantic_graph_pipeline._call_agent", side_effect=mock_call_agent):
        with patch("pydantic_graph_pipeline._otio_has_audio", mock_has_audio):
            with patch("pydantic_graph_pipeline._otio_has_video", mock_has_video):
                with patch("pydantic_graph_pipeline._output_exists", mock_output_exists):
                    with patch("job_queue.get_queue_summary", mock_queue_summary):
                        with tempfile.TemporaryDirectory() as tmpdir:
                            timeline_path = os.path.join(tmpdir, "timelines", "doc.otio")
                            event_log_path = os.path.join(tmpdir, "events.jsonl")
                            os.makedirs(os.path.dirname(timeline_path), exist_ok=True)

                            state = PipelineState(
                                current_task="Test documentary",
                                timeline_path=timeline_path,
                                event_log_path=event_log_path,
                                run_id="test_run",
                            )
                            deps = AgentURLs()

                            result = await run_pipeline(state, deps, "Test documentary")

                            print(f"Result: {result}")
                            print(f"Agent calls: {call_count['n']}")

                            # Verify events were recorded
                            from event_store import EventStore
                            store = EventStore(event_log_path)
                            events = store.read_all()
                            print(f"Events recorded: {len(events)}")

                            effect_types = [e.effect.effect_type for e in events]
                            print(f"Effect types: {effect_types}")
                            assert "UpdateScript" in effect_types
                            assert "GenerateNarrationAudio" in effect_types
                            print("PASS")


if __name__ == "__main__":
    asyncio.run(test_pipeline_dry_run())
