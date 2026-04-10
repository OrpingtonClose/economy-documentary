"""
Scenario Director -- EvaluatorOptimizer pattern for script generation.

Generator agent reads corpus from state["corpus_path"], writes SCENARIO
markdown with V1/V2/V3 voice blocks per scene.

Evaluator agent checks ADHD compliance:
- Max 45s per scene
- 3 voices present per scene
- No rhetorical questions
- Visual variety mandate
- Dopamine hooks

Loops until evaluator rates GOOD or EXCELLENT.

Architecture::

    EvaluatorOptimizer("scenario_director")
    ├── Agent("scenario_generator")   # generates script
    └── Agent("scenario_evaluator")   # ADHD compliance check
"""

from __future__ import annotations

import json
import logging

from google.adk.agents import Agent
from google.adk.tools.exit_loop_tool import exit_loop

from agents.model_config import build_model
from callbacks.before_model import before_model_callback
from callbacks.after_model import after_model_callback
from callbacks.before_tool import before_tool_callback
from callbacks.after_tool import after_tool_callback
from callbacks.deterministic_steps import clean_scenes_after_scenario, extract_json_array
from tools.otio_tools import create_timeline_tool

logger = logging.getLogger(__name__)

# -- Generator agent -----------------------------------------------------------
_GENERATOR_INSTRUCTION = """\
You are the Scenario Director for an ADHD-friendly documentary pipeline.

Read the research corpus from {corpus_path} and the topic "{topic}".

You must output TWO things:

1. A MOVIE-LEVEL VISUAL STYLE directive in state["visual_style"] — a JSON object:
   {{
     "style": "<overall aesthetic, e.g. photorealistic documentary, stylized animation, painterly, etc.>",
     "realism_anchors": ["<terms that anchor the look, e.g. 4K, raw footage, no CGI, live action>"],
     "avoid": ["<things to avoid, e.g. cartoon, anime, CGI, morphing, illustration>"],
     "palette": "<colour/lighting direction, e.g. warm natural tones, golden hour>",
     "camera_language": "<default camera feel, e.g. stabilized handheld, tripod-locked, slow dolly>",
     "reference_genre": "<genre tag for LTX-2.3, e.g. Documentary, Period drama, Cinematic>"
   }}
   This is the ENTIRE FILM'S visual identity. Every downstream visual prompt and
   quality check will enforce it. Choose the style that best serves the topic.

2. A SCENARIO document as a JSON array of scenes in state["scenes"].

Each scene MUST have:
- scene_num: integer (1-based)
- title: short descriptive title
- duration_sec: target duration (MAX 45 seconds per scene)
- voices: array of exactly 3 voice blocks:
  - V1: "The Hook" — provocative opener, challenges assumptions
  - V2: "The Expert" — provides data, evidence, nuance
  - V3: "The Storyteller" — human angle, emotional connection
  Each voice block has: voice (V1/V2/V3), text (the narration), tone (descriptor)
- visual_notes: brief notes on visual approach for this scene
- dopamine_hook: what makes this scene grab attention in first 3 seconds

LANGUAGE MODE: "{language}"
- If "en": write all voice text in English only.
- If "ru": write all voice text in Russian only.
- If "dual_ru_en": write EACH voice block with BOTH languages using this format:
    "text": "[RU] <Russian narration>\n[EN] <English translation>"
  The Russian text is the PRIMARY narration; the English is a faithful translation.

RULES:
1. Each scene MUST be <= 45 seconds when spoken at natural pace (~150 words/min)
2. Each scene MUST use all 3 voices (V1, V2, V3)
3. NO rhetorical questions — make declarative statements
4. Each scene MUST specify different visual approaches for variety
5. Open with a dopamine hook — surprising fact, counterintuitive claim, or vivid image
6. Build narrative arc across scenes: hook → tension → insight → resolution
7. The visual_style MUST be consistent and appropriate for the topic

After generating both visual_style and scenes, create the OTIO timeline by calling
create_timeline(topic, num_scenes) where num_scenes = len(scenes).
This MUST be done before the Audio Agent runs.

Output the scene array as valid JSON in state["scenes"] and the visual style
object in state["visual_style"].
"""

scenario_generator = Agent(
    name="scenario_generator",
    model=build_model(),
    instruction=_GENERATOR_INSTRUCTION,
    tools=[create_timeline_tool],
    # NOTE: output_key="scenes" was removed intentionally.
    # ADK's output_key only saves the *final* text response.  When the
    # generator outputs scenes then calls create_timeline, the post-tool
    # response is often empty → output_key silently discards the scenes.
    # Instead, after_model_callback captures scenes from every LLM response
    # and persists them to both state["scenes"] and _scenes_backup.json.
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
    before_tool_callback=before_tool_callback,
    after_tool_callback=after_tool_callback,
)

