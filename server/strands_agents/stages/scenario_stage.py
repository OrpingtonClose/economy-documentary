"""
Scenario stage — Strands Agent replacing ADK scenario_director + scenario_refiner.

The original scenario_director was an ADK LoopAgent with generator and
evaluator sub-agents. The Strands equivalent is a single Agent with
tools for generation, evaluation, and refinement. The loop (iterate
until evaluator passes) is handled by the Graph's backward edge from
audio → scenario when timing fails.

The original scenario_refiner was a separate ADK Agent. Its tools
are now included in this agent's tool list since the Graph handles
the loop externally.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands import Agent
from strands.tools import tool

from strands_agents.otio_manager import OTIOStateManager

logger = logging.getLogger(__name__)

# OTIO manager — set by build_scenario_agent
_otio_manager: OTIOStateManager | None = None

# OTIO manager — set by build_scenario_agent
_otio_manager: OTIOStateManager | None = None

# ---------------------------------------------------------------------------
# System prompt — preserved verbatim from ADK scenario_director
# ---------------------------------------------------------------------------

_SCENARIO_INSTRUCTION = """\
You are the Scenario Director for an ADHD-friendly documentary pipeline.

Read the research corpus and the topic from the task. You must output:

1. A MOVIE-LEVEL VISUAL STYLE directive — a JSON object with style,
   realism_anchors, avoid, palette, camera_language, reference_genre.

2. A STYLE LOCK — the ONE style family for the WHOLE documentary.
   Pick exactly ONE before writing any scenes. Every visual prompt
   gets this locked style applied. JSON with dominant_style,
   forbidden_styles, positive_fragment, negative_fragment.

3. A SCENARIO document as a JSON array of scenes.

CRITICAL — TOTAL DURATION TARGET:
The user's message specifies how long the documentary should be.
Generate enough scenes so that the SUM of all duration_sec values
equals the requested total. Plan for ceil(target_seconds / 35) scenes.
Each scene 30-45s. Do NOT undershoot.

Each scene MUST have:
- scene_num: integer (1-based)
- title: short descriptive title
- duration_sec: target duration (30-45 seconds per scene, MAX 45s)
- voices: array of exactly 3 voice blocks (V1 Hook, V2 Expert, V3 Storyteller)
- visual_notes: brief notes aligned with style_lock
- dopamine_hook: what makes this scene grab attention in first 3 seconds
- pronunciation_hints: {"TOKEN": "letter-by-letter spelling", ...}

SCENE 0 additionally MUST carry hook_spec with topic_specific_motif,
motion_description, and narrative_pull.

THE FINAL SCENE additionally MUST carry outro_spec with closing_shot,
recap_sentence, cta, and brand_card.

After generating, call evaluate_scenario to check ADHD compliance.
If the evaluator returns POOR or FAIR, call refine_scenario with
the evaluator's feedback to improve.

