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
from tools.validation_tools import validate_otio_compliance_tool, validate_stage_output_tool

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

async def _visual_concepter_before_model(callback_context, llm_request):
    """Chunk scenes so visual_concepter never exceeds output token limits.

    If there are more than 3 scenes in content_analysis, this callback
    processes ALL chunks internally (making direct LLM calls for earlier
    chunks) in a single LoopAgent iteration. This decouples chunking from
    the evaluation loop so LoopAgent iterations are used only for
    evaluator refinement, not chunk advancement.

    The final chunk is left for the normal model call to handle, so the
    LoopAgent's output_key mechanism works correctly.
    """
    import json as _json
    import litellm

    state = callback_context.state

    # If chunking already completed in a prior call, just pass through
    if state.get("_vc_chunking_done"):
        return await before_model_callback(callback_context, llm_request)

    raw_ca = state.get("content_analysis", "")
    try:
        ca = _json.loads(raw_ca) if raw_ca.strip() else {}
    except (_json.JSONDecodeError, TypeError):
        ca = {}

    # Determine scenes in content_analysis
    scene_key = "scenes" if "scenes" in ca else "semantic_segments"
    scenes_data = ca.get(scene_key, [])
    if not isinstance(scenes_data, list):
        return await before_model_callback(callback_context, llm_request)

    chunk_size = 3
    total_scenes = len(scenes_data)

    if total_scenes <= chunk_size:
        # Small enough — no chunking needed
        state["_vc_chunking_done"] = True
        return await before_model_callback(callback_context, llm_request)

    # Process ALL chunks except the last one internally via direct LLM calls.
    # The last chunk is left for the normal model call.
    total_chunks = (total_scenes + chunk_size - 1) // chunk_size
    accumulated = []

    from agents.model_config import build_model, ADK_MODEL_NAME
    _model_obj = build_model()
    # build_model() returns Union[str, LiteLlm]. For direct litellm calls
    # we need the string model name, not the ADK wrapper.
    model_name = _model_obj if isinstance(_model_obj, str) else ADK_MODEL_NAME

    for chunk_idx in range(total_chunks - 1):  # all except last
        start = chunk_idx * chunk_size
        end = min(start + chunk_size, total_scenes)

        chunked_ca = dict(ca)
        chunked_ca[scene_key] = scenes_data[start:end]
        chunked_ca["_chunk_info"] = {
            "chunk_idx": chunk_idx,
            "scenes_in_chunk": list(range(start, end)),
            "total_scenes": total_scenes,
            "is_partial": True,
        }

        logger.info(
            "Visual concepter: processing chunk %d/%d (scenes %d-%d of %d) via direct LLM call",
            chunk_idx + 1, total_chunks, start + 1, end, total_scenes,
        )

        # Build a minimal prompt for this chunk
        chunk_prompt = (
            f"Generate visual concepts for the following scenes. "
            f"This is chunk {chunk_idx + 1} of {total_chunks}.\n\n"
            f"Content analysis (partial):\n{_json.dumps(chunked_ca, ensure_ascii=False)}\n\n"
            f"Visual style: {state.get('visual_style', '')}\n\n"
            f"Output a JSON array of visual concept objects."
        )

        try:
            # Ensure Gemini models use the gemini/ prefix for Google AI Studio
            import os as _os
            _model = model_name
            if (
                isinstance(_model, str)
                and _model.startswith("gemini-")
                and not _model.startswith("gemini/")
                and _os.environ.get("GOOGLE_API_KEY")
            ):
                _model = f"gemini/{_model}"

            resp = litellm.completion(
                model=_model,
                messages=[
                    {"role": "system", "content": _VISUAL_CONCEPTER_INSTRUCTION},
                    {"role": "user", "content": chunk_prompt},
                ],
                temperature=0.7,
            )
            raw_text = resp.choices[0].message.content or ""

            # Parse concepts from response
            text = raw_text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)

            parsed = _json.loads(text)
            if isinstance(parsed, list):
                accumulated.extend(parsed)
            elif isinstance(parsed, dict) and "concepts" in parsed:
                accumulated.extend(parsed["concepts"])

            logger.info(
                "Visual concepter: chunk %d/%d yielded %d concepts",
                chunk_idx + 1, total_chunks,
                len(parsed) if isinstance(parsed, list) else len(parsed.get("concepts", [])),
            )
        except Exception as exc:
            logger.warning(
                "Visual concepter: chunk %d/%d LLM call failed: %s",
                chunk_idx + 1, total_chunks, exc,
            )

    # Store accumulated concepts from earlier chunks
    state["_vc_pre_accumulated"] = _json.dumps(accumulated, ensure_ascii=False)
    state["_vc_chunking_done"] = True

    # Set content_analysis to only the LAST chunk for the normal model call
    last_start = (total_chunks - 1) * chunk_size
    last_end = total_scenes
    last_ca = dict(ca)
    last_ca[scene_key] = scenes_data[last_start:last_end]
    last_ca["_chunk_info"] = {
        "chunk_idx": total_chunks - 1,
        "scenes_in_chunk": list(range(last_start, last_end)),
        "total_scenes": total_scenes,
        "is_partial": True,
        "previously_accumulated": len(accumulated),
    }
    state["content_analysis"] = _json.dumps(last_ca, ensure_ascii=False)
    # Save full content_analysis for restoration after generation
    state["_vc_full_content_analysis"] = raw_ca

    logger.info(
        "Visual concepter: %d earlier chunks done (%d concepts), "
        "last chunk (scenes %d-%d) left for normal model call",
        total_chunks - 1, len(accumulated), last_start + 1, last_end,
    )

    return await before_model_callback(callback_context, llm_request)


