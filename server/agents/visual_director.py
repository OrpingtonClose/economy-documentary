"""
Visual Director -- LoopAgent with 3 sub-agents for visual planning.

Architecture::

    LoopAgent("visual_director", max_iterations=3)
    ├── Agent("content_analyst")        # semantic analysis + LoRA selection
    ├── Agent("visual_concepter")       # simple, direct video prompts
    └── Agent("coherence_evaluator")    # quality gate

Content Analyst reads full narration text + WhisperX timing, identifies
semantic structure, determines visual breakpoints based on CONTENT SHIFTS.

Visual Concepter creates simple, direct video prompts per visual phrase.

Coherence Evaluator checks visual-narration alignment and rates quality.
If < GOOD, outputs feedback for next iteration. If >= GOOD, escalates.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent
from google.adk.agents.loop_agent import LoopAgent
from google.adk.tools.exit_loop_tool import exit_loop

from agents.model_config import build_model
from callbacks.before_model import before_model_callback
from callbacks.after_model import after_model_callback
from callbacks.before_tool import before_tool_callback
from callbacks.after_tool import after_tool_callback
from callbacks.deterministic_steps import write_visual_metadata_to_otio
from callbacks.timeline_guardian import timeline_guardian_callback
from tools.lora_tools import get_lora_details_tool, query_lora_catalog_tool

logger = logging.getLogger(__name__)

# -- Content Analyst -----------------------------------------------------------
_CONTENT_ANALYST_INSTRUCTION = """\
You are the Content Analyst for a documentary visual pipeline.

Read the full narration text from {scenes} and word-level timing from
{whisperx_alignment}. Your job is to understand the SEMANTIC STRUCTURE
of the narration and determine where visual changes should happen.

ANALYSIS PROCESS:
1. Read each scene's narration text with WhisperX timing
2. Identify semantic segments:
   - Concept explanations (when a new idea is introduced)
   - Examples (concrete illustrations of abstract ideas)
   - Transitions (bridges between topics)
   - Emotional beats (moments of impact, surprise, revelation)
3. Determine visual breakpoints based on CONTENT SHIFTS, not time
   - A visual change should happen when the MEANING changes
   - Longer visual phrases for sustained explanations
   - Shorter visual phrases for rapid-fire facts or emotional peaks
4. Query the LoRA catalog to select styles matching each visual phrase:
   - Use query_lora_catalog(content_type, mood, tags) to find matches
   - Use get_lora_details(lora_id) for full information
   - LoRA transitions should be MOTIVATED by narrative shifts

OUTPUT: Per-scene semantic map with:
- Visual phrase boundaries (start_time, end_time from WhisperX data)
- Content type for each phrase (explanation, example, transition, emotional)
- Suggested LoRA style with justification
- Visual mood keywords

Store the result as JSON in state["content_analysis"].
"""

content_analyst = Agent(
    name="content_analyst",
    model=build_model(thinker=True),
    instruction=_CONTENT_ANALYST_INSTRUCTION,
    tools=[query_lora_catalog_tool, get_lora_details_tool],
    output_key="content_analysis",
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
    before_tool_callback=before_tool_callback,
    after_tool_callback=after_tool_callback,
)

# -- Visual Concepter ----------------------------------------------------------
_VISUAL_CONCEPTER_INSTRUCTION = """\
You are the Visual Concepter for a documentary pipeline.

Read the content analysis from {content_analysis} which contains:
- Per-scene semantic maps with visual phrase boundaries
- Content types and LoRA selections
- Visual mood keywords

For EACH visual phrase, create a SIMPLE, DIRECT PROMPT:

Write prompts as SHORT, CONCRETE descriptions (1-2 sentences max).
Focus on ONE clear subject doing ONE clear action in ONE clear setting.

GOOD prompts (simple, concrete, filmable):
- "Golden cloudberries growing on a mossy bog in Finnish Lapland, soft morning light"
- "Close-up of hands picking ripe orange berries from low bushes, shallow depth of field"
- "Aerial view of vast Nordic marshland dotted with orange berries, golden hour"
- "Finnish 2-euro coin rotating slowly, cloudberry design visible, dark background"

BAD prompts (too complex, abstract, multi-layered):
- "A vibrant tapestry of cloudberry cultivation unfolds across the boreal landscape, 
   camera tracking through misty valleys as golden light illuminates..."