# -- Callbacks (defined before agents that reference them) ---------------------


def _check_scenario_approval(callback_context):
    """After evaluator: save a backup of scenes when approved.

    The LoopAgent may re-run the generator even after GOOD approval
    (if the LLM forgot to call exit_loop). The second generator output
    may produce malformed JSON that overwrites state["scenes"].
    We save a backup here so clean_scenes_after_scenario can recover.
    """
    state = callback_context.state
    eval_output = state.get("_last_evaluator_output", "")
    if "APPROVED: true" in eval_output or "RATING: EXCELLENT" in eval_output or "RATING: GOOD" in eval_output:
        logger.info("Scenario approved by evaluator")
        # Save a backup of the current scenes state in case the loop
        # re-runs and the next generator output is malformed.
        raw_scenes = state.get("scenes", "")
        if raw_scenes:
            scenes = extract_json_array(str(raw_scenes))
            if scenes:
                state["_approved_scenes_backup"] = json.dumps(scenes)
                logger.info(
                    "Saved approved scenes backup: %d scenes", len(scenes)
                )
    return None


def _scenario_phase_setup(callback_context):
    """Set pipeline phase before scenario director runs.

    If the scenario stage was already completed in B2, skip the entire
    LoopAgent by returning Content (which ADK treats as 'agent already answered').
    """
    from google.genai import types as genai_types
    state = callback_context.state
    state["pipeline_phase"] = "scenario"
    stages_complete = state.get("_b2_stages_complete", [])
    if "scenario" in stages_complete:
        logger.info("B2: scenario stage already complete, skipping LoopAgent")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Scenario restored from B2 checkpoint — skipped.")],
        )
    return None


def _clean_scenes_after_scenario_wrapper(callback_context):
    """After scenario_director: clean scenes JSON then run timeline guardian."""
    return clean_scenes_after_scenario(callback_context)


# -- Evaluator agent -----------------------------------------------------------
_EVALUATOR_INSTRUCTION = """\
You are the ADHD Compliance Evaluator for a documentary script.

Read the generated scenes from {scenes} and evaluate them against these criteria:

HARD REQUIREMENTS (any failure = POOR):
1. Every scene has exactly 3 voice blocks (V1, V2, V3)
2. No scene exceeds 45 seconds (~112 words per scene max)
3. No rhetorical questions anywhere in the text
4. The scenes JSON is valid and parseable

QUALITY CRITERIA:
5. Visual variety: no two consecutive scenes should suggest the same visual approach
6. Dopamine hooks: each scene opens with something attention-grabbing
7. Narrative arc: scenes build toward insight, not just list facts
8. Voice distinctiveness: V1/V2/V3 sound genuinely different in tone

RATING:
- POOR: hard requirements violated
- FAIR: hard requirements met, but quality criteria mostly failed
- GOOD: hard requirements met, most quality criteria met
- EXCELLENT: everything works, narrative is compelling

Output format:
```
RATING: [POOR|FAIR|GOOD|EXCELLENT]
ISSUES:
- [list specific issues]
SUGGESTIONS:
- [list specific improvements]
```

If rating is GOOD or EXCELLENT:
1. Output "APPROVED: true"
2. Call the exit_loop() tool to signal that the scenario is accepted and the
   loop should stop.

IMPORTANT: You MUST call exit_loop() when the rating is GOOD or EXCELLENT.
Do NOT call exit_loop() if the rating is POOR or FAIR.
"""

scenario_evaluator = Agent(
    name="scenario_evaluator",
    model=build_model(synthesis=True),
    instruction=_EVALUATOR_INSTRUCTION,
    tools=[exit_loop],
    output_key="_last_evaluator_output",
    after_agent_callback=_check_scenario_approval,
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
    before_tool_callback=before_tool_callback,
    after_tool_callback=after_tool_callback,
)


# -- Combined agent using LoopAgent for evaluate-optimize loop -----------------
# ADK doesn't have a built-in EvaluatorOptimizer, so we implement it as a
# LoopAgent with generator + evaluator sub-agents. The evaluator has the
# exit_loop tool which sets event.actions.escalate=True to break the loop.
from google.adk.agents.loop_agent import LoopAgent


scenario_director = LoopAgent(
    name="scenario_director",
    description=(
        "Generates ADHD-friendly documentary scripts using an evaluate-optimize "
        "loop. Generator creates scenes with 3 voices per scene, evaluator "
        "checks ADHD compliance. Loops until GOOD or EXCELLENT rating."
    ),
    max_iterations=3,
    sub_agents=[scenario_generator, scenario_evaluator],
    before_agent_callback=_scenario_phase_setup,
    # Use clean_scenes_after_scenario which extracts JSON then runs timeline guardian
    after_agent_callback=_clean_scenes_after_scenario_wrapper,
)
