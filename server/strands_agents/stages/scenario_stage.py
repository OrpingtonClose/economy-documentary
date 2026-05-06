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

import logging
from typing import Any

from strands import Agent
from strands.tools import tool

from strands_agents.otio_manager import OTIOStateManager

logger = logging.getLogger(__name__)

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
# Tools
# ---------------------------------------------------------------------------


@tool
def generate_scenario(corpus_path: str, topic: str, target_duration_sec: int = 420,
                      language: str = "en", max_scene_duration: int = 45) -> str:
    """Generate a documentary scenario from a research corpus.

    Reads the corpus, creates a visual style, style lock, and scene array.
    Returns the scenario as a JSON string.

    Args:
        corpus_path: Path to the research corpus file.
        topic: The documentary topic.
        target_duration_sec: Target total duration in seconds.
        language: Language mode (en, ru, dual_ru_en).
        max_scene_duration: Maximum seconds per scene.
    """
    # In production, this reads the corpus and generates the scenario.
    # The LLM agent handles the actual generation; this tool provides
    # the structure and validation.
    return f"[generate_scenario] Scenario generation requested for '{topic}' — placeholder"


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
    # In production, this runs the deterministic evaluator.
    return "[evaluate_scenario] Verdict: GOOD — placeholder"


@tool
def refine_scenario(scenario_json: str, feedback: str) -> str:
    """Refine a scenario based on evaluator feedback.

    Takes the current scenario and the evaluator's critique,
    then returns an improved version.

    Args:
        scenario_json: Current scenario as JSON string.
        feedback: The evaluator's feedback to address.
    """
    return "[refine_scenario] Refinement applied — placeholder"


@tool
def create_timeline(topic: str) -> str:
    """Create a new OTIO timeline for the documentary.

    Initializes the canonical track structure (V1_Video, A1_Narration, A2_Music).

    Args:
        topic: Documentary topic (used as timeline name).
    """
    return f"[create_timeline] Timeline '{topic}' created — placeholder"


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
    tools = [
        generate_scenario,
        evaluate_scenario,
        refine_scenario,
        create_timeline,
    ]

    # Add OTIO-aware tools if manager is available
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

    return Agent(
        name="scenario",
        system_prompt=_SCENARIO_INSTRUCTION,
        tools=tools,
        model=model,
    )
