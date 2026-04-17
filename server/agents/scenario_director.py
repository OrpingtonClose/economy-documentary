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
_QUICK_TEST_RULES = """

QUICK TEST MODE IS ACTIVE.
You MUST follow these STRICT constraints for a ~1-minute test movie:
- Generate EXACTLY 2 scenes (no more, no fewer)
- Each scene MUST be 10-15 seconds when spoken (~25-37 words per scene total across all voices)
- Each voice block should be 1-2 SHORT sentences only
- Total movie duration MUST be under 60 seconds
- Keep visual_notes very brief (one sentence)
- Keep dopamine_hook very brief (one phrase)
- The 2 scenes should still form a mini narrative arc: hook → payoff
"""

_GENERATOR_INSTRUCTION = """\
You are the Scenario Director for an ADHD-friendly documentary pipeline.

Read the research corpus from {corpus_path} and the topic "{topic}".

You must output THREE things — the scenario creator IS the intelligence layer.
Downstream stages (audio, visual, production) will NOT second-guess your
decisions; they will enforce them.

1. A MOVIE-LEVEL VISUAL STYLE directive in state["visual_style"] — a JSON object:
   {{
     "style": "<overall aesthetic, e.g. photorealistic documentary, stylized animation, painterly, etc.>",
     "realism_anchors": ["<terms that anchor the look, e.g. 4K, raw footage, no CGI, live action>"],
     "avoid": ["<things to avoid, e.g. cartoon, anime, CGI, morphing, illustration>"],
     "palette": "<colour/lighting direction, e.g. warm natural tones, golden hour>",
     "camera_language": "<default camera feel, e.g. stabilized handheld, tripod-locked, slow dolly>",
     "reference_genre": "<genre tag for LTX-2.3, e.g. Documentary, Period drama, Cinematic>"
   }}

2. A STYLE LOCK in state["style_lock"] — the ONE style family for the WHOLE
   documentary. Pick exactly ONE before writing any scenes. Every visual prompt
   gets this locked style applied. This prevents visual whiplash (e.g. mixing
   anime + watercolor + cyberpunk in the same documentary). JSON object:
   {{
     "dominant_style": "<one of: cinematic_documentary | hand_drawn_animation | realistic_3d | stylized_2d_animation | live_action_interview | archival_footage | mixed_media_collage | painterly>",
     "forbidden_styles": ["anime", "cartoon", "watercolor", "cyberpunk", "<etc — style keywords that must NOT appear>"],
     "positive_fragment": "<prompt fragment injected into EVERY visual prompt, e.g. 'cinematic documentary, photoreal, natural lighting, 4K, shallow depth of field'>",
     "negative_fragment": "<negative prompt for diffusion, e.g. 'anime, cartoon, morphing, distorted anatomy, text overlay, watermark'>"
   }}

3. A SCENARIO document as a JSON array of scenes in state["scenes"].

CRITICAL — TOTAL DURATION TARGET:
The user's message specifies how long the documentary should be (e.g. "7
minute documentary").  You MUST generate enough scenes so that the SUM of
all duration_sec values equals the requested total.  A structural check
runs BEFORE the LLM evaluator; it caps the verdict at POOR if:
  * sum(duration_sec) < 95% of target, OR
  * number_of_scenes < ceil(target_seconds / 45), OR
  * sum of narration words < target_seconds / 60 * 150 (wpm).
Plan for ceil(target_seconds / 35) scenes. Each scene 30-45s. Do NOT
undershoot the target — the evaluator is no longer lenient on duration.

Each scene MUST have:
- scene_num: integer (1-based)
- title: short descriptive title
- duration_sec: target duration (30-45 seconds per scene, MAX {max_scene_duration}s)
- voices: array of exactly 3 voice blocks:
  - V1: "The Hook" — provocative opener, challenges assumptions
  - V2: "The Expert" — provides data, evidence, nuance
  - V3: "The Storyteller" — human angle, emotional connection
  Each voice block has: voice (V1/V2/V3), text (the narration), tone (descriptor)
- visual_notes: brief notes on visual approach for this scene
  MUST align with style_lock.dominant_style. NEVER reference a style from
  style_lock.forbidden_styles (e.g. do not write "anime-style" if you
  locked cinematic_documentary). A structural check flags violations.
- dopamine_hook: what makes this scene grab attention in first 3 seconds
- pronunciation_hints: {{"TOKEN": "letter-by-letter spelling", ...}}
  Include EVERY initialism, capitalized abbreviation, or likely-mispronounced
  term that appears in ANY voice block's text. Examples:
    {{"PAG": "P-A-G", "DBS": "D-B-S", "fMRI": "f-M-R-I"}}
  A structural check scans narration for all-caps tokens and caps the
  verdict at POOR for any token missing here.  Common English words like
  "OK" / "THE" are whitelisted; everything else must be declared.
- ssml: optional pre-rendered SSML string for the TTS engine, or null.
  When provided, the TTS adapter prefers this over the plain text.
  Example: "<speak>The <say-as interpret-as='characters'>PAG</say-as> projects widely.</speak>"

SCENE 0 (the opening) additionally MUST carry:
- hook_spec: {{
    "topic_specific_motif": "<concrete noun phrase tied to the ACTUAL topic — NOT a generic 'blurry 3D brain'>",
    "motion_description": "<what the camera / subject does in the first 2-3 seconds>",
    "narrative_pull": "<why the viewer stays watching past 7 seconds>"
  }}
  The motif must be at least 2 words and reference something concrete about
  the documentary's subject.  A generic "a brain" fails.  "A single
  periaqueductal gray neuron on a microscope slide" passes.

THE FINAL SCENE additionally MUST carry:
- outro_spec: {{
    "closing_shot": "<concrete visual, e.g. 'wide shot of empty operating theatre, lights dim'>",
    "recap_sentence": "<one-sentence documentary summary>",
    "cta": "<call to action, e.g. 'subscribe for the next episode on deep brain stimulation'>",
    "brand_card": "<brand text overlay, e.g. documentary title + channel>"
  }}
  Documentaries that end on a fade with no outro fail this check.

LANGUAGE MODE: "{language}"
- If "en": write all voice text in English only.
- If "ru": write all voice text in Russian only.
- If "dual_ru_en": write EACH voice block with BOTH languages using this format:
    "text": "[RU] <Russian narration>\n[EN] <English translation>"
  The Russian text is the PRIMARY narration; the English is a faithful translation.
{quick_test_rules}
RULES:
1. Each scene MUST be <= {max_scene_duration} seconds when spoken at natural pace (~150 words/min)
2. Each scene MUST use all 3 voices (V1, V2, V3)
3. NO rhetorical questions — make declarative statements. Forbidden openers
   include "What happens when...?", "Can we harness...?", "What if...?",
   "How do we...?", "Imagine...", "Consider...". A regex + LLM check caps
   the verdict at POOR for any rhetorical question found in narration.
4. Each scene MUST specify different visual approaches for variety — BUT
   all within the locked style_lock.dominant_style.
5. Open with a dopamine hook — surprising fact, counterintuitive claim, or vivid image
6. Build narrative arc across scenes: hook → tension → insight → resolution
7. The visual_style and style_lock MUST be consistent and appropriate for the topic
8. STAY ON THE USER'S TOPIC. The user prompt is the north star. A semantic
   topic-fidelity check classifies every scene on/tangential/off-topic; more
   than 1 off-topic scene OR > 30% tangential caps the verdict at POOR.

After generating visual_style, style_lock, and scenes, create the OTIO timeline by calling
create_timeline(topic, num_scenes) where num_scenes = len(scenes).
This MUST be done before the Audio Agent runs.

Output the scene array as valid JSON in state["scenes"] and the visual style
object in state["visual_style"].
"""

