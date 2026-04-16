"""Video planner agent -- plans and executes video generation.

Replaces server/agents/visual_director.py, server/agents/production_supervisor.py,
and server/orchestrator/production_orchestrator.py.

Uses sub-agents as tools for content analysis, visual concepting, and coherence
evaluation, plus GPU tools for actual video generation.
"""

from __future__ import annotations

import logging
import os

from strands import Agent, tool
from strands.agent.conversation_manager.sliding_window_conversation_manager import SlidingWindowConversationManager
from strands.vended_plugins.skills import AgentSkills

from agents.model_config import build_model
from plugins.concurrency_plugin import ConcurrencyPlugin
from plugins.dashboard_plugin import DashboardPlugin
from plugins.rate_limit_plugin import RateLimitPlugin
import tools.b2_checkpoint as b2
from tools.otio_tools import add_video_clip, get_timeline_status
from tools.video_tools import generate_video_clip, probe_clip

logger = logging.getLogger(__name__)

_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")


@tool
def upload_checkpoint(local_path: str, b2_relative_path: str) -> str:
    """Upload a pipeline artifact to Backblaze B2 for checkpointing.

    Args:
        local_path: Local filesystem path to the file to upload.
        b2_relative_path: Relative path within the B2 bucket.

    Returns:
        Status message indicating success or failure.
    """
    try:
        success = b2.upload_file(local_path, b2_relative_path)
        if success:
            return f"Uploaded {local_path} to B2 as {b2_relative_path}"
        return f"Upload failed for {local_path}"
    except Exception as e:
        return f"Upload error: {e}"