def _visual_concepter_after_model(callback_context, llm_response):
    """Merge pre-accumulated concepts (from earlier chunks) with the last chunk.

    The before_model_callback processes all chunks except the last one
    internally via direct LLM calls. This after_model callback merges
    those pre-accumulated concepts with the last chunk's output (from
    the normal model call) and modifies llm_response so output_key
    stores the complete result.
    """
    import json as _json

    # Run the original after_model_callback first
    result = after_model_callback(callback_context, llm_response)

    state = callback_context.state

    # If no pre-accumulated concepts exist, nothing to merge
    raw_pre = state.get("_vc_pre_accumulated")
    if not raw_pre:
        return result

    try:
        pre_accumulated = _json.loads(raw_pre)
    except (_json.JSONDecodeError, TypeError):
        pre_accumulated = []

    if not pre_accumulated:
        # Even though there's nothing to merge, we must still restore
        # the full content_analysis (which was overwritten with only the
        # last chunk by the before_model callback) and clean up state.
        full_ca = state.get("_vc_full_content_analysis")
        if full_ca:
            state["content_analysis"] = full_ca
        for key in ("_vc_pre_accumulated", "_vc_full_content_analysis", "_vc_chunking_done"):
            try:
                del state[key]
            except (KeyError, TypeError):
                pass
        return result

    # Extract last chunk's concepts from llm_response
    raw_llm_text = ""
    try:
        if hasattr(llm_response, "content") and llm_response.content:
            if hasattr(llm_response.content, "parts"):
                raw_llm_text = "".join(
                    getattr(p, "text", "") for p in llm_response.content.parts
                )
            elif isinstance(llm_response.content, str):
                raw_llm_text = llm_response.content
    except Exception:
        raw_llm_text = ""

    last_chunk_concepts = []
    if raw_llm_text.strip():
        text = raw_llm_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            parsed = _json.loads(text)
            if isinstance(parsed, list):
                last_chunk_concepts = parsed
            elif isinstance(parsed, dict) and "concepts" in parsed:
                last_chunk_concepts = parsed["concepts"]
        except (_json.JSONDecodeError, TypeError):
            pass

    # Merge: pre-accumulated (earlier chunks) + last chunk
    all_concepts = pre_accumulated + last_chunk_concepts
    all_concepts_json = _json.dumps(all_concepts, ensure_ascii=False)

    # Modify llm_response so output_key stores the complete merged result
    try:
        if hasattr(llm_response, "content") and hasattr(llm_response.content, "parts"):
            for part in llm_response.content.parts:
                if hasattr(part, "text"):
                    part.text = all_concepts_json
                    break
    except Exception:
        pass

    # Also set state directly as a safety net
    state["visual_concepts"] = all_concepts_json

    # Restore full content_analysis for the evaluator
    full_ca = state.get("_vc_full_content_analysis")
    if full_ca:
        state["content_analysis"] = full_ca

    logger.info(
        "Visual concepter: merged %d pre-accumulated + %d last-chunk = %d total concepts",
        len(pre_accumulated), len(last_chunk_concepts), len(all_concepts),
    )

    # Clean up chunk state
    for key in ("_vc_pre_accumulated", "_vc_full_content_analysis", "_vc_chunking_done"):
        try:
            del state[key]
        except (KeyError, TypeError):
            pass

    return result