def _save_generator_scenes(callback_context):
    """After generator: save scenes backup unconditionally.

    The generator's output_key writes to state['scenes'] before this
    callback fires.  We parse and save the scenes immediately so that
    clean_scenes_after_scenario has a fallback even if:
    - the evaluator's output_key overwrites state['scenes'],
    - the LoopAgent re-runs the generator with empty/malformed output,
    - the evaluator calls exit_loop() and ADK skips output_key.

    STREAMING FIX: If state['scenes'] is empty/unparseable, fall back to
    the accumulated generator text (_generator_accumulated_text) which
    collects all streaming chunks across after_model calls.
    """
    state = callback_context.state
    raw = state.get("scenes", "")
    scenes = extract_json_array(str(raw)) if raw else None

    # Fallback: try accumulated generator text (streaming chunks joined)
    if not scenes:
        accumulated = state.get("_generator_accumulated_text", "")
        if accumulated:
            from callbacks.after_model import _extract_scenes_array
            scenes = _extract_scenes_array(str(accumulated))
            if scenes:
                # Also persist to state and disk so downstream code finds them
                scenes_json = json.dumps(scenes, ensure_ascii=False)
                state["scenes"] = scenes_json
                import os
                timeline_dir = os.environ.get(
                    "TIMELINE_DIR", "/tmp/documentary-pipeline/timelines"
                )
                backup_path = os.path.join(timeline_dir, "_scenes_backup.json")
                os.makedirs(os.path.dirname(backup_path) or ".", exist_ok=True)
                with open(backup_path, "w") as f:
                    f.write(scenes_json)
                logger.info(
                    "Recovered %d scenes from accumulated generator text → state + %s",
                    len(scenes), backup_path,
                )

    if scenes:
        # Only save if the evaluator hasn't already saved an approved backup.
        # Otherwise, the generator's unapproved output would overwrite the
        # evaluator-approved scenes when the LoopAgent re-runs.
        if not state.get("_approved_scenes_backup"):
            state["_approved_scenes_backup"] = json.dumps(scenes)
            logger.info(
                "Saved generator scenes backup: %d scenes", len(scenes)
            )
        else:
            logger.info(
                "Skipping generator backup: evaluator-approved backup already exists"
            )
    else:
        logger.warning(
            "No scenes found in state or accumulated text after generator completed"
        )

    # Reset accumulated text for next loop iteration
    state["_generator_accumulated_text"] = ""
    return None


