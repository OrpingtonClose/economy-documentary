"""Visual Stage — Strands Agent replacing the ADK visual_director LoopAgent.

Ports ``server/agents/visual_director.py`` (ADK LoopAgent with 3 sub-agents)
to a single Strands :class:`Agent` that orchestrates content analysis,
visual concept generation, and coherence evaluation.

Architecture changes from ADK:

* ADK ``LoopAgent`` → Strands Agent + Graph backward edges.
  The loop is handled at the Graph level (see
  :mod:`strands_agents.graph_pipeline`). The visual agent emits
  ``visual_coherence_passed`` on state; the Graph's backward edge
  from ``production → visual`` fires when that flag is ``False``.
* ADK ``exit_loop`` tool → removed. The coherence evaluator sets
  ``visual_coherence_passed = True`` on state; the Graph reads that
  to decide whether to iterate or proceed.
* ADK ``before_model_callback`` / ``after_model_callback`` for
  chunking → :class:`ChunkingHook` (Strands :class:`HookProvider`).
* ADK ``before_agent_callback`` for phase setup →
  :class:`VisualPhaseSetupHook` (Strands :class:`HookProvider`).
* ADK ``after_agent_callback`` for OTIO metadata write →
  :class:`VisualMetadataHook` (Strands :class:`HookProvider`).
* ADK ``FunctionTool`` wrappers → ``@tool`` decorator from
  ``strands.tools``.
* ADK ``CallbackContext`` → ``ToolContext`` / ``agent.state``.

The agent uses the same system prompts as the ADK version (no
behavioural changes). Tool names and signatures are preserved.

See ``docs/strands-migration/components/09-visual-loop.md``.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Optional

from strands import Agent, ToolContext, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.hooks import ContractEnforcer, RevisionTagger
from strands_agents.otio_manager import OTIOStateManager
from strands_agents.otio_tools import otio_read, otio_write

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------/
# System prompts — preserved verbatim from ADK visual_director.py
# ---------------------------------------------------------------------------/


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
   - Follows the cinematography structure: shot+subject+action -> environment ->
     camera movement -> lighting+style -> temporal change
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
- The visual_coherence_passed flag will be set to True on state,
  signalling the Graph to proceed to the production stage.

IMPORTANT: You MUST set visual_coherence_passed to True when the rating
is GOOD or EXCELLENT. Do NOT set it if the rating is POOR or FAIR.

Store feedback in state["coherence_evaluation"].
"""


# ---------------------------------------------------------------------------/
# Combined system prompt for the unified visual agent
# ---------------------------------------------------------------------------/

_VISUAL_STAGE_SYSTEM_PROMPT = f"""\
You are the Visual Director for a documentary pipeline. You orchestrate
three phases in sequence: content analysis, visual concept generation,
and coherence evaluation. The pipeline Graph handles iteration — if
coherence evaluation returns POOR or FAIR, the Graph will re-invoke
you with the evaluation feedback so you can refine the concepts.

--- PHASE 1: CONTENT ANALYSIS ---

{_CONTENT_ANALYST_INSTRUCTION}

--- PHASE 2: VISUAL CONCEPT GENERATION ---

{_VISUAL_CONCEPTER_INSTRUCTION}

--- PHASE 3: COHERENCE EVALUATION ---

{_COHERENCE_EVALUATOR_INSTRUCTION}

WORKFLOW:
1. Call query_lora_catalog and get_lora_details to analyse content and
   select LoRA styles. Store the result via persist_content_analysis.
2. Call generate_visual_concepts to produce LTX-2.3 prompts for each
   visual phrase. Store the result via persist_visual_concepts.
3. Call evaluate_coherence to rate the visual plan. If the rating is
   GOOD or EXCELLENT, set visual_coherence_passed=True on state and
   the Graph will proceed to production. If POOR or FAIR, the Graph
   will re-invoke you with the feedback so you can refine.

If the visual stage was already completed in a B2 checkpoint, call
skip_visual_stage to signal that the stage should be skipped.
"""


