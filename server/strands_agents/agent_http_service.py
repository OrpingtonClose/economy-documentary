"""FastAPI surface for a strands.Agent — HTTP base protocol.

Each agent runs as an independent HTTP service.
All endpoints speak free-flowing plain text (text/plain).

When an agent receives text, it is automatically parsed with instructor
before the agent "sees" it. Parsed structured data is stored in
agent.state so tools can access it without the LLM reasoning about parsing.

  GET  /  — inspect agent. Never interrupts running work.
  POST /  — interrupt current work, process text as new task, return result.
"""

from __future__ import annotations

import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import Response
from strands import Agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Automatic instructor parsing — transparent to agents
# ---------------------------------------------------------------------------

def _auto_parse(text: str, agent_name: str, agent: Agent) -> None:
    """Parse incoming text with instructor and store structured data in agent.state.

    This runs BEFORE the agent processes the text. The LLM never has to
    reason about parsing — it can access parsed data via tool_context.
    """
    # 1. Scenario text extraction (audio/video agents)
    if "--- SCENARIO TEXT ---" in text and "--- END SCENARIO ---" in text:
        try:
            start = text.index("--- SCENARIO TEXT ---") + len("--- SCENARIO TEXT ---")
            end = text.index("--- END SCENARIO ---")
            scenario_raw = text[start:end].strip()
            if scenario_raw:
                _parse_scenario(scenario_raw, agent)
        except Exception as exc:
            logger.warning("[%s] Scenario parse failed: %s", agent_name, exc)

    # 2. Audio agent output extraction (gate agent)
    if agent_name == "otio" and "artifact_path=" in text and ".wav" in text:
        try:
            _parse_audio_report(text, agent)
        except Exception as exc:
            logger.warning("[%s] Audio report parse failed: %s", agent_name, exc)

    # 3. Video agent output extraction (gate agent)
    if agent_name == "otio" and "artifact_path=" in text and ".mp4" in text:
        try:
            _parse_video_report(text, agent)
        except Exception as exc:
            logger.warning("[%s] Video report parse failed: %s", agent_name, exc)

    # 4. Clip artifacts extraction (assembly agent)
    if "--- CLIP ARTIFACTS ---" in text and "--- END CLIP ARTIFACTS ---" in text:
        try:
            start = text.index("--- CLIP ARTIFACTS ---") + len("--- CLIP ARTIFACTS ---")
            end = text.index("--- END CLIP ARTIFACTS ---")
            clips_json = text[start:end].strip()
            if clips_json:
                agent.state.set("parsed_clip_artifacts", json.loads(clips_json))
                logger.info("[%s] Parsed clip artifacts automatically", agent_name)
        except Exception as exc:
            logger.warning("[%s] Clip artifacts parse failed: %s", agent_name, exc)


def _parse_scenario(scenario_raw: str, agent: Agent) -> None:
    """Parse scenario text into structured scenes via instructor."""
    from structured_extract import extract
    from pydantic import BaseModel, Field
    from typing import List

    class Scene(BaseModel):
        title: str = Field(default="")
        duration_sec: int = Field(default=30)
        narration_v1_hook: str = Field(default="")
        narration_v2_expert: str = Field(default="")
        narration_v3_storyteller: str = Field(default="")
        visual_notes: str = Field(default="")
        dopamine_hook: str = Field(default="")

    class ScenarioDoc(BaseModel):
        scenes: List[Scene] = Field(default_factory=list)
        visual_style: dict = Field(default_factory=dict)
        style_lock: dict = Field(default_factory=dict)

    doc = extract(
        ScenarioDoc,
        scenario_raw,
        system_prompt="Extract structured documentary scenario data from the raw text. Identify all scenes with their narration scripts, visual notes, and timing.",
    )
    agent.state.set("parsed_scenario", {
        "scenes": [s.model_dump() for s in doc.scenes],
        "visual_style": doc.visual_style,
        "style_lock": doc.style_lock,
        "raw": scenario_raw,
    })
    logger.info("Auto-parsed scenario: %d scenes", len(doc.scenes))