@tool
def verify_production_plan(plan_json: str) -> str:
    """Verify a production plan against gatekeeper and contract rules.

    Args:
        plan_json: JSON string of the production plan.

    Returns:
        JSON string with verification results.
    """
    import json

    try:
        from orchestrator.plan_verifier import ProductionPlanVerifier

        plan = json.loads(plan_json)
        verifier = ProductionPlanVerifier()
        result = verifier.verify(plan)
        return json.dumps(result)
    except ImportError:
        return json.dumps({"status": "skipped", "message": "PlanVerifier module not available, verification not performed"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@tool
def save_visual_concepts(concepts_json: str, tool_context=None) -> str:
    """Persist visual concepts to shared pipeline state before production.

    MUST be called after generating visual concepts and before calling
    run_deterministic_production. Writes concepts to invocation_state
    and triggers duration normalization against actual narration timing.

    Args:
        concepts_json: JSON array of visual concept objects. Each concept
            should have scene_num, phrase_idx, prompt, duration, lora_id.

    Returns:
        Confirmation message with concept count.
    """
    import json

    if tool_context is None:
        return "Error: tool_context not available, cannot persist visual concepts"

    state = tool_context.invocation_state
    state["visual_concepts"] = concepts_json

    # Trigger duration normalization via write_visual_metadata_to_otio
    from callbacks._compat import StateDictProxy
    from callbacks.deterministic_steps import write_visual_metadata_to_otio

    class _Adapter:
        def __init__(self, s: dict) -> None:
            self.state = s if isinstance(s, StateDictProxy) else StateDictProxy(s)

    adapter = _Adapter(state)
    try:
        write_visual_metadata_to_otio(adapter)
    except Exception as exc:
        logger.warning("visual metadata OTIO write failed: %s", exc)

    # Count concepts for confirmation
    from callbacks.deterministic_steps import extract_json_array

    concepts = extract_json_array(concepts_json)
    count = len(concepts) if concepts else "unknown"
    logger.info("save_visual_concepts: persisted %s concepts to pipeline state", count)
    return f"Saved {count} visual concepts to pipeline state. Ready for run_deterministic_production."


@tool
def run_deterministic_production(tool_context=None) -> str:
    """Run deterministic video production for all scenes.

    Reads visual concepts from invocation_state, generates video clips,
    and writes results back. Uses the existing deterministic production
    callback logic for reliability.

    Prerequisite: save_visual_concepts must be called first to populate
    invocation_state with visual concepts.

    Returns:
        Status summary of video production.
    """
    from callbacks.deterministic_steps import deterministic_production_callback

    state = tool_context.invocation_state if tool_context else {}

    from callbacks._compat import StateDictProxy

    class _StateAdapter:
        def __init__(self, s: dict) -> None:
            self.state = s if isinstance(s, StateDictProxy) else StateDictProxy(s)

    adapter = _StateAdapter(state)

    try:
        result = deterministic_production_callback(adapter)
        if result is not None:
            return f"Video production complete. Result: {result}"
        return "Video production complete."
    except Exception as e:
        logger.exception("video production failed")
        return f"Video production failed: {e}"


# Sub-agent system prompts
_CONTENT_ANALYST_PROMPT = """\
You are a content analyst. Read narration text and WhisperX timing data.
Output a semantic map that identifies:
- Key concepts and themes per scene
- Emotional arc across scenes
- Timing constraints per visual phrase
- Suggested visual metaphors for abstract concepts
"""

_VISUAL_CONCEPTER_PROMPT = """\
You are a visual concepter for documentary video generation.
Given a semantic map, generate video prompts for LTX-2.3.
Follow the 5-layer prompt structure: subject+action, environment, camera, style, temporal.
Check your available_skills for ltx-prompt-craft and cinematography guidance.
Avoid human figures in complex scenarios. Use objects, landscapes, and nature instead.
"""

_COHERENCE_EVALUATOR_PROMPT = """\
You are a visual coherence evaluator. Review all generated video concepts
across the documentary and check for:
- Visual consistency (similar style, color palette, mood)
- Camera movement variety (no consecutive same movements)
- LoRA consistency within scenes
- Smooth visual transitions between scenes
Flag any issues and suggest fixes.
"""

VIDEO_PLANNER_PROMPT = """\
You are a video production planner. Given a documentary scenario with verified
audio timing, plan and execute video generation.

Use your sub-agents to analyze content, generate visual concepts, and evaluate
coherence. Then execute production using run_deterministic_production.

Check your available_skills for technique guidance on ltx-prompt-craft,
cinematography, batch-optimization, and recovery-strategies.

IMPORTANT WORKFLOW:
1. Analyze narration content and timing with content_analyst
2. Generate visual concepts with visual_concepter (output a JSON array of concepts,
   each with scene_num, phrase_idx, prompt, duration, lora_id, negative_prompt)
3. Evaluate coherence with coherence_evaluator
4. MANDATORY: Call save_visual_concepts with the concepts JSON array. This persists
   concepts to pipeline state and normalizes durations against narration timing.
   If you skip this step, run_deterministic_production will have no concepts to
   generate video for and the pipeline will fail.
5. Run production with run_deterministic_production
6. Verify results with get_timeline_status
"""


def build_video_planner() -> Agent:
    """Build and return the video planner agent with sub-agents."""
    technique_skills = AgentSkills(skills=[_SKILLS_DIR])

    content_analyst = Agent(
        name="content_analyst",
        system_prompt=_CONTENT_ANALYST_PROMPT,
        model=build_model(thinker=True),
        tools=[],
        plugins=[ConcurrencyPlugin()],
    )

    visual_concepter = Agent(
        name="visual_concepter",
        system_prompt=_VISUAL_CONCEPTER_PROMPT,
        model=build_model(),
        tools=[],
        plugins=[technique_skills, ConcurrencyPlugin()],
    )

    coherence_evaluator = Agent(
        name="coherence_evaluator",
        system_prompt=_COHERENCE_EVALUATOR_PROMPT,
        model=build_model(vision=True),
        tools=[],
        plugins=[ConcurrencyPlugin()],
    )

    return Agent(
        name="video_planner",
        system_prompt=VIDEO_PLANNER_PROMPT,
        model=build_model(),
        tools=[
            content_analyst.as_tool(
                "Analyze narration content and timing to create a semantic map"
            ),
            visual_concepter.as_tool(
                "Generate video prompts from semantic analysis"
            ),
            coherence_evaluator.as_tool(
                "Evaluate visual coherence across all generated concepts"
            ),
            save_visual_concepts,
            run_deterministic_production,
            verify_production_plan,
            generate_video_clip,
            probe_clip,
            add_video_clip,
            get_timeline_status,
            upload_checkpoint,
        ],
        plugins=[
            technique_skills,
            ConcurrencyPlugin(),
            RateLimitPlugin(),
            DashboardPlugin(),
        ],
        conversation_manager=SlidingWindowConversationManager(
            window_size=40,
        ),
    )