visual_concepter = Agent(
    name="visual_concepter",
    model=build_model(),
    instruction=_VISUAL_CONCEPTER_INSTRUCTION,
    output_key="visual_concepts",
    before_model_callback=_visual_concepter_before_model,
    after_model_callback=_visual_concepter_after_model,
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
    """After evaluator: log when visuals are approved and mirror QA verdict."""
    state = callback_context.state
    eval_output = state.get("coherence_evaluation", "")
    eval_text = str(eval_output or "")
    if "APPROVED: true" in eval_text:
        logger.info("Visual concepts approved by coherence evaluator")

    # Mirror the coherence evaluator rating to the critique store so the
    # pull-based escalation supervisor can read it.  Fail-closed: if
    # parsing / store access fails, log and continue — this must never
    # break the live pipeline.
    try:
        from critique.qa_storage import mirror_coherence_evaluator_result

        rating = _parse_coherence_rating(eval_text)
        if rating:
            artifact_id = (
                state.get("run_id")
                or state.get("session_id")
                or state.get("documentary_id")
                or "current_run"
            )
            mirror_coherence_evaluator_result(
                rating,
                artifact_type="visual_concept",
                artifact_id=str(artifact_id),
                rationale=eval_text[:2000],
                produced_by="coherence_evaluator",
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("coherence evaluator: critique mirror skipped: %s", exc)
    return None


def _parse_coherence_rating(text: str) -> str:
    """Extract an EXCELLENT/GOOD/FAIR/POOR rating from evaluator output.

    The coherence evaluator emits free-form text; ``RATING: GOOD`` is the
    documented convention but agents sometimes drop the prefix.  We look
    for the token anywhere in the output and return the first match, or
    an empty string if none is found (in which case the store is not
    written to).
    """

    if not text:
        return ""
    upper = text.upper()
    for candidate in ("EXCELLENT", "GOOD", "FAIR", "POOR"):
        if candidate in upper:
            return candidate
    return ""


def _evaluator_before_tool(callback_context, tool_name, tool_input):
    """Block exit_loop if chunking is still in progress.

    With the new internal-chunking approach, this should rarely fire
    since all chunks are processed in a single before_model_callback.
    But kept as a safety net in case state flags are stale.
    """
    if tool_name == "exit_loop" and "_vc_pre_accumulated" in callback_context.state:
        logger.info(
            "Coherence evaluator tried to exit_loop but chunking state "
            "is still present — blocking exit"
        )
        return "Cannot exit yet — visual concepts are still being finalized. " \
               "Please rate as FAIR and wait for the next iteration."
    # Delegate to standard before_tool_callback for all other cases
    return before_tool_callback(callback_context, tool_name, tool_input)


coherence_evaluator = Agent(
    name="coherence_evaluator",
    model=build_model(vision=True),
    instruction=_COHERENCE_EVALUATOR_INSTRUCTION,
    tools=[exit_loop, validate_otio_compliance_tool, validate_stage_output_tool],
    output_key="coherence_evaluation",
    after_agent_callback=_check_coherence_approval,
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
    before_tool_callback=_evaluator_before_tool,
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

    # B2 skip check FIRST — avoid blocking on gatekeeper/infra for a completed stage
    stages_complete = state.get("_b2_stages_complete", [])
    if "visual_direction" in stages_complete:
        logger.info("B2: visual_direction stage already complete, skipping LoopAgent")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Visual direction restored from B2 checkpoint \u2014 skipped.")],
        )

    # GATEKEEPER: stage handoff check (audio → visual_direction)
    # Runs AFTER B2 skip so checkpoint resumes don't trigger unnecessary
    # validation + intervention windows.
    from gatekeeper import check_stage_handoff, has_rejects, intervention_window
    from callbacks.state_manager import safe_state_dict
    handoff_checks = check_stage_handoff("audio", "visual_direction", safe_state_dict(state))
    if has_rejects(handoff_checks):
        rejects = [c for c in handoff_checks if c.verdict.value == "reject"]
        raise RuntimeError(
            "GATEKEEPER BLOCKED visual_direction start: "
            + "; ".join(c.message for c in rejects)
        )
    if not intervention_window("visual_direction_start", handoff_checks):
        raise RuntimeError("GATEKEEPER: user halted pipeline at visual_direction start")

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

            # CRITICAL: When before_agent_callback returns Content, ADK
            # skips the after_agent_callback entirely.  We must explicitly
            # run the post-processing that _visual_after_with_gate would
            # have done: write_visual_metadata_to_otio (B2 upload, infra
            # notification, timeline guardian) + mark_stage_ready("prompts")
            # to unblock the production stage's approval gate.
            from callbacks.deterministic_steps import write_visual_metadata_to_otio
            try:
                write_visual_metadata_to_otio(callback_context)
                logger.info("QUICK-TEST: write_visual_metadata_to_otio completed")
            except RuntimeError:
                raise  # OTIO violations are fatal — never swallow
            except Exception as e:
                logger.warning("QUICK-TEST: write_visual_metadata_to_otio error: %s", e)

            from callbacks.approval_gate import mark_stage_ready, approve_stage
            mark_stage_ready("prompts")
            approve_stage("prompts")
            logger.info("QUICK-TEST: marked prompts stage ready + approved")

            return genai_types.Content(
                role="model",
                parts=[genai_types.Part(
                    text=f"Visual direction complete (quick-test): {len(concepts)} concepts generated deterministically."
                )],
            )
        else:
            logger.warning("QUICK-TEST: no scenes found, falling through to LLM-based visual planning")

    # Ensure visual_style has a default so LLM agent templates don't crash
    # with KeyError when {visual_style} is referenced but not set.
    if not state.get("visual_style"):
        _default_vs = json.dumps({
            "style": "cinematic documentary realism",
            "palette": "warm earth tones with natural highlights",
            "camera_language": "steady, observational",
            "realism_anchors": ["4K", "raw footage", "natural lighting"],
            "avoid": ["CGI", "cartoon", "anime", "text overlay", "split screen"],
        }, ensure_ascii=False)
        state["visual_style"] = _default_vs
        logger.warning("visual_style not in state — injected default: %s", _default_vs)

    # Also ensure content_analysis has a default (referenced by coherence_evaluator)
    if not state.get("content_analysis"):
        state["content_analysis"] = json.dumps({"mode": "pending", "note": "awaiting content_analyst output"})
        logger.warning("content_analysis not in state — injected placeholder")

    # INFRA: notify stage start for timing watchdog (only if stage will actually run)
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("visual_direction")
    check_infra_pause()

    return None


def _generate_quick_test_concepts(state) -> list:
    """Generate visual concepts from scenes data without LLM calls.

    ARCHITECTURE RULE: One video concept per narration phrase per scene.
    The concept's duration MUST match the corresponding narration clip's
    source_range from the OTIO timeline.  This is the fundamental contract
    that ensures video and audio stay in sync during assembly.

    The OTIO timeline (populated by the audio stage) is the AUTHORITATIVE
    source for durations.  We never use the scenario's ``duration_sec``
    estimate for video sizing — it doesn't account for actual TTS output.

    Quick-test mode: simple cinematography prompts, default LoRA, no deep
    semantic analysis, no LoRA catalog queries, no coherence evaluation.
    """
    import json
    from callbacks.deterministic_steps import extract_json_array
    from tools.otio_tools import get_narration_durations_by_scene

    raw_scenes = state.get("scenes", "[]")
    scenes = extract_json_array(str(raw_scenes))
    if not scenes:
        return []

    # Read actual narration durations from OTIO timeline (authoritative)
    from callbacks.deterministic_steps import _MockToolContext
    narr_durations = get_narration_durations_by_scene(
        tool_context=_MockToolContext(state),
    )
    if not narr_durations:
        logger.warning(
            "QUICK-TEST: no narration durations found in OTIO — "
            "audio stage may not have run. Falling back to scenario estimates."
        )

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

    # Camera movements to vary between phrases
    _CAMERA_MOVES = [
        "slow dolly forward",
        "gentle pan right",
        "slow crane up",
        "handheld with controlled micro-shake",
        "slow orbit around the subject",
        "truck left at eye level",
    ]

    concepts = []
    for scene in scenes:
        sn = scene.get("scene_num", 0)
        title = scene.get("title", "scene")
        visual_notes = scene.get("visual_notes", "")

        # Build a simple cinematography prompt from visual_notes
        if visual_notes:
            base_desc = visual_notes
        else:
            base_desc = f"Documentary footage related to {title}"

        # Get narration phrases for this scene from OTIO
        scene_phrases = narr_durations.get(sn, [])

        if scene_phrases:
            # OTIO-DRIVEN: one concept per narration phrase
            cumulative_time = 0.0
            for pidx, (voice, phrase_dur) in enumerate(scene_phrases):
                camera = _CAMERA_MOVES[pidx % len(_CAMERA_MOVES)]
                prompt = (
                    f"{base_desc} "
                    f"The camera performs a {camera}, capturing the scene "
                    f"with shallow depth of field. "
                    f"Lighting is soft and natural with warm highlights. "
                    f"Shot in {realism_anchors}. "
                    f"Over time, the light shifts subtly as the scene evolves."
                )

                # Cap individual concept duration at 10s (LTX-2.3 limit)
                concept_dur = min(phrase_dur, 10.0)

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

            logger.info(
                "Scene %d: %d concepts from OTIO narration (%s)",
                sn, len(scene_phrases),
                ", ".join(f"{v}={d:.1f}s" for v, d in scene_phrases),
            )
        else:
            # FALLBACK: no OTIO data — use scenario estimate (single concept)
            # This path should only trigger if audio stage didn't run.
            duration = scene.get("duration_sec", 13.0)
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
            logger.warning(
                "Scene %d: no OTIO narration data, using single concept "
                "with scenario estimate (%.1fs)",
                sn, min(duration, 10.0),
            )

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
    max_iterations=5,
    sub_agents=[content_analyst, visual_concepter, coherence_evaluator],
    before_agent_callback=_visual_phase_setup,
    # write_visual_metadata_to_otio writes gap metadata then runs timeline guardian
    after_agent_callback=write_visual_metadata_to_otio,
)
