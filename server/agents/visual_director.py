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

Read the content analysis from {content_analysis} and the MOVIE-LEVEL VISUAL
STYLE from {visual_style}. The visual_style defines the ENTIRE FILM'S look.
Every prompt you write MUST conform to this style directive.

You are writing prompts for LTX-2.3, an AI video generation model. LTX-2.3
responds to prompts written like CINEMATOGRAPHY SHOT DESCRIPTIONS, not keyword
tags. Think like a Director of Photography planning each shot.

PROMPT FORMAT (mandatory — follow this structure for EVERY prompt):
Write a SINGLE FLOWING PARAGRAPH (4-6 sentences) covering these elements in order:
1. SHOT SIZE + SUBJECT + ACTION: "A medium close-up of golden cloudberries
   glistening on a mossy bog as morning dew drips from their surfaces."
2. ENVIRONMENT + ATMOSPHERE: "The scene takes place in a vast Finnish
   marshland at dawn, thin mist hovering above dark peat water."
3. CAMERA MOVEMENT: "The camera performs a slow dolly forward at low angle,
   gliding just above the moss surface." (use ONE movement per shot)
4. LIGHTING + STYLE: "Lighting is soft golden-hour key with warm highlights
   and cool shadow fill. Shot on a 50mm lens, natural color." Include the
   realism_anchors from the visual_style (e.g. "4K", "raw footage").
5. TEMPORAL CHANGE: "Over time, the mist thins slightly as sunlight
   intensifies across the berries."

USE PRESENT-TENSE VERBS: "walks", "glows", "tilts" — never past tense.

INCLUDE REALISM ANCHORS from visual_style.realism_anchors in every prompt
(e.g. "4K", "raw footage", "no CGI", "live action").

AVOID everything in visual_style.avoid (these become the negative prompt).

LTX-2.3 STRENGTHS (lean into these):
- Cinematic compositions with thoughtful lighting and shallow depth of field
- Single-subject emotional expressions and subtle gestures
- Atmosphere: fog, mist, golden-hour light, rain, reflections
- Clear camera language: "slow dolly in", "handheld tracking", "crane up"
- Stylized aesthetics when requested by visual_style

LTX-2.3 WEAKNESSES (avoid these entirely):
- Complex human figures in historical/period scenarios (causes cartoon look)
- Multiple characters interacting in the same frame
- Text, logos, or readable writing
- Complex physics or chaotic motion
- Overloaded scenes with too many subjects or actions

INSTEAD OF HUMAN FIGURES, use:
- Close-ups of objects, tools, artifacts, hands
- Landscapes and environments that evoke the era
- Macro details: textures, materials, surfaces
- Atmospheric establishing shots
- Animals, nature, weather

CAMERA MOVEMENTS (pick ONE per shot):
- Tripod-locked (extremely stable)
- Slow dolly in / dolly out
- Crane up / crane down
- Pan left / pan right
- Truck left / truck right
- Slow orbit (partial arc)
- Handheld (controlled micro-shake)

RULES:
- ONE subject, ONE action, ONE setting per prompt
- NO abstract concepts, infographics, split-screens, or text overlays
- NO human figures in complex historical scenarios
- Vary camera movements between consecutive phrases
- Duration of each visual phrase comes from the content analysis timing

OUTPUT: JSON array of visual concepts, each containing:
- scene_num, phrase_idx, start_time, end_time, duration
- prompt (flowing cinematography paragraph, 4-6 sentences)
- negative_prompt (auto-generated from visual_style.avoid)
- lora_id, lora_weight
- camera_movement, environment, mood

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

Read the visual concepts from {visual_concepts}, the content analysis
from {content_analysis}, and the MOVIE-LEVEL VISUAL STYLE from {visual_style}.

EVALUATION CRITERIA:

1. MOVIE-LEVEL STYLE CONSISTENCY:
   Does EVERY prompt conform to the visual_style directive?
   - Style: Does each prompt's aesthetic match visual_style.style?
   - Realism anchors: Are visual_style.realism_anchors present in each prompt?
   - Avoidance: Does any prompt contain elements from visual_style.avoid?
   - Palette: Is the lighting/colour direction consistent with visual_style.palette?
   - Camera: Does the camera language match visual_style.camera_language?
   THIS IS THE MOST IMPORTANT CHECK. A beautifully written prompt that
   contradicts the movie's visual identity is a FAILURE.

2. LTX-2.3 PROMPT FORMAT:
   - Each prompt is a SINGLE FLOWING PARAGRAPH (4-6 sentences)
   - Uses present-tense verbs ("walks", "glows", not "walking", "glowing")
   - Follows the cinematography structure: shot+subject+action → environment →
     camera movement → lighting+style → temporal change
   - ONE camera movement per shot (no stacking)
   - ONE subject, ONE action, ONE setting (no overloading)
   - NO human figures in complex historical/period scenarios
   - NO abstract concepts, infographics, split-screens, or text overlays

3. NARRATIVE-VISUAL CONNECTION:
   Does each visual deeply connect to what the narration communicates at that
   exact moment? A clip about "rising inflation" shouldn't show generic cityscapes.