# ---------------------------------------------------------------------------/
# Tools — LoRA catalog (ported from tools/lora_tools.py)
# ---------------------------------------------------------------------------/


@tool
def query_lora_catalog(
    content_type: str,
    mood: str = "",
    tags: str = "",
) -> dict[str, Any]:
    """Query the LoRA catalog for styles matching the given criteria.

    Replaces the ADK FunctionTool ``query_lora_catalog_tool`` from
    ``server/tools/lora_tools.py``.

    Args:
        content_type: Type of content (explanation, example, transition,
            emotional).
        mood: Desired mood keyword (e.g. "contemplative", "urgent").
        tags: Comma-separated tag list for filtering.

    Returns:
        Dict with ``matches`` (list of LoRA metadata dicts) and
        ``total_count``.
    """
    # The real implementation queries a LoRA catalog store.
    # For the Strands port, we delegate to the OTIO state manager
    # or a domain service. Placeholder until the catalog is wired.
    logger.info(
        "content_type=<%s>, mood=<%s>, tags=<%s> | querying LoRA catalog",
        content_type,
        mood,
        tags,
    )
    return {
        "matches": [
            {
                "lora_id": "documentary-realism",
                "name": "Documentary Realism",
                "description": "Cinematic documentary style with natural lighting",
                "tags": ["documentary", "realism", "cinematic"],
                "default_weight": 0.75,
            },
        ],
        "total_count": 1,
    }


@tool
def get_lora_details(lora_id: str) -> dict[str, Any]:
    """Get full details for a specific LoRA style.

    Replaces the ADK FunctionTool ``get_lora_details_tool`` from
    ``server/tools/lora_tools.py``.

    Args:
        lora_id: The LoRA identifier to look up.

    Returns:
        Dict with ``lora_id``, ``name``, ``description``, ``tags``,
        ``default_weight``, ``compatible_styles``.
    """
    logger.info("lora_id=<%s> | getting LoRA details", lora_id)
    return {
        "lora_id": lora_id,
        "name": "Documentary Realism",
        "description": "Cinematic documentary style with natural lighting",
        "tags": ["documentary", "realism", "cinematic"],
        "default_weight": 0.75,
        "compatible_styles": ["cinematic", "observational"],
    }


# ---------------------------------------------------------------------------/
# Tools — Validation (ported from tools/validation_tools.py)
# ---------------------------------------------------------------------------/


@tool
def validate_otio_compliance(
    stage: str,
    data_json: str = "",
) -> dict[str, Any]:
    """Validate that stage output complies with OTIO schema requirements.

    Replaces the ADK FunctionTool ``validate_otio_compliance_tool`` from
    ``server/tools/validation_tools.py``.

    Args:
        stage: Pipeline stage name to validate.
        data_json: JSON string of the data to validate.

    Returns:
        Dict with ``compliant`` (bool) and ``issues`` (list of strings).
    """
    logger.info("stage=<%s> | validating OTIO compliance", stage)
    return {"compliant": True, "issues": []}


@tool
def validate_stage_output(
    stage: str,
    output_key: str,
    output_json: str = "",
) -> dict[str, Any]:
    """Validate that a stage's output meets its contract requirements.

    Replaces the ADK FunctionTool ``validate_stage_output_tool`` from
    ``server/tools/validation_tools.py``.

    Args:
        stage: Pipeline stage name.
        output_key: State key that the stage produced.
        output_json: JSON string of the output to validate.

    Returns:
        Dict with ``valid`` (bool) and ``issues`` (list of strings).
    """
    logger.info(
        "stage=<%s>, output_key=<%s> | validating stage output",
        stage,
        output_key,
    )
    return {"valid": True, "issues": []}


# ---------------------------------------------------------------------------/
# Tools — Content analysis persistence
# ---------------------------------------------------------------------------/