def _build_generator_instruction() -> str:
    """Build the generator instruction, injecting quick-test rules if active.

    We use {quick_test_rules} and {max_scene_duration} as template vars
    that will be resolved by ADK from session state at runtime.
    """
    # These are template placeholders — ADK resolves them from state at runtime.
    # The actual values are set in run_pipeline.py when --quick-test is passed.
    return _GENERATOR_INSTRUCTION


scenario_generator = Agent(
    name="scenario_generator",
    model=build_model(),
    instruction=_build_generator_instruction(),
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
    after_agent_callback=_save_generator_scenes,
)

# -- Callbacks (defined before agents that reference them) ---------------------


def _run_structural_checks_before_evaluator(callback_context):
    """Run deterministic structural checks and write the report into state.

    This fires before the LLM evaluator runs.  We parse the current scenes
    + style_lock + user topic from state, run ``run_all_structural_checks``
    and stash a human-readable report into state["structural_report"] so
    the LLM evaluator can read it via the {structural_report} template var.

    Parsing failures (malformed JSON, missing fields) are not fatal — we
    emit a minimal report indicating "could not parse" and let the LLM
    evaluator make the call.  The evaluator is instructed to rate POOR
    when the structural ceiling is POOR.
    """
    from tools.scenario_evaluator_checks import (
        format_report,
        run_all_structural_checks,
    )

    state = callback_context.state

    raw_scenes = state.get("scenes", "")
    scenes = extract_json_array(str(raw_scenes)) if raw_scenes else None
    if not scenes:
        state["structural_report"] = (
            "OVERALL_CAP: POOR\n\n"
            "[FAIL (cap=POOR)] scenes_parse: could not parse scenes JSON from state"
        )
        return None

    # style_lock may live either directly in state (preferred) or inside the
    # first scene of the generator output as a convenience — support both.
    style_lock_raw = state.get("style_lock")
    if isinstance(style_lock_raw, str) and style_lock_raw.strip():
        try:
            style_lock_raw = json.loads(style_lock_raw)
        except json.JSONDecodeError:
            style_lock_raw = None
    if not style_lock_raw and isinstance(scenes, list) and scenes:
        style_lock_raw = scenes[0].get("_style_lock") or scenes[0].get("style_lock")

    user_prompt = str(state.get("topic", "") or "")

    # Target duration: we try a few state keys used elsewhere in the
    # pipeline.  A non-numeric value yields 0 which skips duration checks.
    target_duration_sec = 0.0
    for key in ("target_duration_sec", "target_duration", "duration_target_sec"):
        val = state.get(key)
        if val:
            try:
                target_duration_sec = float(val)
                break
            except (TypeError, ValueError):
                continue

    scenario = {"scenes": scenes, "style_lock": style_lock_raw or {}}
    report = run_all_structural_checks(
        scenario,
        user_prompt=user_prompt,
        target_duration_sec=target_duration_sec,
    )
    formatted = format_report(report)
    state["structural_report"] = formatted
    state["_structural_report_overall_cap"] = report.overall
    logger.info(
        "Scenario structural checks: overall_cap=%s, %d failures",
        report.overall, len(report.failed()),
    )
    return None


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

    # B2 skip check FIRST — avoid blocking on infra pause for a completed stage
    stages_complete = state.get("_b2_stages_complete", [])
    if "scenario" in stages_complete:
        logger.info("B2: scenario stage already complete, skipping LoopAgent")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Scenario restored from B2 checkpoint — skipped.")],
        )

    # INFRA: notify stage start for timing watchdog (only if stage will actually run)
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("scenario")
    check_infra_pause()

    return None


