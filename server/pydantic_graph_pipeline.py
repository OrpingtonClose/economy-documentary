"""pydantic-graph pipeline orchestrator.

All agents are independent HTTP services. The orchestrator calls them via HTTP POST.
No shared state, no in-process calls.

The orchestrator reads OTIO as a projection (rebuilt from events) to decide routing.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

import httpx
import opentimelineio as otio
from pydantic_graph import End, GraphBuilder, StepContext

from effect_parser import parse_agent_text
from event_store import EventStore


@dataclass
class PipelineState:
    """Mutable state carried through the graph."""

    current_task: str = ""
    last_agent_output: str = ""
    timeline_path: str = ""
    event_log_path: str = ""
    run_id: str = ""
    completed_stages: list[str] = field(default_factory=list)


@dataclass
class AgentURLs:
    """HTTP endpoints for each agent."""

    scenario: str = "http://localhost:9001"
    audio: str = "http://localhost:9002"
    video: str = "http://localhost:9003"
    otio_gate: str = "http://localhost:9004"
    assembly: str = "http://localhost:9005"
    provisioner: str = "http://localhost:9006"


async def _call_agent(url: str, text: str, timeout: float = 300.0) -> str:
    """Call an agent via HTTP POST with plain text."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url.rstrip("/") + "/",
            content=text,
            headers={"Content-Type": "text/plain"},
        )
        resp.raise_for_status()
        return resp.text


def _record_effect(
    state: PipelineState, agent_id: str, agent_output: str
) -> None:
    """Parse agent output into effect and append to event store."""
    effect = parse_agent_text(agent_id, agent_output)
    store = EventStore(state.event_log_path)
    store.append(effect, otio_hash_before="")


def _read_otio(timeline_path: str) -> otio.schema.Timeline:
    """Read OTIO timeline from disk."""
    if os.path.exists(timeline_path):
        return otio.schema.Timeline.deserialize_from_file(timeline_path)
    return otio.schema.Timeline(name="documentary")


def _otio_has_audio(timeline: otio.schema.Timeline) -> bool:
    """Check if A1_Narration track has clips."""
    for track in timeline.tracks:
        if track.name == "A1_Narration" and len(list(track)) > 0:
            return True
    return False


def _otio_has_video(timeline: otio.schema.Timeline) -> bool:
    """Check if V1_Video track has clips."""
    for track in timeline.tracks:
        if track.name == "V1_Video" and len(list(track)) > 0:
            return True
    return False


def _output_exists(timeline_path: str) -> bool:
    """Check if final output MP4 exists."""
    output_dir = os.path.join(os.path.dirname(timeline_path), "output")
    return len(glob.glob(os.path.join(output_dir, "*.mp4"))) > 0


def _build_status(timeline: otio.schema.Timeline, last_output: str) -> str:
    """Build status report for agents."""
    has_audio = _otio_has_audio(timeline)
    has_video = _otio_has_video(timeline)
    return (
        f"Pipeline status:\n"
        f"- Audio track (A1_Narration): {'HAS clips' if has_audio else 'EMPTY'}\n"
        f"- Video track (V1_Video): {'HAS clips' if has_video else 'EMPTY'}\n"
        f"\nPrevious output:\n{last_output}\n"
    )


# ============================================================================
# Graph Builder
# ============================================================================

g = GraphBuilder(
    state_type=PipelineState,
    deps_type=AgentURLs,
    input_type=str,
    output_type=str,
)


@g.step
async def orchestrator_step(
    ctx: StepContext[PipelineState, AgentURLs, str],
) -> End[str]:
    """Pipeline orchestrator.

    Calls agents in sequence via HTTP. Reads OTIO state to decide routing.
    All agent communication is via HTTP — no in-process calls.
    """
    state = ctx.state
    deps = ctx.deps

    # 1. Run scenario agent
    result = await _call_agent(deps.scenario, ctx.inputs)
    state.last_agent_output = result
    _record_effect(state, "scenario", result)

    # Main loop: audio -> video -> assembly -> provisioner (as needed)
    max_iterations = 50
    for iteration in range(max_iterations):
        timeline = _read_otio(state.timeline_path)
        has_audio = _otio_has_audio(timeline)
        has_video = _otio_has_video(timeline)
        has_output = _output_exists(state.timeline_path)

        from job_queue import get_queue_summary
        audio_summary = get_queue_summary("audio")
        video_summary = get_queue_summary("video")
        pending = (
            audio_summary.get("pending", 0)
            + audio_summary.get("assigned", 0)
            + video_summary.get("pending", 0)
            + video_summary.get("assigned", 0)
        )

        status = _build_status(timeline, state.last_agent_output)

        if not has_audio:
            result = await _call_agent(deps.audio, status)
            state.last_agent_output = result
            _record_effect(state, "audio", result)
            continue

        if not has_video:
            result = await _call_agent(deps.video, status)
            state.last_agent_output = result
            _record_effect(state, "video", result)
            continue

        if not has_output:
            result = await _call_agent(deps.assembly, status)
            state.last_agent_output = result
            _record_effect(state, "assembly", result)
            continue

        if pending > 0:
            result = await _call_agent(deps.provisioner, status)
            state.last_agent_output = result
            _record_effect(state, "provisioner", result)
            continue

        # All complete
        return End(
            f"Pipeline complete. Output: {state.timeline_path}\n"
            f"Iterations: {iteration + 1}\n"
            f"Final status:\n{status}"
        )

    return End(f"Pipeline reached max iterations ({max_iterations}). Last status:\n{status}")


# ============================================================================
# Wire the graph
# ============================================================================

g.add(
    g.edge_from(g.start_node).to(orchestrator_step),
    g.edge_from(orchestrator_step).to(g.end_node),
)

pipeline_graph = g.build()