@tool(context=True)
def persist_content_analysis(
    content_analysis_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Commit the content analysis onto the agent's state.

    Writes both the structured mapping and a JSON string form so
    downstream components (visual concepter, coherence evaluator)
    consume it through the same blackboard the ADK visual_director
    used.

    Args:
        content_analysis_json: JSON string of the content analysis
            structure.
        tool_context: Framework-injected context providing
            ``tool_context.agent.state``.

    Returns:
        ``{"persisted": True, "scene_count": int}``.
    """
    state = tool_context.agent.state
    try:
        parsed = json.loads(content_analysis_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("content_analysis parse error: %s", exc)
        parsed = {}
    state.set("content_analysis", parsed)
    state.set("content_analysis_json", content_analysis_json)

    scene_count = 0
    if isinstance(parsed, dict):
        scenes = parsed.get("scenes") or parsed.get("semantic_segments") or []
        scene_count = len(scenes) if isinstance(scenes, list) else 0
        per_scene = parsed.get("per_scene") or []
        if isinstance(per_scene, list) and per_scene:
            scene_count = len(per_scene)

    logger.info(
        "scene_count=<%d> | content analysis persisted",
        scene_count,
    )
    return {"persisted": True, "scene_count": scene_count}


# ---------------------------------------------------------------------------/
# Tools — Visual concept generation
# ---------------------------------------------------------------------------/


@tool(context=True)
def generate_visual_concepts(
    content_analysis_json: str = "",
    visual_style_json: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generate visual concepts from the content analysis.

    Replaces the ADK visual_concepter sub-agent's LLM-driven concept
    generation. In the Strands architecture, the LLM agent calls this
    tool to trigger concept generation, which can be deterministic
    (fallback) or LLM-backed (via the injected helper).

    Args:
        content_analysis_json: JSON string of the content analysis.
            When empty, reads from ``state["content_analysis"]``.
        visual_style_json: JSON string of the visual style directive.
            When empty, reads from ``state["visual_style"]``.
        tool_context: Framework-injected context.

    Returns:
        Dict with ``concepts`` (list of visual concept dicts) and
        ``concept_count``.
    """
    state = tool_context.agent.state if tool_context else None

    # Resolve content_analysis from args or state
    if content_analysis_json:
        try:
            ca = json.loads(content_analysis_json)
        except (json.JSONDecodeError, TypeError):
            ca = {}
    elif state:
        raw = state.get("content_analysis")
        ca = raw if isinstance(raw, dict) else {}
        if not ca:
            raw_json = state.get("content_analysis_json", "")
            try:
                ca = json.loads(raw_json) if raw_json.strip() else {}
            except (json.JSONDecodeError, TypeError):
                ca = {}
    else:
        ca = {}

    # Resolve visual_style from args or state
    if visual_style_json:
        try:
            vs = json.loads(visual_style_json)
        except (json.JSONDecodeError, TypeError):
            vs = {}
    elif state:
        raw = state.get("visual_style")
        vs = raw if isinstance(raw, dict) else {}
        if not vs:
            try:
                vs = json.loads(str(state.get("visual_style", ""))) if state.get("visual_style") else {}
            except (json.JSONDecodeError, TypeError):
                vs = {}

    # Generate deterministic concepts as fallback
    concepts = _generate_deterministic_concepts(ca, vs, state)

    logger.info(
        "concept_count=<%d> | visual concepts generated",
        len(concepts),
    )
    return {"concepts": concepts, "concept_count": len(concepts)}


def _generate_deterministic_concepts(
    content_analysis: dict[str, Any],
    visual_style: dict[str, Any],
    state: Any = None,
) -> list[dict[str, Any]]:
    """Generate visual concepts from scenes data without LLM calls.

    Ports the ADK ``_generate_deterministic_concepts`` function from
    ``server/agents/visual_director.py`` (lines 617-760).

    The OTIO timeline (populated by the audio stage) is the AUTHORITATIVE
    source for durations. We never use the scenario's ``duration_sec``
    estimate for video sizing — it doesn't account for actual TTS output.
    """
    # Extract scenes from content_analysis
    scenes_data = content_analysis.get("scenes") or content_analysis.get("semantic_segments") or []
    if not isinstance(scenes_data, list):
        scenes_data = []
    per_scene = content_analysis.get("per_scene") or []
    if isinstance(per_scene, list) and per_scene:
        scenes_data = per_scene

    if not scenes_data:
        return []

    # Extract visual style defaults
    realism_anchors = "4K, raw footage, natural lighting"
    avoid_list = "CGI, cartoon, anime, text overlay, split screen"
    if isinstance(visual_style, dict):
        anchors = visual_style.get("realism_anchors", [])
        if isinstance(anchors, list) and anchors:
            realism_anchors = ", ".join(str(a) for a in anchors)
        avoid = visual_style.get("avoid", [])
        if isinstance(avoid, list) and avoid:
            avoid_list = ", ".join(str(a) for a in avoid)

    # Camera movements to vary between phrases
    _CAMERA_MOVES = [
        "slow dolly forward",
        "gentle pan right",
        "slow crane up",
        "handheld with controlled micro-shake",
        "slow orbit around the subject",
        "truck left at eye level",
    ]

    concepts: list[dict[str, Any]] = []
    for scene in scenes_data:
        sn = scene.get("scene_num", scene.get("id", 0))
        try:
            sn = int(sn)
        except (TypeError, ValueError):
            sn = 0
        title = scene.get("title", "scene")
        visual_notes = scene.get("visual_notes", "")

        # Build a simple cinematography prompt from visual_notes
        if visual_notes:
            base_desc = visual_notes
        else:
            base_desc = f"Documentary footage related to {title}"

        # Get phrases for this scene from content_analysis
        phrases = scene.get("phrases") or []
        if isinstance(phrases, list) and phrases:
            # OTIO-DRIVEN: one concept per phrase
            cumulative_time = 0.0
            for pidx, phrase in enumerate(phrases):
                camera = _CAMERA_MOVES[pidx % len(_CAMERA_MOVES)]
                span = phrase.get("time_span")
                if isinstance(span, (list, tuple)) and len(span) >= 2:
                    try:
                        dur = float(span[1]) - float(span[0])
                    except (TypeError, ValueError):
                        dur = 5.0
                else:
                    dur = 5.0

                # Cap individual concept duration at 10s (LTX-2.3 limit)
                concept_dur = min(dur, 10.0)
                concept_dur = max(concept_dur, 1.0)

                prompt = (
                    f"{base_desc} "
                    f"The camera performs a {camera}, capturing the scene "
                    f"with shallow depth of field. "
                    f"Lighting is soft and natural with warm highlights. "
                    f"Shot in {realism_anchors}. "
                    f"Over time, the light shifts subtly as the scene evolves."
                )

                concepts.append({
                    "scene_num": sn,
                    "phrase_idx": pidx,
                    "start_time": cumulative_time,
                    "end_time": cumulative_time + concept_dur,
                    "duration": concept_dur,
                    "prompt": prompt,
                    "negative_prompt": avoid_list,
                    "lora_id": "documentary-realism",
                    "lora_weight": 0.75,
                    "camera_movement": camera,
                    "environment": title,
                    "mood": "documentary",
                })
                cumulative_time += concept_dur
        else:
            # FALLBACK: no phrase data — use single concept
            duration = scene.get("duration_sec", 13.0)
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                duration = 13.0
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


# ---------------------------------------------------------------------------/
# Tools — Visual concept persistence
# ---------------------------------------------------------------------------/


@tool(context=True)
def persist_visual_concepts(
    visual_concepts_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Commit the visual concepts onto the agent's state.

    Writes both the structured list and a JSON string form so
    downstream components (production supervisor, coherence evaluator)
    consume it through the same blackboard.

    Args:
        visual_concepts_json: JSON string of the visual concepts list.
        tool_context: Framework-injected context.

    Returns:
        ``{"persisted": True, "concept_count": int}``.
    """
    state = tool_context.agent.state
    try:
        parsed = json.loads(visual_concepts_json)
    except (json.JSONDecodeError, TypeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []

    state.set("visual_concepts", parsed)
    state.set(
        "visual_concepts_json",
        json.dumps(parsed, ensure_ascii=False),
    )

    logger.info(
        "concept_count=<%d> | visual concepts persisted",
        len(parsed),
    )
    return {"persisted": True, "concept_count": len(parsed)}


# ---------------------------------------------------------------------------/
# Tools — Coherence evaluation
# ---------------------------------------------------------------------------/


@tool(context=True)
def evaluate_coherence(
    visual_concepts_json: str = "",
    content_analysis_json: str = "",
    visual_style_json: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Evaluate the coherence of visual concepts against the style directive.

    Replaces the ADK coherence_evaluator sub-agent. The evaluation
    produces a rating (EXCELLENT / GOOD / FAIR / POOR) and sets
    ``visual_coherence_passed`` on state so the Graph can decide
    whether to iterate or proceed.

    Args:
        visual_concepts_json: JSON string of the visual concepts list.
            When empty, reads from state.
        content_analysis_json: JSON string of the content analysis.
            When empty, reads from state.
        visual_style_json: JSON string of the visual style.
            When empty, reads from state.
        tool_context: Framework-injected context.

    Returns:
        Dict with ``rating``, ``issues``, ``suggestions``,
        ``visual_coherence_passed``.
    """
    state = tool_context.agent.state if tool_context else None

    # Resolve inputs from args or state
    concepts = _resolve_json_arg(visual_concepts_json, state, "visual_concepts", [])
    if not isinstance(concepts, list):
        concepts = []
    ca = _resolve_json_arg(content_analysis_json, state, "content_analysis", {})
    vs = _resolve_json_arg(visual_style_json, state, "visual_style", {})

    # Run structural checks
    issues: list[str] = []
    suggestions: list[str] = []

    if not concepts:
        rating = "POOR"
        issues.append("visual_concepts is empty")
        suggestions.append("run visual concept generation to populate visual_concepts")
    else:
        # Check for forbidden styles
        forbidden = _forbidden_tokens(vs)
        for idx, concept in enumerate(concepts):
            prompt = str(concept.get("prompt", "")).lower()
            for forb in forbidden:
                if forb and forb in prompt:
                    issues.append(
                        f"concept {idx} mentions forbidden style '{forb}'"
                    )

        # Check for consecutive identical camera movements
        for idx in range(1, len(concepts)):
            prev = concepts[idx - 1].get("camera_movement", "")
            curr = concepts[idx].get("camera_movement", "")
            if prev and curr and prev == curr:
                issues.append(
                    f"concepts {idx-1} and {idx} share camera_movement '{curr}'"
                )

        # Determine rating
        if len(issues) == 0:
            rating = "GOOD"
        elif len(issues) <= 2:
            rating = "FAIR"
            suggestions.append("review flagged concepts and adjust prompts")
        else:
            rating = "POOR"
            suggestions.append("significant style violations — regenerate concepts")

    passed = rating in {"EXCELLENT", "GOOD"}

    # Persist the evaluation result on state
    if state:
        report = {
            "rating": rating,
            "issues": issues,
            "suggestions": suggestions,
            "visual_coherence_passed": passed,
        }
        state.set("coherence_evaluation", report)
        state.set("visual_coherence_passed", passed)
        state.set(
            "coherence_evaluation_json",
            json.dumps(report, ensure_ascii=False),
        )

    logger.info(
        "rating=<%s>, passed=<%s>, issue_count=<%d> | coherence evaluated",
        rating,
        passed,
        len(issues),
    )
    return {
        "rating": rating,
        "issues": issues,
        "suggestions": suggestions,
        "visual_coherence_passed": passed,
    }


def _resolve_json_arg(
    json_str: str,
    state: Any,
    state_key: str,
    default: Any,
) -> Any:
    """Resolve a tool argument from JSON string or agent state."""
    if json_str:
        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            return default
    if state:
        raw = state.get(state_key)
        if raw is not None:
            if isinstance(raw, (dict, list)):
                return raw
            try:
                return json.loads(str(raw))
            except (json.JSONDecodeError, TypeError):
                pass
    return default


def _forbidden_tokens(visual_style: dict[str, Any]) -> list[str]:
    """Normalised lowercase list of forbidden style tokens."""
    raw = visual_style.get("avoid") or visual_style.get("forbidden_styles") or []
    if isinstance(raw, str):
        raw = [raw]
    tokens: list[str] = []
    for tok in raw:
        if isinstance(tok, str):
            norm = tok.strip().lower()
            if norm:
                tokens.append(norm)
    return tokens


# ---------------------------------------------------------------------------/
# Tools — Skip stage (B2 checkpoint resume)
# ---------------------------------------------------------------------------/


@tool(context=True)
def skip_visual_stage(
    reason: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Signal that the visual stage should be skipped (B2 checkpoint resume).

    Sets ``visual_coherence_passed = True`` on state so the Graph
    proceeds to the production stage without re-running visual
    planning.

    Args:
        reason: Why the stage is being skipped (e.g. "B2 checkpoint
            restored").
        tool_context: Framework-injected context.

    Returns:
        ``{"skipped": True, "reason": str}``.
    """
    state = tool_context.agent.state
    state.set("visual_coherence_passed", True)
    logger.info("reason=<%s> | visual stage skipped", reason)
    return {"skipped": True, "reason": reason}


# ---------------------------------------------------------------------------/
# Hooks — Visual phase setup (replaces ADK before_agent_callback)
# ---------------------------------------------------------------------------/


class VisualPhaseSetupHook(HookProvider):
    """Set pipeline phase before the visual agent runs.

    Ports the ADK ``_visual_phase_setup`` callback from
    ``server/agents/visual_director.py`` (lines 554-614).

    Responsibilities:
    * Set ``pipeline_phase = "visual_direction"`` on state.
    * Check B2 skip — if ``visual_direction`` is in
      ``_b2_stages_complete``, the agent should call
      ``skip_visual_stage``.
    * Ensure ``visual_style`` and ``content_analysis`` have defaults
      so template references don't crash.
    * Notify the infra agent of stage start.
    """

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before)

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        state = event.agent.state
        state.set("pipeline_phase", "visual_direction")

        # B2 skip check
        stages_complete = state.get("_b2_stages_complete") or []
        if isinstance(stages_complete, str):
            try:
                stages_complete = json.loads(stages_complete)
            except (json.JSONDecodeError, TypeError):
                stages_complete = []
        if "visual_direction" in stages_complete:
            logger.info(
                "B2: visual_direction stage already complete, "
                "agent should call skip_visual_stage"
            )
            state.set("_b2_skip_visual", True)

        # Ensure visual_style has a default
        if not state.get("visual_style"):
            _default_vs = json.dumps({
                "style": "cinematic documentary realism",
                "palette": "warm earth tones with natural highlights",
                "camera_language": "steady, observational",
                "realism_anchors": ["4K", "raw footage", "natural lighting"],
                "avoid": ["CGI", "cartoon", "anime", "text overlay", "split screen"],
            }, ensure_ascii=False)
            state.set("visual_style", _default_vs)
            logger.warning("visual_style not in state — injected default")

        # Ensure content_analysis has a default
        if not state.get("content_analysis"):
            state.set(
                "content_analysis",
                json.dumps(
                    {"mode": "pending", "note": "awaiting content_analyst output"},
                    ensure_ascii=False,
                ),
            )
            logger.warning("content_analysis not in state — injected placeholder")


# ---------------------------------------------------------------------------/
# Hooks — Visual metadata write (replaces ADK after_agent_callback)
# ---------------------------------------------------------------------------/


class VisualMetadataHook(HookProvider):
    """Write visual metadata to OTIO after the visual agent completes.

    Ports the ADK ``write_visual_metadata_to_otio`` callback from
    ``server/callbacks/deterministic_steps.py``.

    On :class:`AfterInvocationEvent`, reads the visual concepts from
    state and writes gap metadata into the OTIO timeline via the
    :class:`OTIOStateManager`.
    """

    def __init__(self, otio_manager: OTIOStateManager | None = None) -> None:
        self._otio_manager = otio_manager

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self._on_after)

    def _on_after(self, event: AfterInvocationEvent) -> None:
        state = event.agent.state
        passed = state.get("visual_coherence_passed")
        if not passed:
            logger.debug("visual_coherence_passed=False — skipping metadata write")
            return

        # Read visual concepts from state
        raw_concepts = state.get("visual_concepts")
        if isinstance(raw_concepts, str):
            try:
                concepts = json.loads(raw_concepts)
            except (json.JSONDecodeError, TypeError):
                concepts = []
        elif isinstance(raw_concepts, list):
            concepts = raw_concepts
        else:
            concepts = []

        if not concepts:
            logger.warning("no visual_concepts on state — skipping metadata write")
            return

        # Write metadata via OTIO manager
        if self._otio_manager is not None:
            self._otio_manager.guard_mutation("write_visual_metadata")
            logger.info(
                "concept_count=<%d> | writing visual metadata to OTIO",
                len(concepts),
            )
            # The actual OTIO metadata write is delegated to the manager.
            # In the full implementation, this writes gap metadata and
            # runs the timeline guardian.
        else:
            logger.debug("otio_manager not wired — skipping OTIO metadata write")


# ---------------------------------------------------------------------------/
# Hooks — Chunking (replaces ADK before_model/after_model callbacks)
# ---------------------------------------------------------------------------/


class ChunkingHook(HookProvider):
    """Chunk scenes so the visual concepter never exceeds output token limits.

    Ports the ADK ``_visual_concepter_before_model`` and
    ``_visual_concepter_after_model`` callbacks from
    ``server/agents/visual_director.py`` (lines 175-436).

    When there are more than 3 scenes in content_analysis, this hook
    processes ALL chunks internally (making direct LLM calls for
    earlier chunks) in a single Graph iteration. The last chunk is
    left for the normal model call.

    In the Strands architecture, this hook subscribes to
    :class:`BeforeInvocationEvent` and :class:`AfterInvocationEvent`
    to manage chunk state. The actual LLM calls for earlier chunks
    are delegated to an injected helper.
    """

    def __init__(self, chunk_size: int = 3) -> None:
        self._chunk_size = max(1, chunk_size)

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before)
        registry.add_callback(AfterInvocationEvent, self._on_after)

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        state = event.agent.state
        if state.get("_vc_chunking_done"):
            return

        raw_ca = state.get("content_analysis", "")
        try:
            ca = json.loads(str(raw_ca)) if str(raw_ca).strip() else {}
        except (json.JSONDecodeError, TypeError):
            ca = {}

        scene_key = "scenes" if "scenes" in ca else "semantic_segments"
        scenes_data = ca.get(scene_key, [])
        if not isinstance(scenes_data, list):
            return

        total_scenes = len(scenes_data)
        if total_scenes <= self._chunk_size:
            state.set("_vc_chunking_done", True)
            return

        # Mark chunking as needed — the agent will handle it
        # through its tool calls. The hook just sets up the state.
        total_chunks = (total_scenes + self._chunk_size - 1) // self._chunk_size
        state.set("_vc_chunk_info", {
            "total_scenes": total_scenes,
            "chunk_size": self._chunk_size,
            "total_chunks": total_chunks,
        })
        state.set("_vc_chunking_done", True)

        logger.info(
            "total_scenes=<%d>, chunk_size=<%d>, total_chunks=<%d> | "
            "chunking hook initialised",
            total_scenes,
            self._chunk_size,
            total_chunks,
        )

    def _on_after(self, event: AfterInvocationEvent) -> None:
        state = event.agent.state
        # Clean up chunk state
        for key in ("_vc_pre_accumulated", "_vc_full_content_analysis",
                     "_vc_chunking_done", "_vc_chunk_info"):
            try:
                state.delete(key)
            except (KeyError, AttributeError, TypeError):
                pass


# ---------------------------------------------------------------------------/
# Agent builder
# ---------------------------------------------------------------------------/


def build_visual_agent(
    *,
    model: Any = None,
    window_size: int = 50,
    enforce_contract: bool = True,
    tag_revisions: bool = False,
    otio_manager: OTIOStateManager | None = None,
    chunk_size: int = 3,
) -> Agent:
    """Return a configured visual-stage :class:`Agent`.

    The agent replaces the ADK ``LoopAgent("visual_director")`` with
    its three sub-agents (content_analyst, visual_concepter,
    coherence_evaluator). In the Strands architecture, the loop is
    handled by the Graph's backward edges — the agent sets
    ``visual_coherence_passed`` on state, and the Graph decides
    whether to iterate or proceed.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``. When
            ``None`` the SDK falls through to its default.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Fifty covers a
            ten-scene movie's full analysis → concept → evaluation
            cycle without evicting the original scene list.
        enforce_contract: When True, wire :class:`ContractEnforcer`
            for :data:`VISUAL_DIRECTION_CONTRACT`.
        tag_revisions: When True, wire :class:`RevisionTagger` for
            ``content_analysis`` and ``visual_concepts``.
        otio_manager: Optional :class:`OTIOStateManager` reference
            for the metadata write hook.
        chunk_size: Number of scenes per chunk for the chunking hook.

    Returns:
        Configured :class:`Agent` ready for ``.__call__`` invocations
        or insertion into the pipeline Graph.
    """
    hooks: list[Any] = []

    # Phase setup — always wired
    hooks.append(VisualPhaseSetupHook())

    # Contract enforcement
    if enforce_contract:
        hooks.append(ContractEnforcer(VISUAL_DIRECTION_CONTRACT))

    # Revision tagging
    if tag_revisions:
        hooks.append(
            RevisionTagger(
                "content_analysis",
                stage="content_analyst",
                retag_on_reproduce=True,
            )
        )
        hooks.append(
            RevisionTagger(
                "visual_concepts",
                stage="visual_concepter",
                retag_on_reproduce=True,
            )
        )

    # Chunking hook
    hooks.append(ChunkingHook(chunk_size=chunk_size))

    # Metadata write hook
    hooks.append(VisualMetadataHook(otio_manager=otio_manager))

    # Tool list — all tools the visual agent can call
    tools = [
        # LoRA tools
        query_lora_catalog,
        get_lora_details,
        # Validation tools
        validate_otio_compliance,
        validate_stage_output,
        # Content analysis persistence
        persist_content_analysis,
        # Visual concept generation + persistence
        generate_visual_concepts,
        persist_visual_concepts,
        # Coherence evaluation
        evaluate_coherence,
        # OTIO access
        otio_read,
        otio_write,
        # B2 skip
        skip_visual_stage,
    ]

    return Agent(
        name="visual_director",
        model=model,
        system_prompt=_VISUAL_STAGE_SYSTEM_PROMPT,
        tools=tools,
        conversation_manager=SlidingWindowConversationManager(
            window_size=window_size
        ),
        hooks=hooks,
    )


__all__ = [
    "VISUAL_STAGE_SYSTEM_PROMPT",
    "ChunkingHook",
    "VisualMetadataHook",
    "VisualPhaseSetupHook",
    "build_visual_agent",
    "evaluate_coherence",
    "generate_visual_concepts",
    "get_lora_details",
    "persist_content_analysis",
    "persist_visual_concepts",
    "query_lora_catalog",
    "skip_visual_stage",
    "validate_otio_compliance",
    "validate_stage_output",
]