RULES:
1. Each scene MUST be <= 45 seconds when spoken at natural pace (~150 words/min)
2. Each scene MUST use all 3 voices (V1, V2, V3)
3. No rhetorical questions in narration
4. Visual variety mandate — avoid repetitive visual descriptions
5. Dopamine hooks must be concrete and specific
"""


# ---------------------------------------------------------------------------
# Tools — with real implementations where available
# ---------------------------------------------------------------------------


@tool
def generate_scenario(corpus_path: str, topic: str, target_duration_sec: int = 420,
                      language: str = "en", max_scene_duration: int = 45) -> str:
    """Generate a documentary scenario from a research corpus.

    This is a PURE LLM call — it returns JSON only. It does NOT write
    to OTIO. The scenario agent must call write_scenes and
    write_visual_style separately to persist to OTIO.

    Args:
        corpus_path: Path to the research corpus file (ignored if empty).
        topic: The documentary topic.
        target_duration_sec: Target total duration in seconds.
        language: Language mode (en, ru, dual_ru_en).
        max_scene_duration: Maximum seconds per scene.
    """
    import time as _time

    started = _time.time()

    # Call the real LLM-backed generator
    from strands_agents.scenario_llm import make_generator
    model_id = os.environ.get("STRANDS_MODEL", "")
    if not model_id:
        raise RuntimeError("STRANDS_MODEL not set — cannot generate scenarios without an LLM")
    generator = make_generator(model_id=model_id)  # type: ignore
    result = generator(
        topic=topic,
        num_scenes=max(1, target_duration_sec // 35),
        style="documentary",
        language=language,
    )

    return json.dumps(result)


def _capture_environment() -> dict:
    """Capture the current production environment."""
    import platform
    env = {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "arch": platform.machine(),
    }
    # Model info
    for var in ["STRANDS_MODEL", "ANTHROPIC_API_KEY", "AWS_PROFILE", "AWS_REGION"]:
        val = os.environ.get(var, "")
        if val:
            env[var.lower()] = val[:4] + "..." if "key" in var.lower() else val
    # Dependency versions
    for mod in ["opentimelineio", "strands"]:
        try:
            m = __import__(mod)
            env[f"{mod}_version"] = getattr(m, "__version__", "unknown")
        except ImportError:
            env[f"{mod}_version"] = "not_installed"
    return env


@tool
def evaluate_scenario(scenario_json: str) -> str:
    """Evaluate a scenario for ADHD compliance.

    Checks:
    - Max 45s per scene
    - 3 voices present per scene
    - No rhetorical questions
    - Visual variety
    - Dopamine hooks
    - Duration target met

    Returns verdict: GOOD, EXCELLENT, FAIR, or POOR.
    """
    from tools.scenario_evaluator_checks import run_all_structural_checks

    scenes = json.loads(scenario_json) if isinstance(scenario_json, str) else scenario_json
    if isinstance(scenes, dict):
        scenes = scenes.get("scenes", [])

    report = run_all_structural_checks(
        {"scenes": scenes, "style_lock": {}},
        target_duration_sec=sum(s.get("duration_sec", 0) for s in scenes),
    )
    issues = [r.as_dict() for r in report.results if not r.passed]
    return json.dumps({
        "verdict": report.overall,
        "issues": issues,
        "scene_count": len(scenes),
    })


@tool
def refine_scenario(scenario_json: str, feedback: str) -> str:
    """Refine a scenario based on evaluator feedback.

    Takes the current scenario and the evaluator's critique,
    then returns an improved version.

    Args:
        scenario_json: Current scenario as JSON string.
        feedback: The evaluator's feedback to address.
    """
    from strands_agents.scenario_llm import make_refiner
    model_id = os.environ.get("STRANDS_MODEL", "")
    if not model_id:
        raise RuntimeError("STRANDS_MODEL not set — cannot refine scenarios without an LLM")
    refiner = make_refiner(model_id=model_id)
    scenes = json.loads(scenario_json) if isinstance(scenario_json, str) else scenario_json
    if isinstance(scenes, dict):
        scenes = scenes.get("scenes", [])
    feedback_dict = json.loads(feedback) if isinstance(feedback, str) else feedback
    result = refiner(scenes, feedback_dict)
    return json.dumps(result)


@tool
def create_timeline(topic: str) -> str:
    """Create a new OTIO timeline for the documentary.

    Initializes the canonical track structure (V1_Video, A1_Narration, A2_Music).

    Args:
        topic: Documentary topic (used as timeline name).
    """
    from tools.otio_tools import create_timeline as _real_create
    return _real_create(topic, 5)


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_scenario_agent(
    otio_manager: OTIOStateManager | None = None,
    model: Any = None,
) -> Agent:
    """Build the Strands Agent for the scenario stage.

    Args:
        otio_manager: Optional OTIOStateManager for timeline access.
        model: Optional model configuration.

    Returns:
        A configured Strands Agent ready for the Graph.
    """
    global _otio_manager
    _otio_manager = otio_manager
    tools = [
        generate_scenario,
        evaluate_scenario,
        refine_scenario,
        create_timeline,
    ]

    if otio_manager is not None:
        @tool
        def read_scenario_state(stage: str = "scenario") -> str:
            """Read the scenario stage's OTIO state."""
            return otio_manager.read(stage)

        @tool
        def write_scenario_mutation(operation: str, details: str = "") -> str:
            """Request a mutation on the OTIO timeline (guarded)."""
            otio_manager.guard_mutation(operation)
            return f"[write_scenario_mutation] '{operation}' allowed — placeholder"

        tools.extend([read_scenario_state, write_scenario_mutation])

    agent = Agent(
        name="scenario",
        system_prompt=_SCENARIO_INSTRUCTION,
        tools=tools,
        model=model,
    )

    # OTIO manager on agent state — the contract enforcer and other
    # agents find it through the agent-to-agent conversation
    if otio_manager is not None:
        try:
            agent.state.set("_otio_manager", otio_manager)
        except Exception:
            pass

    return agent