4. VISUAL VARIETY:
   - No consecutive visual phrases with the same camera movement
   - No consecutive visual phrases with the same environment
   - Diverse range of perspectives throughout

5. NEGATIVE PROMPT:
   - Each concept includes a negative_prompt derived from visual_style.avoid

RATING:
- POOR: Style violations, human figures in historical scenes, or major narrative disconnects
- FAIR: Mostly consistent but some prompts deviate from visual_style or use weak format
- GOOD: Strong style consistency with minor issues
- EXCELLENT: Every prompt is a perfectly formatted cinematography paragraph that
  matches the movie's visual identity

If POOR or FAIR:
- Output specific feedback for each problematic visual phrase
- Explain what the visual SHOULD convey vs what it currently conveys
- Flag any style violations against visual_style

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
    """Set pipeline phase before visual director runs.

    If the visual_direction stage was already completed in B2, skip the
    entire LoopAgent by returning Content.

    In quick-test mode, skip the full LLM-based visual planning loop
    (content_analyst + visual_concepter + coherence_evaluator) and
    generate simple visual concepts deterministically from scenes data.
    This reduces the visual direction stage from ~7 minutes (multiple
    LLM calls with deep reasoning) to <1 second.
    """
    import json
    import os
    from google.genai import types as genai_types
    state = callback_context.state
    state["pipeline_phase"] = "visual_direction"

    # B2 skip check FIRST — avoid blocking on infra pause for a completed stage
    stages_complete = state.get("_b2_stages_complete", [])
    if "visual_direction" in stages_complete:
        logger.info("B2: visual_direction stage already complete, skipping LoopAgent")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Visual direction restored from B2 checkpoint \u2014 skipped.")],
        )

    # QUICK-TEST: bypass entire LoopAgent with deterministic visual concepts
    quick_test = os.environ.get("DOCUMENTARY_QUICK_TEST", "").strip().lower() in ("1", "true", "yes")
    if quick_test:
        logger.info("QUICK-TEST: bypassing visual director LoopAgent with deterministic concepts")
        concepts = _generate_quick_test_concepts(state)
        if concepts:
            state["visual_concepts"] = json.dumps(concepts, ensure_ascii=False)
            state["content_analysis"] = json.dumps(
                {"mode": "quick-test", "scenes": len(concepts)},
                ensure_ascii=False,
            )
            logger.info("QUICK-TEST: generated %d visual concepts deterministically", len(concepts))
            return genai_types.Content(
                role="model",
                parts=[genai_types.Part(
                    text=f"Visual direction complete (quick-test): {len(concepts)} concepts generated deterministically."
                )],
            )
        else:
            logger.warning("QUICK-TEST: no scenes found, falling through to LLM-based visual planning")

    # INFRA: notify stage start for timing watchdog (only if stage will actually run)
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("visual_direction")
    check_infra_pause()

    return None


def _generate_quick_test_concepts(state) -> list:
    """Generate simple visual concepts from scenes data without LLM calls.

    For quick-test mode: one concept per scene using visual_notes from the
    scenario, a default LoRA, and simple cinematography prompts. No deep
    semantic analysis, no LoRA catalog queries, no coherence evaluation.
    """
    import json
    from callbacks.deterministic_steps import extract_json_array

    raw_scenes = state.get("scenes", "[]")
    scenes = extract_json_array(str(raw_scenes))
    if not scenes:
        return []

    # Extract visual style if available
    raw_vs = str(state.get("visual_style", ""))
    realism_anchors = "4K, raw footage, natural lighting"
    avoid_list = "CGI, cartoon, anime, text overlay, split screen"
    try:
        vs = json.loads(raw_vs) if raw_vs.strip() else {}
        if isinstance(vs, dict):
            anchors = vs.get("realism_anchors", [])
            if anchors:
                realism_anchors = ", ".join(anchors)
            avoid = vs.get("avoid", [])
            if avoid:
                avoid_list = ", ".join(avoid)
    except (json.JSONDecodeError, TypeError):
        pass

    concepts = []
    for scene in scenes:
        sn = scene.get("scene_num", 0)
        title = scene.get("title", "scene")
        visual_notes = scene.get("visual_notes", "")
        duration = scene.get("duration_sec", 13.0)

        # Build a simple cinematography prompt from visual_notes
        if visual_notes:
            base_desc = visual_notes
        else:
            base_desc = f"Documentary footage related to {title}"

        prompt = (
            f"{base_desc} "
            f"The camera performs a slow dolly forward, capturing the scene "
            f"with shallow depth of field. "
            f"Lighting is soft and natural with warm highlights. "
            f"Shot in {realism_anchors}. "
            f"Over time, the light shifts subtly as the scene evolves."
        )

        concepts.append({
            "scene_num": sn,
            "phrase_idx": 0,
            "start_time": 0.0,
            "end_time": min(duration, 10.0),
            "duration": min(duration, 10.0),
            "prompt": prompt,
            "negative_prompt": avoid_list,
            "lora_id": "documentary-realism",
            "lora_weight": 0.75,
            "camera_movement": "slow dolly forward",
            "environment": title,
            "mood": "documentary",
        })

    return concepts


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