def _parse_audio_report(text: str, agent: Agent) -> None:
    """Parse audio agent output into structured clip list via instructor."""
    from structured_extract import extract
    from pydantic import BaseModel, Field
    from typing import List

    class AudioClip(BaseModel):
        scene_num: int = Field(default=0)
        voice: str = Field(default="V1")
        wav_path: str = Field(default="")
        duration_sec: float = Field(default=5.0)

    class AudioReport(BaseModel):
        clips: List[AudioClip] = Field(default_factory=list)

    report = extract(
        AudioReport,
        text,
        system_prompt="Extract structured audio clip information from the agent's report. Identify scene numbers, voice roles, WAV file paths, and durations.",
    )
    agent.state.set("parsed_audio_clips", [c.model_dump() for c in report.clips])
    logger.info("Auto-parsed audio report: %d clips", len(report.clips))


def _parse_video_report(text: str, agent: Agent) -> None:
    """Parse video agent output into structured clip list via instructor."""
    from structured_extract import extract
    from pydantic import BaseModel, Field
    from typing import List

    class VideoClip(BaseModel):
        scene_num: int = Field(default=0)
        mp4_path: str = Field(default="")
        duration_sec: float = Field(default=5.0)
        lora_id: str = Field(default="")

    class VideoReport(BaseModel):
        clips: List[VideoClip] = Field(default_factory=list)

    report = extract(
        VideoReport,
        text,
        system_prompt="Extract structured video clip information from the agent's report. Identify scene numbers, MP4 file paths, durations, and LoRA IDs.",
    )
    agent.state.set("parsed_video_clips", [c.model_dump() for c in report.clips])
    logger.info("Auto-parsed video report: %d clips", len(report.clips))


# ---------------------------------------------------------------------------
# HTTP service builder
# ---------------------------------------------------------------------------

def build_agent_app(
    agent: Agent,
    name: str,
    agent_registry: dict[str, str] | None = None,
) -> FastAPI:
    """Construct an HTTP service wrapping a strands.Agent.

    Args:
        agent: The strands.Agent instance to expose.
        name: Human-readable agent name (scenario, audio, etc.).
        agent_registry: Mapping of agent names to their HTTP base URLs.
            Injected into agent.state so tools can discover and call other agents.

    Returns:
        FastAPI app ready to serve.
    """
    from strands.agent.conversation_manager import SlidingWindowConversationManager

    agent.conversation_manager = SlidingWindowConversationManager(
        window_size=50,
    )

    if agent_registry:
        agent.state.set("agent_registry", agent_registry)
        agent.state.set("self_url", agent_registry.get(name, ""))

    app = FastAPI(title=f"agent-{name}")

    _last_task: str = ""
    _last_result: str = ""
    _uptime_start: float = __import__("time").time()

    @app.get("/")
    def _inspect() -> Response:
        """Inspect agent without interrupting. Returns free-flowing text."""
        uptime = __import__("time").time() - _uptime_start
        lines = [f"I am the {name} agent."]
        if _last_task:
            lines.append(f"My last task was: {_last_task[:200]}")
        if _last_result:
            lines.append(f"My last result was: {_last_result[:200]}")
        lines.append(f"I have been running for {round(uptime, 1)} seconds.")
        return Response(
            content="\n".join(lines),
            media_type="text/plain",
        )

    @app.post("/")
    async def _invoke(request: Request) -> Response:
        """Receive raw text, auto-parse with instructor, invoke agent, return result."""
        nonlocal _last_task, _last_result
        body = await request.body()
        text = body.decode("utf-8").strip()
        if not text:
            return Response(
                content="error: empty body",
                media_type="text/plain",
                status_code=400,
            )

        _last_task = text
        logger.info("Agent '%s' received task: %s", name, text[:80])

        # ---- Automatic instructor parsing (transparent to agent) ----
        _auto_parse(text, name, agent)

        try:
            result = await agent.invoke_async(text)
            result_text = str(result)
            _last_result = result_text
            logger.info("Agent '%s' completed. Result length: %d chars", name, len(result_text))
            return Response(
                content=result_text,
                media_type="text/plain",
            )
        except Exception as exc:
            logger.exception("Agent '%s' failed: %s", name, exc)
            _last_result = f"error: {exc}"
            return Response(
                content=f"error: {exc}",
                media_type="text/plain",
                status_code=500,
            )

    return app