def _clean_scenes_after_scenario_wrapper(callback_context):
    """After scenario_director: clean scenes JSON then run timeline guardian."""
    return clean_scenes_after_scenario(callback_context)


# -- Evaluator agent -----------------------------------------------------------
_EVALUATOR_INSTRUCTION = """\
You are the ADHD Compliance Evaluator for a documentary script.

Read the generated scenes from {scenes} and evaluate them against the
criteria below. A deterministic structural check has ALREADY run and
caps the ceiling verdict (see {structural_report}).  You may NOT rate
higher than that ceiling.  Read the structural report first.
{quick_test_rules}

STRUCTURAL CEILING (from pre-evaluator checks):
{structural_report}

If the structural report says OVERALL_CAP=POOR, your rating is POOR —
even if the narrative sounds fine.  The scenario creator MUST fix every
structural failure before the evaluator can give GOOD or EXCELLENT.

HARD REQUIREMENTS (any failure = POOR):
1. Every scene has exactly 3 voice blocks (V1, V2, V3)
2. No scene exceeds {max_scene_duration} seconds (~{max_words_per_scene} words per scene max)
3. No rhetorical questions anywhere in the text
4. The scenes JSON is valid and parseable
5. Scene 0 has a topic-specific hook_spec (not a generic "brain"/"city"/etc.)
6. The final scene has a complete outro_spec (closing_shot + recap_sentence + cta + brand_card)
7. A global style_lock is set and every scene's visual_notes respects it
   (no mention of styles listed in style_lock.forbidden_styles)
8. Every initialism / all-caps abbreviation in narration appears in the
   scene's pronunciation_hints (missing PAG → POOR)
9. Sum of duration_sec >= 95% of the user-requested target duration
10. All scenes are on-topic w.r.t. the user prompt (≤ 1 off-topic scene
    AND ≤ 30% tangential)

QUALITY CRITERIA:
11. Visual variety: no two consecutive scenes should suggest the same visual approach
12. Dopamine hooks: each scene opens with something attention-grabbing
13. Narrative arc: scenes build toward insight, not just list facts
14. Voice distinctiveness: V1/V2/V3 sound genuinely different in tone

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
Do NOT rate GOOD or EXCELLENT if the STRUCTURAL CEILING is POOR or FAIR.
"""

def _evaluator_before_agent(callback_context):
    """Run structural checks, then delegate to the phase-setup sequence."""
    _run_structural_checks_before_evaluator(callback_context)
    return None


scenario_evaluator = Agent(
    name="scenario_evaluator",
    model=build_model(synthesis=True),
    instruction=_EVALUATOR_INSTRUCTION,
    tools=[exit_loop],
    output_key="_last_evaluator_output",
    before_agent_callback=_evaluator_before_agent,
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
