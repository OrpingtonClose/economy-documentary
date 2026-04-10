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

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.timeline_guardian import timeline_guardian_callback
from tools.otio_tools import create_timeline_tool

logger = logging.getLogger(__name__)

# -- Generator agent -----------------------------------------------------------
_GENERATOR_INSTRUCTION = """\
You are the Scenario Director for an ADHD-friendly documentary pipeline.

Read the research corpus from {corpus_path} and the topic "{topic}".
Generate a SCENARIO document as a JSON array of scenes.

Each scene MUST have:
- scene_num: integer (1-based)
- title: short descriptive title
- duration_sec: target duration (MAX 45 seconds per scene)
- voices: array of exactly 3 voice blocks:
  - V1: "The Hook" — provocative opener, challenges assumptions
  - V2: "The Expert" — provides data, evidence, nuance
  - V3: "The Storyteller" — human angle, emotional connection
  Each voice block has: voice (V1/V2/V3), text (the narration), tone (descriptor)
- visual_notes: brief notes on visual style for this scene
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

After generating scenes, create the OTIO timeline by calling
create_timeline(topic, num_scenes) where num_scenes = len(scenes).
This MUST be done before the Audio Agent runs.

Output the scene array as valid JSON in state["scenes"].
"""

scenario_generator = Agent(
    name="scenario_generator",
    model=build_model(),
    instruction=_GENERATOR_INSTRUCTION,
    tools=[create_timeline_tool],
    output_key="scenes",
)

# -- Callbacks (defined before agents that reference them) ---------------------


def _check_scenario_approval(callback_context):
    """After evaluator: check if scenario is approved to break loop."""
    state = callback_context.state
    # Check the last evaluator output for APPROVED or GOOD/EXCELLENT
    eval_output = state.get("_last_evaluator_output", "")
    if "APPROVED: true" in eval_output or "RATING: EXCELLENT" in eval_output or "RATING: GOOD" in eval_output:
        state["escalate"] = True
        logger.info("Scenario approved by evaluator")
    return None


def _scenario_phase_setup(callback_context):
    """Set pipeline phase before scenario director runs."""
    callback_context.state["pipeline_phase"] = "scenario"
    return None


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

If rating is GOOD or EXCELLENT, also output:
APPROVED: true
"""

scenario_evaluator = Agent(
    name="scenario_evaluator",
    model=build_model(synthesis=True),
    instruction=_EVALUATOR_INSTRUCTION,
    output_key="_last_evaluator_output",
    after_agent_callback=_check_scenario_approval,
)


# -- Combined agent using LoopAgent for evaluate-optimize loop -----------------
# ADK doesn't have a built-in EvaluatorOptimizer, so we implement it as a
# LoopAgent with generator + evaluator sub-agents. The evaluator's
# after_agent_callback sets escalate=True when quality is GOOD or EXCELLENT.
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
    after_agent_callback=timeline_guardian_callback,
)