- "Split-screen infographic showing economic data overlaid on pastoral scenes..."
- "Conceptual visualization of market dynamics through abstract flowing shapes..."

RULES:
- ONE subject, ONE action, ONE setting per prompt
- NO abstract concepts, infographics, split-screens, or text overlays
- NO multi-clause descriptions — keep it filmable by AI video generation
- Vary camera angles between phrases (close-up, wide, aerial, tracking)
- Duration of each visual phrase comes from the content analysis timing

OUTPUT: JSON array of visual concepts, each containing:
- scene_num, phrase_idx, start_time, end_time, duration
- prompt (simple 1-2 sentence description)
- lora_id, lora_weight
- Camera style, environment, mood

Store the result in state["visual_concepts"].
"""

visual_concepter = Agent(
    name="visual_concepter",
    model=build_model(),
    instruction=_VISUAL_CONCEPTER_INSTRUCTION,
    output_key="visual_concepts",
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
)

# -- Coherence Evaluator -------------------------------------------------------
_COHERENCE_EVALUATOR_INSTRUCTION = """\
You are the Coherence Evaluator for a documentary visual pipeline.

Read the visual concepts from {visual_concepts} and the content analysis
from {content_analysis}.

EVALUATION CRITERIA:

1. NARRATIVE-VISUAL CONNECTION:
   Does each visual deeply connect to what the narration communicates at that
   exact moment? A clip about "rising inflation" shouldn't show generic cityscapes.

2. LORA TRANSITION MOTIVATION:
   Are LoRA style transitions motivated by narrative shifts? Style changes
   should feel intentional, not random. Check that transition_affinity rules
   from the LoRA catalog are respected.

3. VISUAL VARIETY:
   - No consecutive visual phrases with the same camera style
   - No consecutive visual phrases with the same environment
   - Diverse range of perspectives throughout

4. PROMPT QUALITY:
   - Prompts are simple and concrete (1-2 sentences, ONE subject/action/setting)
   - No abstract concepts, infographics, split-screens, or text overlays
   - Prompts are specific enough for AI video generation (no vague descriptions)
   - Durations are reasonable (2-15 seconds per visual phrase)

RATING:
- POOR: Major disconnects between narration and visuals
- FAIR: Some connection but lacks intentionality
- GOOD: Strong connection with minor issues
- EXCELLENT: Every visual choice is motivated and compelling

If POOR or FAIR:
- Output specific feedback for each problematic visual phrase
- Explain what the visual SHOULD convey vs what it currently conveys

If GOOD or EXCELLENT:
- Output "APPROVED: true"
- Call the exit_loop() tool to signal that visuals are accepted and the loop
  should stop.

IMPORTANT: You MUST call exit_loop() when the rating is GOOD or EXCELLENT.
Do NOT call exit_loop() if the rating is POOR or FAIR.

Store feedback in state["coherence_evaluation"].
"""


def _check_coherence_approval(callback_context):
    """After evaluator: log when visuals are approved."""
    state = callback_context.state
    eval_output = state.get("coherence_evaluation", "")
    if "APPROVED: true" in str(eval_output):
        logger.info("Visual concepts approved by coherence evaluator")
    return None


coherence_evaluator = Agent(
    name="coherence_evaluator",
    model=build_model(vision=True),
    instruction=_COHERENCE_EVALUATOR_INSTRUCTION,
    tools=[exit_loop],
    output_key="coherence_evaluation",
    after_agent_callback=_check_coherence_approval,
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
    before_tool_callback=before_tool_callback,
    after_tool_callback=after_tool_callback,
)


def _visual_phase_setup(callback_context):
    """Set pipeline phase before visual director runs."""
    callback_context.state["pipeline_phase"] = "visual_direction"
    return None


# -- Visual Director (LoopAgent) -----------------------------------------------
visual_director = LoopAgent(
    name="visual_director",
    description=(
        "Iterative visual planning loop: Content Analyst identifies semantic "
        "structure and selects LoRAs, Visual Concepter creates simple direct prompts, "
        "Coherence Evaluator checks narrative-visual alignment. Loops until "
        "GOOD or EXCELLENT rating."
    ),
    max_iterations=3,
    sub_agents=[content_analyst, visual_concepter, coherence_evaluator],
    before_agent_callback=_visual_phase_setup,
    # write_visual_metadata_to_otio writes gap metadata then runs timeline guardian
    after_agent_callback=write_visual_metadata_to_otio,
)
