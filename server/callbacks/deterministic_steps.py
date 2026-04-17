"""
Deterministic pipeline steps -- bypass unreliable LLM tool-calling.

The ADK agents' LLMs are unreliable at calling tools in the correct order.
These callbacks do the mechanical work (TTS generation, OTIO mutations,
video generation, assembly) programmatically, using the same underlying
tool functions but without depending on the LLM to invoke them.

Each function is a ``before_agent_callback`` or ``after_agent_callback``
that returns ``Content`` to skip/augment the LLM agent.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

from callbacks.state_manager import safe_state_dict

logger = logging.getLogger(__name__)

# Maximum duration for a single LTX-Video 2.3 clip (seconds).
# Concepts longer than this are split into sub-clips by the production stage.
_LTX_CAP = 10.0


def _safe_int(val, default: int = 0) -> int:
    """Convert a value to int, handling strings like "scene_001" or "phrase_002".

    Extracts digits from the string and converts to int. Returns *default*
    if no digits are found.
    """
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (ValueError, TypeError):
        digits = re.sub(r'[^0-9]', '', str(val))
        return int(digits) if digits else default


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def _repair_json_text(text: str) -> Optional[str]:
    """Attempt to repair common LLM JSON generation errors.

    Common error: LLM drops the opening ``{`` and ``"voice": "V3",`` for a
    voice block inside a ``voices`` array, leaving a bare ``"text":`` key
    after a ``},`` that closed the previous object.  We detect this by
    looking for ``},\n`` followed by whitespace + ``"key":`` where the key
    isn't starting a new object, and wrap it in ``{``...``}``.
    """
    # Fix: bare key-value after closing brace in an array context.
    # Pattern: }, <newline+spaces> "key": value ... without an opening {
    # We look for },\n<spaces>"<key>" and insert { before the key.
    repaired = re.sub(
        r'(},\s*\n)(\s*)("(?:text|tone|voice)"\s*:)',
        r'\1\2{\n\2  \3',
        text,
    )

    # If we inserted an opening {, we also need to close it before the next
    # }, or before ] that closes the array.  This is tricky in general, so
    # we take a simpler approach: try parsing, and if it still fails, try
    # inserting } before the next }.
    if repaired != text:
        # Try parsing the repaired text directly first
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            pass

        # More aggressive: find the exact line numbers where the regex
        # inserted opening { characters, then insert matching } before
        # the next scope-closing line (starts with } or ]) after each.
        # We track a cumulative offset because each inserted line shifts
        # subsequent indices relative to the original text.
        repair_lines = set()  # line numbers of repair-inserted {
        orig_lines = text.split('\n')
        offset = 0  # cumulative lines inserted before current position
        for i, line in enumerate(repaired.split('\n')):
            if line.strip() == '{':
                orig_idx = i - offset
                if orig_idx < 0 or orig_idx >= len(orig_lines) or orig_lines[orig_idx].strip() != '{':
                    repair_lines.add(i)
                    # The regex inserts "{\n" which adds 1 extra line
                    offset += 1

        lines = repaired.split('\n')
        fixed_lines = []
        seeking_close = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if i in repair_lines:
                seeking_close = True
                fixed_lines.append(line)
                continue
            # Insert closing } before a scope-closing line, but only
            # after we've passed a repair-inserted {.
            if seeking_close and stripped and stripped[0] in ('}', ']'):
                indent = len(line) - len(line.lstrip())
                fixed_lines.append(' ' * (indent + 2) + '}')
                seeking_close = False
            fixed_lines.append(line)

        result = '\n'.join(fixed_lines)
        try:
            json.loads(result)
            return result
        except json.JSONDecodeError:
            pass

    return None


def extract_json_array(text: str) -> Optional[list]:
    """Extract a JSON array from text that may contain preamble/markdown fences.

    Handles:
    - Pure JSON arrays
    - JSON wrapped in ```json ... ``` fences
    - JSON preceded by preamble text ("I apologize...", "Here are the scenes:")
    - Multiple JSON blocks (returns the first valid array)
    - Common LLM JSON errors (missing object wrappers) via repair
    """
    if not text or not text.strip():
        return None

    # Strategy 1: Try parsing the whole string as JSON
    try:
        result = json.loads(text.strip())
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from markdown code fences (handle optional newline)
    fence_pattern = re.compile(r'```(?:json)?\s*\n?(.*?)```', re.DOTALL)
    for match in fence_pattern.finditer(text):
        inner = match.group(1).strip()
        try:
            result = json.loads(inner)
            if isinstance(result, list):
                return result
            # If the fenced content is a dict with an array value, try extracting it
            if isinstance(result, dict):
                # Prefer known keys first
                for key in ("scenes", "visual_concepts", "content_analysis"):
                    if key in result and isinstance(result[key], list):
                        return result[key]
                for v in result.values():
                    if isinstance(v, list):
                        return v
        except (json.JSONDecodeError, ValueError):
            # Try repairing common LLM errors before giving up
            repaired = _repair_json_text(inner)
            if repaired:
                try:
                    result = json.loads(repaired)
                    if isinstance(result, list):
                        logger.info("Extracted JSON array after repair")
                        return result
                except (json.JSONDecodeError, ValueError):
                    pass
            continue

    # Strategy 3: Find the first [ ... ] block in the text
    bracket_depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == '[' and bracket_depth == 0:
            start_idx = i
            bracket_depth = 1
        elif ch == '[':
            bracket_depth += 1
        elif ch == ']':
            bracket_depth -= 1
            if bracket_depth == 0 and start_idx is not None:
                candidate = text[start_idx:i + 1]
                try:
                    result = json.loads(candidate)
                    if isinstance(result, list):
                        return result
                except (json.JSONDecodeError, ValueError):
                    # Try repair
                    repaired = _repair_json_text(candidate)
                    if repaired:
                        try:
                            result = json.loads(repaired)
                            if isinstance(result, list):
                                logger.info("Extracted JSON array after repair (strategy 3)")
                                return result
                        except (json.JSONDecodeError, ValueError):
                            pass
                    start_idx = None
                    continue

    return None


def _repair_unescaped_quotes(text: str) -> Optional[str]:
    """Fix unescaped double-quotes inside JSON string values.

    LLMs sometimes produce JSON like::

        "prompt": "...highlights the \"eat on the spot\" rule...",

    where the inner quotes are not escaped.  This function detects
    key-value lines whose string value contains extra unescaped ``"``
    characters and escapes them so the JSON can be parsed.
    """
    result_lines = []
    changed = False
    for line in text.split('\n'):
        # Match:  <indent>"<key>" : "<value>",?
        m = re.match(r'^(\s*"\w+"\s*:\s*)"(.*)"(,?)\s*$', line)
        if m:
            value = m.group(2)
            # If the value itself contains unescaped quotes, fix them.
            if '"' in value:
                # Protect already-escaped quotes, escape bare ones, restore.
                fixed = (
                    value
                    .replace('\\"', '\x00')
                    .replace('"', '\\"')
                    .replace('\x00', '\\"')
                )
                result_lines.append(f'{m.group(1)}"{fixed}"{m.group(3)}')
                changed = True
                continue
        result_lines.append(line)

    return '\n'.join(result_lines) if changed else None


def extract_json_object(text: str) -> Optional[dict]:
    """Extract a JSON object from text that may contain preamble/markdown fences."""
    if not text or not text.strip():
        return None

    # Strategy 1: Try the whole string
    try:
        result = json.loads(text.strip())
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from markdown fences (handle optional newline)
    fence_pattern = re.compile(r'```(?:json)?\s*\n?(.*?)```', re.DOTALL)
    for match in fence_pattern.finditer(text):
        inner = match.group(1).strip()
        try:
            result = json.loads(inner)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            # Try repairing unescaped quotes
            repaired = _repair_unescaped_quotes(inner)
            if repaired:
                try:
                    result = json.loads(repaired)
                    if isinstance(result, dict):
                        logger.info("Extracted JSON object after quote repair")
                        return result
                except (json.JSONDecodeError, ValueError):
                    pass
            continue

    # Strategy 3: Find first { ... } block
    brace_depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == '{' and brace_depth == 0:
            start_idx = i
            brace_depth = 1
        elif ch == '{':
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start_idx is not None:
                candidate = text[start_idx:i + 1]
                try:
                    result = json.loads(candidate)
                    if isinstance(result, dict):
                        return result
                except (json.JSONDecodeError, ValueError):
                    # Try repairing unescaped quotes
                    repaired = _repair_unescaped_quotes(candidate)
                    if repaired:
                        try:
                            result = json.loads(repaired)
                            if isinstance(result, dict):
                                logger.info(
                                    "Extracted JSON object after quote repair (strategy 3)"
                                )
                                return result
                        except (json.JSONDecodeError, ValueError):
                            pass
                    start_idx = None
                    continue

    return None


# ---------------------------------------------------------------------------
# Post-scenario: clean up state["scenes"] to pure JSON
# ---------------------------------------------------------------------------

def clean_scenes_after_scenario(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """After scenario_director: extract clean JSON from state['scenes'] and state['visual_style'].

    ADK's output_key only saves the *final* text response from the LLM.
    When the generator outputs scenes then calls create_timeline, the
    post-tool response is often empty, so output_key silently discards
    the scenes.  The after_model_callback captures scenes and visual_style
    to both state and backup files on disk.  This callback reads from
    whichever source has the data.

    Falls back to ``_approved_scenes_backup`` if the current scenes
    state cannot be parsed (e.g. the LoopAgent re-ran the generator
    after approval and the second output was malformed).

    If the scenario stage was already completed in B2, this is a no-op
    (state was restored from B2 on startup).
    """
    state = callback_context.state
    timeline_dir = os.environ.get("TIMELINE_DIR", "/tmp/documentary-pipeline/timelines")
    scenes_file = os.path.join(timeline_dir, "_scenes_backup.json")
    visual_style_file = os.path.join(timeline_dir, "_visual_style_backup.json")

    # --- Recover visual_style (may be lost by LoopAgent state scoping) -----
    raw_vs = str(state.get("visual_style", ""))
    if not raw_vs.strip() or raw_vs.strip() == "":
        if os.path.exists(visual_style_file):
            logger.info("State visual_style empty, recovering from backup %s", visual_style_file)
            with open(visual_style_file) as f:
                state["visual_style"] = f.read()
            logger.info("Recovered visual_style from disk backup")
    else:
        logger.info("visual_style present in state (%d chars)", len(raw_vs))

    # --- Try state first, then disk backup, then in-memory backup ----------
    raw_str = str(state.get("scenes", ""))
    scenes = extract_json_array(raw_str) if raw_str.strip() not in ("", "[]") else None

    if not scenes and os.path.exists(scenes_file):
        logger.info("State scenes empty/invalid, recovering from disk backup %s", scenes_file)
        with open(scenes_file) as f:
            backup_data = f.read()
        scenes = extract_json_array(backup_data)

    if not scenes:
        # Try the in-memory backup saved by _check_scenario_approval
        backup = state.get("_approved_scenes_backup", "")
        if backup:
            backup_scenes = extract_json_array(str(backup))
            if backup_scenes:
                scenes = backup_scenes
                logger.warning(
                    "Used approved scenes backup: %d scenes "
                    "(current state had malformed JSON)",
                    len(backup_scenes),
                )

    if scenes:
        # ── Duration budget: scale scene durations so total = target ──
        # The scenario LLM generates scenes whose duration_sec values sum
        # to the user's requested total (e.g. 420s for "7 minutes").  But
        # the audio stage adds structural silence gaps (inter-voice pauses
        # between V1→V2→V3 and inter-scene transitions).  These gaps are
        # part of the movie runtime.  We scale scene durations DOWN so:
        #   sum(scene_duration_sec) + total_gaps = original_target
        #
        # The gap constants MUST match those in deterministic_audio_callback.
        _INTER_VOICE_PAUSE = 1.5
        _INTER_SCENE_PAUSE = 2.5
        original_total = sum(s.get("duration_sec", 0) for s in scenes)
        if original_total > 0:
            num_scenes = len(scenes)
            # Compute total gap overhead that audio stage will insert
            total_voice_gaps = 0.0
            for s in scenes:
                voices = s.get("voices") or []
                active = sum(1 for v in voices if v.get("text", "").strip())
                total_voice_gaps += max(0, active - 1) * _INTER_VOICE_PAUSE
            total_scene_gaps = max(0, num_scenes - 1) * _INTER_SCENE_PAUSE
            total_gap_overhead = total_voice_gaps + total_scene_gaps

            narration_budget = original_total - total_gap_overhead
            if narration_budget > 0 and narration_budget < original_total:
                scale = narration_budget / original_total
                for s in scenes:
                    old_dur = s.get("duration_sec", 0)
                    s["duration_sec"] = round(old_dur * scale, 2)
                new_total = sum(s.get("duration_sec", 0) for s in scenes)
                logger.info(
                    "Duration budget: scaled narration %.1fs → %.1fs "
                    "(gaps=%.1fs, movie target=%.1fs)",
                    original_total, new_total, total_gap_overhead,
                    new_total + total_gap_overhead,
                )
            else:
                logger.warning(
                    "Duration budget: gap overhead %.1fs >= total %.1fs — "
                    "skipping scaling (too many scenes/voices for target)",
                    total_gap_overhead, original_total,
                )

        # Store per-voice narration budgets for the audio stage's QA loop.
        # Each voice gets a budget proportional to its word count within
        # the scene's (already-scaled) duration_sec.
        voice_budgets: dict[str, float] = {}  # "scene_NNN_voice" → seconds
        for s in scenes:
            sn = _safe_int(s.get("scene_num", 0))
            dur = float(s.get("duration_sec", 0))
            voices = s.get("voices") or []
            active = [v for v in voices if v.get("text", "").strip()]
            total_words = sum(len(v.get("text", "").split()) for v in active)
            if total_words <= 0:
                total_words = 1
            for v in active:
                voice_name = v.get("voice", "V1")
                words = len(v.get("text", "").split())
                budget = dur * (words / total_words) if dur > 0 else 0
                key = f"scene_{sn:03d}_{voice_name}"
                voice_budgets[key] = round(budget, 2)
        state["_voice_budgets"] = json.dumps(voice_budgets)
        logger.info("Voice budgets: %d entries, total=%.1fs",
                     len(voice_budgets), sum(voice_budgets.values()))

        state["scenes"] = json.dumps(scenes, ensure_ascii=False)
        logger.info("Cleaned scenes JSON: %d scenes extracted", len(scenes))

        # Ensure OTIO timeline exists — create deterministically if the LLM
        # did not call the create_timeline tool during scenario generation.
        tp = state.get("_timeline_path", "")
        if not tp or not os.path.exists(tp):
            from tools.otio_tools import create_timeline as _create_tl
            topic = state.get("topic", "") or "documentary"
            # Fallback: derive topic from first scene title if state topic is empty
            if topic == "documentary" and scenes:
                first_title = scenes[0].get("title", "")
                if first_title:
                    topic = first_title[:40]
            _tl_result = _create_tl(topic=str(topic), num_scenes=len(scenes))
            import json as _json
            _tl_info = _json.loads(_tl_result)
            state["_timeline_path"] = _tl_info["timeline_path"]
            logger.info(
                "Deterministically created OTIO timeline: %s (%d scenes)",
                _tl_info["timeline_path"], len(scenes),
            )

        # Upload scenario artifacts to B2 immediately
        from tools.b2_checkpoint import upload_scenario, upload_stage_marker, upload_pipeline_state, upload_timeline
        vs_raw = str(state.get("visual_style", ""))
        _b2_ok = upload_scenario(json.dumps(scenes, ensure_ascii=False), vs_raw)
        _b2_ok = upload_pipeline_state(safe_state_dict(state)) and _b2_ok
        # Upload timeline if it exists
        tp = state.get("_timeline_path", "")
        if tp and os.path.exists(tp):
            upload_timeline(tp)
        # Only mark stage complete if critical artifacts uploaded
        if _b2_ok:
            upload_stage_marker("scenario")
    else:
        logger.error(
            "Failed to extract scenes from state (len=%d), disk backup (exists=%s), "
            "and in-memory backup",
            len(raw_str), os.path.exists(scenes_file),
        )

    # INFRA: notify scenario stage complete
    from infra_agent import get_infra_agent
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_complete("scenario")

    # Run timeline guardian after cleaning
    from callbacks.timeline_guardian import timeline_guardian_callback
    return timeline_guardian_callback(callback_context)


# ---------------------------------------------------------------------------
# Narration text trimming for duration budget enforcement
# ---------------------------------------------------------------------------

_MAX_TRIM_RETRIES = 2  # max re-generation attempts per voice

def _trim_text_to_budget(text: str, actual_duration: float, budget: float) -> str:
    """Trim narration text proportionally to fit within a duration budget.

    Removes sentences from the end until the estimated word count fits
    the budget.  At ~150 words/min (standard narration rate), we estimate
    how many words the budget allows and trim accordingly.

    Returns the trimmed text, or the original text if trimming would
    remove more than 40% of the content (in that case, the scenario
    should be adjusted instead — see the audio QA gate).
    """
    if actual_duration <= budget or budget <= 0:
        return text

    ratio = budget / actual_duration  # e.g. 0.85 means keep 85%
    if ratio < 0.6:
        # Would remove >40% — flag for scenario adjustment instead
        logger.warning(
            "Narration trimming would remove %.0f%% of text "
            "(actual=%.1fs, budget=%.1fs) — too aggressive, skipping trim",
            (1 - ratio) * 100, actual_duration, budget,
        )
        return text

    words = text.split()
    target_words = max(1, int(len(words) * ratio))

    # Trim at sentence boundaries when possible
    sentences = text.replace("! ", ".|").replace("? ", ".|").replace(". ", ".|").split("|")
    trimmed_sentences = []
    word_count = 0
    for sentence in sentences:
        s_words = len(sentence.split())
        if word_count + s_words > target_words and trimmed_sentences:
            break
        trimmed_sentences.append(sentence)
        word_count += s_words

    result = " ".join(trimmed_sentences).strip()
    if not result:
        result = " ".join(words[:target_words])

    logger.info(
        "Trimmed narration: %d → %d words (ratio=%.2f, budget=%.1fs)",
        len(words), len(result.split()), ratio, budget,
    )
    return result


# ---------------------------------------------------------------------------
# Deterministic audio generation
# ---------------------------------------------------------------------------

def deterministic_audio_callback(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Before audio_agent: generate all narration deterministically.

    Parses scenes from state, generates TTS for each voice, adds clips
    to OTIO timeline, runs alignment. Returns Content to skip the LLM.

    If the audio stage was already completed in B2, skip entirely.
    """
    state = callback_context.state
    stages_complete = state.get("_b2_stages_complete", [])
    if "audio" in stages_complete:
        logger.info("B2: audio stage already complete, skipping TTS generation")
        state["pipeline_phase"] = "audio"
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Audio stage restored from B2 checkpoint — skipped.")],
        )

    # OTIO GATE: refuse to proceed if a previous stage flagged a violation
    if state.get("otio_violation"):
        from recovery import escalate_pipeline_error
        _otio_gate_msg = f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
        escalate_pipeline_error(
            operation_name="audio_otio_gate",
            error_msg=_otio_gate_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="A previous stage flagged an OTIO violation.",
            agent_policy_type="otio",
        )
        raise RuntimeError(_otio_gate_msg)

    # CONTRACT: validate preconditions before starting audio stage
    from contracts import AUDIO_CONTRACT, validate_preconditions
    validate_preconditions(AUDIO_CONTRACT, safe_state_dict(state))

    # INFRA: notify stage start + check if pipeline is paused
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("audio")
    check_infra_pause()

    state["pipeline_phase"] = "audio"

    # ── Timing loop re-iteration: clear stale narration clips ─────────
    # When the timing loop re-iterates (timing_evaluator found audio
    # overshoots the budget, scenario_refiner adjusted scenes), the
    # A1_Narration track still contains clips from the previous iteration.
    # Without clearing, add_narration_clip's idempotency check would skip
    # unchanged clips but still append new ones, and any removed/renamed
    # clips would linger — producing a corrupted timeline with overlapping
    # narration in the final assembly.
    _audio_regen = state.get("_audio_needs_regeneration", False)
    if isinstance(_audio_regen, str):
        _audio_regen = _audio_regen.lower() in ("true", "1", "yes")
    if _audio_regen:
        from tools.otio_tools import clear_narration_track
        _tl_path = state.get("_timeline_path", "")
        _removed = clear_narration_track(_tl_path)
        logger.info(
            "Timing loop re-iteration: cleared %d stale narration items", _removed,
        )
        state["_audio_needs_regeneration"] = False

    # Parse scenes
    raw_scenes = state.get("scenes", "[]")
    scenes = extract_json_array(str(raw_scenes))
    if not scenes:
        # Notify stage complete so the timing watchdog doesn't fire spuriously
        _infra = get_infra_agent()
        if _infra:
            _infra.notify_stage_complete("audio")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="ERROR: No valid scenes JSON in state")],
        )

    language = state.get("language", "en")
    timeline_path = state.get("_timeline_path", "")

    # Import tool functions directly (not via FunctionTool wrappers)
    from tools.tts_tools import generate_narration
    from tools.whisperx_tools import align_narration
    from tools.otio_tools import add_narration_clip, add_narration_gap
    from gatekeeper import check_narration_clip, has_rejects, format_audit_report

    # ── OTIO architecture constants ──────────────────────────────────
    # These pauses are PLANNED structural elements of the timeline.
    # They are written as explicit Gap items in the OTIO so that the
    # assembler renders them faithfully without any ad-hoc additions.
    INTER_VOICE_PAUSE_SEC = 1.5   # breathing room between V1→V2→V3
    INTER_SCENE_PAUSE_SEC = 2.5   # transition between scenes

    # AG-UI: emit artifact events as narration clips are generated
    from agui import get_feedback_store, ArtifactType, ArtifactStatus, ArtifactEvent
    _feedback_store = get_feedback_store()

    # Load per-voice narration budgets from scenario stage
    _raw_budgets = state.get("_voice_budgets", "{}")
    try:
        voice_budgets: dict[str, float] = json.loads(str(_raw_budgets))
    except (json.JSONDecodeError, TypeError):
        voice_budgets = {}
    logger.info("Voice budgets loaded: %d entries", len(voice_budgets))

    alignment_data = {}
    total_clips = 0
    errors = []
    # Track actual durations per voice for scene-level and total QA
    _actual_durations: dict[str, float] = {}  # "scene_NNN_voice" → actual seconds
    # Deferred gatekeeper: collect clip info for batch validation AFTER
    # all artifacts are uploaded to B2 and written to OTIO (audit trail).
    _deferred_gk_clips: list[dict] = []

    for scene_idx, scene in enumerate(scenes):
        scene_num = _safe_int(scene.get("scene_num", 0))
        voices = scene.get("voices") or scene.get("voice_blocks") or []
        # Track which voice index we're on for interleaving gaps
        active_voices = [vb for vb in voices if vb.get("text", "").strip()]
        active_voice_count = len(active_voices)
        current_voice_idx = 0

        for voice_block in voices:
            voice = voice_block.get("voice", "V1")
            text = voice_block.get("text", "")
            if not text or not text.strip():
                continue

            if language == "dual_ru_en":
                # Split into RU and EN blocks
                for lang_code, lang_tag in [("ru", "[RU]"), ("en", "[EN]")]:
                    # Extract language-specific text
                    lang_text = _extract_lang_text(text, lang_tag)
                    if not lang_text:
                        # Fallback: if no [RU] tag, use raw text as Russian
                        if lang_code == "ru":
                            lang_text = text.strip()
                            logger.warning(
                                "Scene %d %s: no [RU] tag, using raw text",
                                scene_num, voice,
                            )
                        # Fallback: if no [EN] tag, translate RU text via LLM
                        elif lang_code == "en":
                            ru_text = _extract_lang_text(text, "[RU]") or text.strip()
                            lang_text = _translate_via_llm(ru_text, "ru", "en")
                            if not lang_text:
                                logger.error(
                                    "Scene %d %s: [EN] missing and translation failed",
                                    scene_num, voice,
                                )
                                continue
                            logger.info(
                                "Scene %d %s: translated RU→EN (%d chars)",
                                scene_num, voice, len(lang_text),
                            )
                    if not lang_text:
                        continue

                    voice_suffix = f"{voice}_{lang_code.upper()}"

                    try:
                        # AG-UI: emit "generating" narration artifact
                        narr_artifact_id = f"narr-s{scene_num:03d}-{voice_suffix}"
                        _feedback_store.register_artifact(ArtifactEvent(
                            id=narr_artifact_id,
                            artifact_type=ArtifactType.NARRATION,
                            status=ArtifactStatus.GENERATING,
                            scene_num=scene_num,
                            language=lang_code,
                            metadata={"voice": voice_suffix, "text_len": len(lang_text)},
                            timestamp=time.time(),
                        ))

                        # Generate narration
                        result_json = generate_narration(
                            scene_num=scene_num,
                            voice_role=voice_suffix,
                            text=lang_text,
                        )
                        result = json.loads(result_json)
                        wav_path = result.get("wav_path", "")
                        duration = result.get("duration", 0)

                        if wav_path and duration > 0:
                            # Track RU clip durations for scene/total QA
                            # (EN clips are alternate and excluded from QA)
                            if lang_code == "ru":
                                budget_key = f"scene_{scene_num:03d}_{voice}"
                                _actual_durations[budget_key] = duration

                            # B2 upload already happened inside generate_narration().
                            # Gatekeeper runs AFTER all clips are in B2 + OTIO (audit trail).
                            voice_budget = voice_budgets.get(
                                f"scene_{scene_num:03d}_{voice}", 0,
                            )
                            _deferred_gk_clips.append({
                                "wav_path": wav_path,
                                "scene_num": scene_num,
                                "voice": voice_suffix,
                                "duration": duration,
                                "budget": voice_budget if lang_code == "ru" else 0,
                            })

                            # AG-UI: update narration artifact
                            _feedback_store.register_artifact(ArtifactEvent(
                                id=narr_artifact_id,
                                artifact_type=ArtifactType.NARRATION,
                                status=ArtifactStatus.PENDING_REVIEW,
                                scene_num=scene_num,
                                language=lang_code,
                                preview_url=wav_path,
                                duration_sec=duration,
                                metadata={"voice": voice_suffix, "text_len": len(lang_text)},
                                timestamp=time.time(),
                            ))

                            # Add clip to OTIO timeline
                            clip_result_json = add_narration_clip(
                                scene_num=scene_num,
                                voice=voice_suffix,
                                wav_path=wav_path,
                                duration=duration,
                                tool_context=_MockToolContext(state),
                            )
                            clip_result = json.loads(clip_result_json)
                            if "error" in clip_result:
                                from recovery import escalate_pipeline_error
                                _clip_msg = (
                                    f"OTIO VIOLATION: failed to add narration clip "
                                    f"scene {scene_num} {voice_suffix}: {clip_result['error']}"
                                )
                                escalate_pipeline_error(
                                    operation_name="audio_narration_clip",
                                    error_msg=_clip_msg,
                                    severity="critical",
                                    default_action="abort",
                                    agent_policy_type="otio",
                                )
                                raise RuntimeError(_clip_msg)
                            total_clips += 1

                            # Run alignment
                            align_result_json = align_narration(
                                wav_path=wav_path,
                                text=lang_text,
                                language=lang_code,
                            )
                            align_key = f"scene_{scene_num:03d}_{voice_suffix}"
                            alignment_data[align_key] = json.loads(align_result_json)

                    except RuntimeError:
                        raise  # TTS failures are fatal — never swallow
                    except Exception as e:
                        err_msg = f"Error processing scene {scene_num} {voice_suffix}: {e}"
                        logger.error(err_msg)
                        errors.append(err_msg)

            else:
                # Single language mode — with duration budget enforcement
                lang_code = language if language in ("ru", "en") else "en"
                budget_key = f"scene_{scene_num:03d}_{voice}"
                voice_budget = voice_budgets.get(budget_key, 0)
                current_text = text
                try:
                    # Budget-aware TTS loop: generate, measure, trim if over
                    wav_path = ""
                    duration = 0.0
                    for _trim_attempt in range(_MAX_TRIM_RETRIES + 1):
                        result_json = generate_narration(
                            scene_num=scene_num,
                            voice_role=voice,
                            text=current_text,
                            language=lang_code,
                        )
                        result = json.loads(result_json)
                        wav_path = result.get("wav_path", "")
                        duration = result.get("duration", 0)

                        if not wav_path or duration <= 0:
                            break  # TTS failed — fall through to error handling

                        # Check against budget (10% tolerance)
                        if voice_budget > 0 and duration > voice_budget * 1.10:
                            if _trim_attempt < _MAX_TRIM_RETRIES:
                                logger.warning(
                                    "Scene %d %s: narration %.1fs > budget %.1fs "
                                    "(+%.0f%%), trimming text (attempt %d/%d)",
                                    scene_num, voice, duration, voice_budget,
                                    ((duration / voice_budget) - 1) * 100,
                                    _trim_attempt + 1, _MAX_TRIM_RETRIES,
                                )
                                current_text = _trim_text_to_budget(
                                    current_text, duration, voice_budget,
                                )
                                # Delete cached WAV so TTS regenerates
                                if os.path.exists(wav_path):
                                    os.remove(wav_path)
                                sidecar = wav_path.replace(".wav", ".txt")
                                if os.path.exists(sidecar):
                                    os.remove(sidecar)
                                continue
                            else:
                                logger.warning(
                                    "Scene %d %s: still over budget after %d trims "
                                    "(%.1fs > %.1fs) — accepting as-is",
                                    scene_num, voice, _MAX_TRIM_RETRIES,
                                    duration, voice_budget,
                                )
                        break  # Within budget or no budget — proceed

                    if wav_path and duration > 0:
                        # Track actual duration for scene/total QA
                        _actual_durations[budget_key] = duration

                        # B2 upload already happened inside generate_narration().
                        # Gatekeeper runs AFTER all clips are in B2 + OTIO (audit trail).
                        _deferred_gk_clips.append({
                            "wav_path": wav_path,
                            "scene_num": scene_num,
                            "voice": voice,
                            "duration": duration,
                            "budget": voice_budget,
                        })

                        clip_result_json = add_narration_clip(
                            scene_num=scene_num,
                            voice=voice,
                            wav_path=wav_path,
                            duration=duration,
                            tool_context=_MockToolContext(state),
                        )
                        clip_result = json.loads(clip_result_json)
                        if "error" in clip_result:
                            from recovery import escalate_pipeline_error
                            _clip_msg = (
                                f"OTIO VIOLATION: failed to add narration clip "
                                f"scene {scene_num} {voice}: {clip_result['error']}"
                            )
                            escalate_pipeline_error(
                                operation_name="audio_narration_clip",
                                error_msg=_clip_msg,
                                severity="critical",
                                default_action="abort",
                            )
                            raise RuntimeError(_clip_msg)
                        total_clips += 1

                        align_result_json = align_narration(
                            wav_path=wav_path,
                            text=current_text,
                            language=lang_code,
                        )
                        align_key = f"scene_{scene_num:03d}_{voice}"
                        alignment_data[align_key] = json.loads(align_result_json)

                except RuntimeError:
                    raise  # TTS failures are fatal — never swallow
                except Exception as e:
                    err_msg = f"Error processing scene {scene_num} {voice}: {e}"
                    logger.error(err_msg)
                    errors.append(err_msg)

            # ── OTIO: interleave inter-voice gap AFTER this voice ──────
            # Silence gap on the NARRATION track only (except after the
            # last voice).  The VIDEO track has NO gaps — video clips are
            # generated long enough to cover narration + the following
            # pause (see production callback / get_video_slot_durations).
            current_voice_idx += 1
            if current_voice_idx < active_voice_count:
                _mock_ctx = _MockToolContext(state)
                add_narration_gap(
                    scene_num=scene_num,
                    duration=INTER_VOICE_PAUSE_SEC,
                    gap_type="inter_voice",
                    gap_index=current_voice_idx,
                    tool_context=_mock_ctx,
                )

        logger.info(
            "Scene %d: added %d inter-voice gaps (%.1fs each) to OTIO",
            scene_num, max(0, active_voice_count - 1), INTER_VOICE_PAUSE_SEC,
        )

        # ── OTIO: add inter-scene transition Gap AFTER this scene ─────
        # Insert immediately after each scene (except the last) so the
        # OTIO track order is: scene1_clips, inter_scene_gap, scene2_clips...
        if scene_idx < len(scenes) - 1:
            _mock_ctx = _MockToolContext(state)
            add_narration_gap(
                scene_num=scene_num,
                duration=INTER_SCENE_PAUSE_SEC,
                gap_type="inter_scene",
                gap_index=scene_idx,
                tool_context=_mock_ctx,
            )
            logger.info(
                "Scene %d: added inter-scene gap (%.1fs) to OTIO",
                scene_num, INTER_SCENE_PAUSE_SEC,
            )

    # Store alignment data in state
    state["whisperx_alignment"] = json.dumps(alignment_data)

    # Upload audio artifacts to B2 — artifacts FIRST, then gatekeeper, then stage marker.
    from tools.b2_checkpoint import upload_stage_marker, upload_pipeline_state, upload_timeline, upload_gatekeeper_report
    _b2_ok = upload_pipeline_state(safe_state_dict(state))
    tp = state.get("_timeline_path", "")
    if tp and os.path.exists(tp):
        upload_timeline(tp)
    # NOTE: stage marker is uploaded AFTER gatekeeper validation below.
    # Uploading it here would let a rejected stage be skipped on restart.

    # GATEKEEPER: batch validation AFTER all artifacts are in B2.
    # Every narration clip is already uploaded (inside generate_narration)
    # and written to OTIO.  Now validate and upload the audit report.
    #
    # QA levels (same pattern as video production):
    #   1. Individual clip QA  — file exists, duration > 0, size > 1KB
    #   2. Individual budget QA — narration fits voice time budget
    #   3. Scene-level QA      — scene narration + gaps ≤ scene duration_sec
    #   4. Total QA            — full assembled narration ≈ target duration
    from gatekeeper import (
        check_narration_duration_budget,
        check_scene_narration_total,
        check_total_narration_duration,
    )
    all_gk_checks = []

    # Level 1 + 2: individual clip structural + budget checks
    for clip_info in _deferred_gk_clips:
        gk_checks = check_narration_clip(
            wav_path=clip_info["wav_path"],
            scene_num=clip_info["scene_num"],
            voice=clip_info["voice"],
            duration=clip_info["duration"],
            stage="audio",
        )
        all_gk_checks.extend(gk_checks)

        # Budget check per voice
        budget_checks = check_narration_duration_budget(
            scene_num=clip_info["scene_num"],
            voice=clip_info["voice"],
            actual_duration=clip_info["duration"],
            budget=clip_info.get("budget", 0),
            stage="audio",
        )
        all_gk_checks.extend(budget_checks)

    # Level 3: scene-level QA
    for scene in scenes:
        sn = _safe_int(scene.get("scene_num", 0))
        scene_budget = float(scene.get("duration_sec", 0))
        scene_voices = scene.get("voices") or []
        active = [v for v in scene_voices if v.get("text", "").strip()]
        scene_voice_gaps = max(0, len(active) - 1) * INTER_VOICE_PAUSE_SEC

        # Sum actual narration durations for this scene
        scene_narr_total = 0.0
        for v in active:
            vname = v.get("voice", "V1")
            key = f"scene_{sn:03d}_{vname}"
            scene_narr_total += _actual_durations.get(key, 0)

        scene_checks = check_scene_narration_total(
            scene_num=sn,
            actual_scene_total=scene_narr_total,
            scene_budget=scene_budget,
            gap_overhead=scene_voice_gaps,
            stage="audio",
        )
        all_gk_checks.extend(scene_checks)

    # Level 4: total assembled narration QA
    total_narration = sum(_actual_durations.values())
    total_voice_gaps = sum(
        max(0, len([v for v in (s.get("voices") or []) if v.get("text", "").strip()]) - 1)
        * INTER_VOICE_PAUSE_SEC
        for s in scenes
    )
    total_scene_gaps = max(0, len(scenes) - 1) * INTER_SCENE_PAUSE_SEC
    actual_movie_duration = total_narration + total_voice_gaps + total_scene_gaps
    # The original target is what the scenario generated before scaling
    # Reconstruct: scaled_total + gap_overhead = original_target
    scaled_narration_total = sum(s.get("duration_sec", 0) for s in scenes)
    target_movie_duration = scaled_narration_total + total_voice_gaps + total_scene_gaps
    total_checks = check_total_narration_duration(
        actual_total=actual_movie_duration,
        target_total=target_movie_duration,
        stage="audio",
    )
    all_gk_checks.extend(total_checks)
    logger.info(
        "Narration QA: total=%.1fs (narration=%.1fs + voice_gaps=%.1fs + "
        "scene_gaps=%.1fs), target=%.1fs",
        actual_movie_duration, total_narration, total_voice_gaps,
        total_scene_gaps, target_movie_duration,
    )

    # Upload gatekeeper audit report to B2 (audit trail)
    if all_gk_checks:
        audit_report = format_audit_report(all_gk_checks, "audio")
        upload_gatekeeper_report(audit_report, "audio")

    # NOW evaluate rejects — everything is safely in B2
    _gk_rejected = False
    if has_rejects(all_gk_checks):
        _gk_rejected = True
        rejects = [c for c in all_gk_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        from recovery import escalate_pipeline_error
        response = escalate_pipeline_error(
            operation_name="audio_gatekeeper",
            error_msg=(
                f"GATEKEEPER REJECT (audio stage, {len(rejects)} reject(s) — "
                f"audit report uploaded to B2): {reject_msgs}"
            ),
            severity="critical",
            default_action="abort",
            diagnosis_hint=(
                "Narration duration drift exceeds threshold. "
                "Root cause is likely insufficient text in the scenario "
                "(LLM generated too few scenes or too-short narration)."
            ),
            agent_policy_type="audio",
            pipeline_state=safe_state_dict(state),
            diagnostic_data={
                "rejects": [{"message": c.message, "verdict": c.verdict.value} for c in rejects],
                "total_checks": len(all_gk_checks),
                "scenes": state.get("scenes", []),
            },
        )
        if response.get("action") not in ("skip", "retry_with_fix", "amend"):
            raise RuntimeError(
                f"GATEKEEPER REJECT (audio stage, {len(rejects)} reject(s) — "
                f"audit report uploaded to B2): {reject_msgs}"
            )
        logger.warning(
            "Audio gatekeeper rejection escalated and resolved with action=%s — continuing pipeline",
            response.get("action"),
        )

    # Stage marker AFTER gatekeeper passes — rejected stages must NOT be
    # marked complete, otherwise they'd be skipped on pipeline restart.
    if _b2_ok and not _gk_rejected:
        upload_stage_marker("audio")

    summary_parts = [
        f"Audio generation complete: {total_clips} narration clips added to timeline.",
        f"Alignment data: {len(alignment_data)} entries.",
    ]
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:3])}")

    logger.info("Deterministic audio: %d clips, %d alignments", total_clips, len(alignment_data))

    # INFRA: notify stage complete
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_complete("audio")

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text="\n".join(summary_parts))],
    )


def _translate_via_llm(text: str, src_lang: str, tgt_lang: str) -> str:
    """Translate text between languages using LiteLLM (best-effort).

    Returns translated text or empty string on failure.
    """
    lang_names = {"ru": "Russian", "en": "English"}
    src_name = lang_names.get(src_lang, src_lang)
    tgt_name = lang_names.get(tgt_lang, tgt_lang)

    try:
        import litellm
        response = litellm.completion(
            model="openrouter/google/gemini-2.5-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate the following {src_name} text to {tgt_name}. "
                        "Output ONLY the translated text, nothing else. "
                        "Preserve the tone and style of the original."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        translated = response.choices[0].message.content.strip()
        return translated
    except Exception as e:
        logger.error("Translation %s→%s failed: %s", src_lang, tgt_lang, e)
        return ""


def _extract_lang_text(text: str, lang_tag: str) -> str:
    """Extract language-specific text from a dual-language block.

    Format: "[RU] Russian text\n[EN] English text"
    """
    lines = text.split("\n")
    collecting = False
    result_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(lang_tag):
            collecting = True
            # Get text after the tag
            after_tag = stripped[len(lang_tag):].strip()
            if after_tag:
                result_lines.append(after_tag)
        elif stripped.startswith("[") and "]" in stripped[:5]:
            # Another language tag starts — stop collecting
            collecting = False
        elif collecting:
            result_lines.append(line)

    return " ".join(result_lines).strip()


# ---------------------------------------------------------------------------
# Post-visual-director: write OTIO gap metadata from visual_concepts
# ---------------------------------------------------------------------------

def _normalize_concept_durations(
    concepts: list[dict],
    narr_durations: dict,
) -> list[dict]:
    """Normalize visual concept durations AND count to match narration timing.

    ARCHITECTURE RULES:
    1. The NUMBER of visual concepts per scene MUST equal the number of
       narration phrases (V1, V2, V3) for that scene.  The gatekeeper
       enforces 1:1 mapping between concepts and phrases.
    2. The sum of video concept durations for a scene MUST equal the sum
       of VIDEO SLOT durations (narration + following gap) for that scene,
       ensuring continuous video with no freeze-frames during pauses.

    The LLM visual director may generate more concepts than narration
    phrases (e.g. 7 concepts for 3 phrases).  This function:
    - Consolidates concepts by merging groups into one concept per phrase,
      picking the best prompt from each group and combining prompts.
    - Scales the resulting durations to match the narration duration.
    - Caps individual durations at 10.0s (LTX-2.3 limit), splitting if
      needed.

    Args:
        concepts: List of visual concept dicts from the LLM.
        narr_durations: Dict from get_video_slot_durations() — each voice's
            full time slot (narration + following gap) so video clips cover
            the entire timeline with no freeze-frames.

    Returns:
        New list of concepts with normalized durations and counts.
    """
    # Group concepts by scene
    by_scene: dict[int, list[dict]] = {}
    for c in concepts:
        sn = c.get("scene_num", 0)
        by_scene.setdefault(sn, []).append(c)

    normalized: list[dict] = []

    for sn, scene_concepts in sorted(by_scene.items()):
        scene_phrases = narr_durations.get(sn, [])
        if not scene_phrases:
            # No narration data — keep concepts as-is (fallback)
            normalized.extend(scene_concepts)
            continue

        num_phrases = len(scene_phrases)
        target_dur = sum(dur for _, dur in scene_phrases)

        if target_dur <= 0:
            normalized.extend(scene_concepts)
            continue

        # ── COUNT NORMALIZATION: ensure concept count == phrase count ──
        # When the LLM generates more concepts than narration phrases,
        # distribute concepts evenly across phrases and merge each group.
        # When fewer, duplicate the last concept to fill missing phrases.
        if len(scene_concepts) > num_phrases:
            logger.info(
                "Scene %d: consolidating %d concepts → %d (matching %d phrases)",
                sn, len(scene_concepts), num_phrases, num_phrases,
            )
            consolidated: list[dict] = []
            # Distribute concepts evenly across phrases using round-robin
            # allocation so each phrase gets roughly equal coverage.
            per_phrase = len(scene_concepts) / num_phrases
            for pidx in range(num_phrases):
                start_idx = int(round(pidx * per_phrase))
                end_idx = int(round((pidx + 1) * per_phrase))
                group = scene_concepts[start_idx:end_idx]
                if not group:
                    group = [scene_concepts[-1]]  # safety fallback

                # Merge: use the first concept as base, combine prompts
                merged = dict(group[0])
                if len(group) > 1:
                    # Combine unique prompts, keeping the first as primary
                    all_prompts = []
                    seen_prompts: set[str] = set()
                    for g in group:
                        p = g.get("prompt", "")
                        if p and p not in seen_prompts:
                            all_prompts.append(p)
                            seen_prompts.add(p)
                    # Use primary prompt but note the merged visual intent
                    if len(all_prompts) > 1:
                        merged["prompt"] = all_prompts[0] + ". Then: " + ". ".join(all_prompts[1:])
                    # Sum durations from the group (will be re-scaled below)
                    merged["duration"] = sum(
                        g.get("duration", 5.0) for g in group
                    )
                merged["phrase_idx"] = pidx
                merged["scene_num"] = sn
                consolidated.append(merged)
            scene_concepts = consolidated
        elif len(scene_concepts) < num_phrases:
            # Expand: duplicate last concept to fill missing phrases
            logger.info(
                "Scene %d: expanding %d concepts → %d (matching %d phrases)",
                sn, len(scene_concepts), num_phrases, num_phrases,
            )
            template = scene_concepts[-1]
            while len(scene_concepts) < num_phrases:
                extra = dict(template)
                extra["phrase_idx"] = len(scene_concepts)
                extra["scene_num"] = sn
                extra["prompt"] = template.get("prompt", "") + " (extended coverage)"
                scene_concepts.append(extra)

        # ── DURATION SCALING: match narration duration per phrase ──
        # Assign each concept the duration of its corresponding phrase.
        # IMPORTANT: Do NOT split concepts here even if duration > LTX cap.
        # The production stage handles splitting into sub-clips internally.
        # Splitting here would break the 1:1 concept↔phrase invariant that
        # the gatekeeper enforces.
        scene_normalized: list[dict] = []
        for pidx, c in enumerate(scene_concepts):
            c_copy = dict(c)
            c_copy["phrase_idx"] = pidx
            c_copy["scene_num"] = sn

            if pidx < len(scene_phrases):
                # Use exact narration phrase duration
                phrase_dur = scene_phrases[pidx][1]
            else:
                # Fallback: proportional scaling
                current_dur = sum(cc.get("duration", 5.0) for cc in scene_concepts)
                phrase_dur = c_copy.get("duration", 5.0) * (target_dur / current_dur) if current_dur > 0 else 5.0

            c_copy["duration"] = round(phrase_dur, 2)
            c_copy["end_time"] = c_copy.get("start_time", 0) + c_copy["duration"]
            # Store the full duration; production will split into ≤10s clips
            # if needed for LTX-2.3.  We preserve 1 concept = 1 phrase here.
            if phrase_dur > _LTX_CAP:
                c_copy["needs_split"] = True
                c_copy["split_count"] = math.ceil(phrase_dur / _LTX_CAP)
            scene_normalized.append(c_copy)

        normalized.extend(scene_normalized)

        # Log normalization
        new_dur = sum(c.get("duration", 0) for c in normalized if c.get("scene_num") == sn)
        logger.info(
            "Scene %d: %d concepts, %.2fs total (target=%.2fs, phrases=%d)",
            sn, len(scene_normalized), new_dur, target_dur, num_phrases,
        )

    return normalized


def write_visual_metadata_to_otio(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """After visual_director: write prompt/LoRA metadata to OTIO gaps.

    The visual director's LLM outputs visual_concepts to state but doesn't
    update the OTIO timeline gaps with the metadata. This callback does that.

    ARCHITECTURE: After parsing concepts, normalize their durations to match
    narration timing from the OTIO.  The LLM controls WHERE visual breaks
    happen; the normalizer controls HOW LONG each segment lasts.
    """
    state = callback_context.state
    raw_concepts = state.get("visual_concepts", "")
    logger.info("Visual concepts state type=%s, len=%d",
                type(raw_concepts).__name__,
                len(str(raw_concepts)) if raw_concepts else 0)

    # Try to parse visual concepts
    concepts = None
    raw_str = str(raw_concepts)

    # Strategy 1: Try as JSON array directly
    concepts = extract_json_array(raw_str)

    # Strategy 2: Try as JSON object with visual_concepts key
    if not concepts:
        obj = extract_json_object(raw_str)
        if obj and "visual_concepts" in obj:
            concepts = obj["visual_concepts"]
        elif obj:
            # Maybe the object IS a single concept
            logger.info("Found JSON object but no visual_concepts key. Keys: %s",
                        list(obj.keys())[:5])

    # Strategy 3: Try to find any JSON array in the text (more aggressive)
    if not concepts and raw_str:
        # Look for visual_concepts key in raw text
        vc_idx = raw_str.find('"visual_concepts"')
        if vc_idx >= 0:
            after_key = raw_str[vc_idx + len('"visual_concepts"'):]
            # Find the colon, then extract the array
            colon_idx = after_key.find(':')
            if colon_idx >= 0:
                concepts = extract_json_array(after_key[colon_idx + 1:])
                if concepts:
                    logger.info("Extracted visual_concepts via text search: %d concepts", len(concepts))

    if not concepts:
        logger.warning("No visual concepts found to write to OTIO (raw=%s...)",
                       raw_str[:200] if raw_str else "empty")
        # Notify stage complete so the timing watchdog doesn't fire spuriously
        from infra_agent import get_infra_agent
        _infra = get_infra_agent()
        if _infra:
            _infra.notify_stage_complete("visual_direction")
        # Still run timeline guardian
        from callbacks.timeline_guardian import timeline_guardian_callback
        return timeline_guardian_callback(callback_context)

    # ── ARCHITECTURE: normalize concept durations to match VIDEO slots ──
    # The LLM controls WHERE visual breaks happen.  This normalizer
    # scales durations so each concept covers the narration PLUS the
    # following silence gap — ensuring continuous video with no freeze-frames.
    from tools.otio_tools import get_video_slot_durations
    slot_durations = get_video_slot_durations(
        tool_context=_MockToolContext(state),
    )
    if slot_durations:
        concepts = _normalize_concept_durations(concepts, slot_durations)
        # Store normalized concepts back into state so production uses them
        state["visual_concepts"] = json.dumps(concepts)
        logger.info("Normalized %d visual concepts to match video slot timing", len(concepts))

    # ── Expose WhisperX word-level data to visual concepts ──────────
    # The alignment data from the audio stage is stored in state.  We
    # attach it to each concept so the production stage (or future
    # intonation-aware visual concepters) can use word-level timing.
    alignment_raw = state.get("whisperx_alignment", "{}")
    try:
        alignment_data = json.loads(alignment_raw) if alignment_raw else {}
    except (json.JSONDecodeError, TypeError):
        alignment_data = {}
    if alignment_data:
        for concept in concepts:
            sn = concept.get("scene_num", 0)
            # Attach ALL alignment data for this scene rather than
            # trying to map phrase_idx to a voice index (which is
            # incorrect after concept normalization re-indexes phrase_idx).
            # Each alignment key is scene_{NNN}_{voice} or
            # scene_{NNN}_{voice}_{lang} in dual mode.
            _sn_int = _safe_int(sn)
            scene_prefix = f"scene_{_sn_int:03d}_"
            scene_align = {
                k: v for k, v in alignment_data.items()
                if k.startswith(scene_prefix)
            }
            if scene_align:
                concept["whisperx_alignment"] = scene_align

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        logger.error("Timeline not found at %s", timeline_path)
        # Notify stage complete so the timing watchdog doesn't fire spuriously
        from infra_agent import get_infra_agent
        _infra = get_infra_agent()
        if _infra:
            _infra.notify_stage_complete("visual_direction")
        from callbacks.timeline_guardian import timeline_guardian_callback
        return timeline_guardian_callback(callback_context)

    import opentimelineio as otio
    from tools.otio_tools import _otio_lock

    video_track_missing = False
    updated = 0

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        video_track = None
        for track in timeline.tracks:
            if track.name == "V1_Video":
                video_track = track
                break

        if video_track is None:
            logger.error("V1_Video track not found")
            video_track_missing = True
        else:
            # Build a map of scene_num (int) -> concepts
            scene_concepts: dict[int, list] = {}
            for concept in concepts:
                sn = _safe_int(concept.get("scene_num", 0))
                if sn not in scene_concepts:
                    scene_concepts[sn] = []
                scene_concepts[sn].append(concept)

            # Update gap metadata
            for item in video_track:
                if isinstance(item, otio.schema.Gap):
                    meta = item.metadata.get("documentary", {})
                    scene_num = meta.get("scene_num", 0)
                    if scene_num in scene_concepts:
                        # Use the first concept for this scene
                        concept = scene_concepts[scene_num][0]
                        meta["prompt"] = concept.get("prompt", "")
                        meta["lora_id"] = concept.get("lora_id", "documentary-realism")
                        meta["lora_weight"] = concept.get("lora_weight", 0.75)
                        meta["visual_phrases"] = scene_concepts[scene_num]
                        item.metadata["documentary"] = meta
                        updated += 1

            otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Updated %d OTIO gaps with visual metadata", updated)

    # Upload visual direction artifacts to B2 immediately
    from tools.b2_checkpoint import upload_visual_concepts, upload_stage_marker, upload_pipeline_state, upload_timeline
    _b2_ok = True
    raw_vc = str(callback_context.state.get("visual_concepts", ""))
    if raw_vc:
        _b2_ok = upload_visual_concepts(raw_vc) and _b2_ok
    _b2_ok = upload_pipeline_state(safe_state_dict(callback_context.state)) and _b2_ok
    tp = callback_context.state.get("_timeline_path", "")
    if tp and os.path.exists(tp):
        upload_timeline(tp)
    # Only mark stage complete if critical artifacts uploaded
    if _b2_ok:
        upload_stage_marker("visual_direction")

    # INFRA: notify visual_direction stage complete
    from infra_agent import get_infra_agent
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_complete("visual_direction")

    # Run timeline guardian
    from callbacks.timeline_guardian import timeline_guardian_callback
    return timeline_guardian_callback(callback_context)


# ---------------------------------------------------------------------------
# Deterministic production (video generation)
# ---------------------------------------------------------------------------

def deterministic_production_callback(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Before production_supervisor: generate all video clips deterministically.

    Reads visual_concepts from state, generates video clips for each phrase,
    probes results, and adds them to the OTIO timeline.

    If the production stage was already completed in B2, skip entirely.
    """
    state = callback_context.state
    stages_complete = state.get("_b2_stages_complete", [])
    if "production" in stages_complete:
        logger.info("B2: production stage already complete, skipping video generation")
        state["pipeline_phase"] = "production"
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Production stage restored from B2 checkpoint — skipped.")],
        )

    # OTIO GATE: refuse to proceed if a previous stage flagged a violation
    if state.get("otio_violation"):
        from recovery import escalate_pipeline_error
        _otio_gate_msg = f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
        escalate_pipeline_error(
            operation_name="production_otio_gate",
            error_msg=_otio_gate_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="A previous stage flagged an OTIO violation.",
        )
        raise RuntimeError(_otio_gate_msg)

    # CONTRACT: validate preconditions before starting production stage
    from contracts import PRODUCTION_CONTRACT, validate_preconditions
    validate_preconditions(PRODUCTION_CONTRACT, safe_state_dict(state))

    # INFRA: notify stage start + check if pipeline is paused
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("production")
    check_infra_pause()

    state["pipeline_phase"] = "production"

    raw_concepts = state.get("visual_concepts", "")
    concepts = extract_json_array(str(raw_concepts))
    if not concepts:
        obj = extract_json_object(str(raw_concepts))
        if obj and "visual_concepts" in obj:
            concepts = obj["visual_concepts"]

    # Extract movie-level visual style for QA enforcement
    raw_visual_style = state.get("visual_style", "")
    visual_style_str = ""
    visual_style_avoid = []
    if raw_visual_style:
        try:
            vs = json.loads(str(raw_visual_style)) if isinstance(raw_visual_style, str) else raw_visual_style
            if isinstance(vs, dict):
                visual_style_str = json.dumps(vs)
                visual_style_avoid = vs.get("avoid", [])
        except (json.JSONDecodeError, TypeError):
            visual_style_str = str(raw_visual_style)

    if not concepts:
        # Fallback: generate a simple concept per scene from scenes data
        raw_scenes = state.get("scenes", "[]")
        scenes = extract_json_array(str(raw_scenes))
        if scenes:
            concepts = []
            for scene in scenes:
                sn = scene.get("scene_num", 0)
                concepts.append({
                    "scene_num": sn,
                    "phrase_idx": 0,
                    "duration": min(scene.get("duration_sec", 5), 10.0),
                    "prompt": f"Documentary footage: {scene.get('title', 'scene')}. {scene.get('visual_notes', '')}",
                    "start_time": 0.0,
                    "end_time": min(scene.get('duration_sec', 5), 10.0),
                    "lora_id": "documentary-realism",
                    "lora_weight": 0.75,
                })

    if not concepts:
        # Notify stage complete so the timing watchdog doesn't fire spuriously
        _infra = get_infra_agent()
        if _infra:
            _infra.notify_stage_complete("production")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="ERROR: No visual concepts found")],
        )

    # ── Consolidate sub-phrase concepts into one per voice per scene ────
    # In full-scale mode the visual director creates multiple sub-phrase
    # visual concepts per scene (6-7), but production generates exactly
    # one video clip per narration phrase (V1/V2/V3 = phrase_idx 0/1/2).
    # Merge sub-phrases by (scene_num, voice) → combined prompt + LoRA.
    _voice_to_idx = {"V1": 0, "V2": 1, "V3": 2}
    _has_voice_field = any(c.get("voice") for c in concepts)
    if _has_voice_field and len(concepts) > 0:
        _grouped: dict[tuple[int, str], list[dict]] = {}
        for c in concepts:
            sn = c.get("scene_num", 0)
            voice = c.get("voice", "V1")
            key = (sn, voice)
            _grouped.setdefault(key, []).append(c)

        consolidated = []
        for (sn, voice), sub_phrases in sorted(_grouped.items()):
            # Combine prompts from all sub-phrases for richer video generation
            combined_prompts = []
            for sp in sub_phrases:
                p = sp.get("prompt", "") or sp.get("content", "")
                if p:
                    combined_prompts.append(p)
            combined_prompt = ". ".join(combined_prompts) if combined_prompts else ""

            # Use the dominant LoRA style (most common) and max weight
            lora_styles = [sp.get("lora_style") or sp.get("lora_id", "documentary-realism") for sp in sub_phrases]
            from collections import Counter
            dominant_lora = Counter(lora_styles).most_common(1)[0][0] if lora_styles else "documentary-realism"
            max_weight = max((sp.get("lora_weight", 0.75) for sp in sub_phrases), default=0.75)

            # Duration from timing data or sum of sub-phrase durations
            total_dur = 0.0
            for sp in sub_phrases:
                st = sp.get("start_time", 0.0)
                et = sp.get("end_time", st + 5.0)
                total_dur += (et - st)
            if total_dur <= 0:
                total_dur = 5.0

            entry: dict = {
                "scene_num": sn,
                "phrase_idx": _voice_to_idx.get(voice, 0),
                "voice": voice,
                "duration": total_dur,
                "prompt": combined_prompt,
                "lora_id": dominant_lora,
                "lora_weight": max_weight,
                "negative_prompt": sub_phrases[0].get("negative_prompt", ""),
                "start_time": sub_phrases[0].get("start_time", 0.0),
                "end_time": sub_phrases[-1].get("end_time", total_dur),
            }
            if total_dur > _LTX_CAP:
                entry["needs_split"] = True
                entry["split_count"] = math.ceil(total_dur / _LTX_CAP)
            consolidated.append(entry)

        logger.info(
            "Consolidated %d sub-phrase concepts → %d per-voice concepts",
            len(concepts), len(consolidated),
        )
        concepts = consolidated

    from tools.video_tools import generate_video_clip, probe_clip
    from tools.otio_tools import add_video_clip, get_video_slot_durations
    from gatekeeper import check_video_clip, check_stage_handoff, has_rejects, intervention_window, format_audit_report

    # GATEKEEPER: stage handoff check (visual_direction → production)
    handoff_checks = check_stage_handoff("visual_direction", "production", safe_state_dict(state))
    if has_rejects(handoff_checks):
        rejects = [c for c in handoff_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        from recovery import escalate_pipeline_error
        response = escalate_pipeline_error(
            operation_name="production_handoff_gatekeeper",
            error_msg=f"GATEKEEPER BLOCKED production start: {reject_msgs}",
            severity="critical",
            default_action="abort",
            diagnosis_hint="Visual direction stage output failed gatekeeper checks.",
            agent_policy_type="video",
            pipeline_state=safe_state_dict(state),
        )
        if response.get("action") not in ("skip", "retry_with_fix", "amend"):
            raise RuntimeError(
                f"GATEKEEPER BLOCKED production start: {reject_msgs}"
            )
        logger.warning(
            "Production handoff gatekeeper rejection resolved with action=%s — continuing",
            response.get("action"),
        )
    if not intervention_window("production_start", handoff_checks):
        raise RuntimeError("GATEKEEPER: user halted pipeline at production start")

    # Read VIDEO SLOT durations (narration + following gap) for cross-validation.
    # This is what each video clip must cover — continuous footage with no
    # freeze-frames during narrator pauses.
    narr_durations = get_video_slot_durations(
        tool_context=_MockToolContext(state),
    )

    # AG-UI: emit artifact events as clips are generated
    from agui import get_feedback_store, ArtifactType, ArtifactStatus, ArtifactEvent
    _feedback_store = get_feedback_store()

    video_dir = os.environ.get("VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video")
    total_clips = 0
    skipped_clips = 0
    errors = []
    # Deferred gatekeeper: collect clip info for batch validation AFTER
    # all artifacts are uploaded to B2 and written to OTIO (audit trail).
    _deferred_gk_clips: list[dict] = []

    # Build default negative prompt from visual_style.avoid
    default_negative = ", ".join(visual_style_avoid) if visual_style_avoid else ""

    # Check how many GPU workers are available for parallelism
    worker_urls = os.environ.get("VIDEO_WORKER_URLS", "")
    num_workers = max(1, len([u for u in worker_urls.split(",") if u.strip()])) if worker_urls else 1
    logger.info("Video generation: %d GPU worker(s), %d concepts to generate", num_workers, len(concepts))

    def _generate_one_clip(concept: dict) -> dict | list[dict]:
        """Generate video clip(s) for a concept. Returns list if split needed.

        When a concept has ``needs_split=True`` (duration > LTX cap of 10s),
        we generate multiple sub-clips of ≤10s each and return a list so
        the caller can add them all to the OTIO timeline.
        """
        scene_num = _safe_int(concept.get("scene_num", 0))
        phrase_idx = _safe_int(concept.get("phrase_idx", 0))
        full_duration = float(concept.get("duration", 5.0))

        # Split concepts >10s into multiple sub-clips for LTX-2.3
        if concept.get("needs_split") and full_duration > _LTX_CAP:
            split_count = concept.get("split_count", math.ceil(full_duration / _LTX_CAP))
            sub_dur = full_duration / split_count
            logger.info(
                "Splitting scene_%03d_phrase_%03d: %.2fs → %d sub-clips of %.2fs",
                scene_num, phrase_idx, full_duration, split_count, sub_dur,
            )
            sub_results = []
            for sub_idx in range(split_count):
                sub_concept = dict(concept)
                sub_concept["duration"] = round(sub_dur, 2)
                sub_concept["needs_split"] = False  # prevent recursion
                sub_concept["_sub_idx"] = sub_idx
                sub_concept["_sub_count"] = split_count
                if sub_idx > 0:
                    sub_concept["prompt"] = concept.get("prompt", "") + f" (continuation {sub_idx + 1})"
                sub_results.append(_generate_one_clip(sub_concept))
            return sub_results

        duration = min(full_duration, _LTX_CAP)
        prompt = concept.get("prompt", "")
        lora_id = concept.get("lora_id", "documentary-realism")
        lora_weight = concept.get("lora_weight", 0.75)
        clip_negative = concept.get("negative_prompt", default_negative)
        # Sub-clips from splitting get a suffix to avoid filename collisions
        sub_idx = concept.get("_sub_idx")
        suffix = f"_sub{sub_idx:02d}" if sub_idx is not None else ""
        output_path = os.path.join(video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}{suffix}.mp4")

        # Skip already-generated clips (resume support)
        status_path = os.path.join(video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}{suffix}_status.json")
        if os.path.exists(output_path) and os.path.exists(status_path):
            try:
                with open(status_path) as sf:
                    prev_status = json.load(sf)
                prev_quality = prev_status.get("quality", "unknown")
                if prev_quality in ("good", "excellent", "acceptable", "rejected_accepted"):
                    logger.info(
                        "Skipping scene_%03d_phrase_%03d (already generated, quality=%s)",
                        scene_num, phrase_idx, prev_quality,
                    )
                    skip_result = {"skipped": True, "output_path": output_path, "scene_num": scene_num,
                            "phrase_idx": phrase_idx, "duration": duration, "lora_id": lora_id}
                    if sub_idx is not None:
                        skip_result["_sub_idx"] = sub_idx
                    return skip_result
            except (json.JSONDecodeError, OSError):
                pass  # re-generate if status file is corrupt

        # AG-UI: emit "generating" artifact event
        sub_suffix = f"-sub{sub_idx:02d}" if sub_idx is not None else ""
        artifact_id = f"video-s{scene_num:03d}-p{phrase_idx:03d}{sub_suffix}"
        _feedback_store.register_artifact(ArtifactEvent(
            id=artifact_id,
            artifact_type=ArtifactType.VIDEO_CLIP,
            status=ArtifactStatus.GENERATING,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
            duration_sec=duration,
            metadata={"prompt": prompt, "lora_id": lora_id},
            timestamp=time.time(),
        ))

        gen_result_json = generate_video_clip(
            prompt=prompt,
            duration_sec=duration,
            lora_id=lora_id,
            lora_weight=lora_weight,
            output_path=output_path,
            negative_prompt=clip_negative,
            visual_style=visual_style_str,
        )
        gen_result = json.loads(gen_result_json)
        gen_result["scene_num"] = scene_num
        gen_result["phrase_idx"] = phrase_idx
        gen_result["duration"] = duration
        gen_result["lora_id"] = lora_id
        gen_result["_output_path"] = output_path
        if sub_idx is not None:
            gen_result["_sub_idx"] = sub_idx

        # AG-UI: update artifact with result
        qa_scores = {}
        if gen_result.get("qa_quality"):
            qa_scores["quality"] = gen_result["qa_quality"]
            qa_scores["reason"] = gen_result.get("qa_reason", "")
        _feedback_store.register_artifact(ArtifactEvent(
            id=artifact_id,
            artifact_type=ArtifactType.VIDEO_CLIP,
            status=ArtifactStatus.PENDING_REVIEW,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
            duration_sec=gen_result.get("actual_duration", duration),
            preview_url=output_path,
            qa_scores=qa_scores,
            metadata={"prompt": prompt, "lora_id": lora_id},
            timestamp=time.time(),
        ))

        return gen_result

    def _collect_result(result: dict | list[dict], dest: list[dict]) -> None:
        """Flatten split results (lists) into the destination list."""
        if isinstance(result, list):
            for r in result:
                _collect_result(r, dest)  # handle nested splits
        else:
            dest.append(result)
            # Record to trace capture for across-run learning
            if _trace_capture and isinstance(result, dict):
                _trace_capture.record_clip_result(
                    clip_id=result.get("clip_id", "unknown"),
                    success=not result.get("error"),
                    gen_time=result.get("gen_time", 0),
                    qa_quality=result.get("qa_quality", ""),
                    qa_reason=result.get("qa_reason", ""),
                    worker_id=result.get("worker_id", "local"),
                    attempt=result.get("qa_attempts", 1),
                    prompt=result.get("prompt", ""),
                    duration_target=result.get("duration_target", 0),
                    duration_actual=result.get("duration_actual", 0),
                )

    # Generate clips — fleet-aware dispatch with work queue when available,
    # falling back to the original ThreadPoolExecutor for single-worker mode.
    results: list[dict] = []

    # Try fleet coordinator first (provides work queue, retry-on-different-worker,
    # cost tracking, and systemic problem detection)
    _fleet_coordinator = None
    try:
        from fleet.coordinator import get_fleet_coordinator
        from fleet.work_queue import QueuedClip as _QueuedClip
        _fleet_coordinator = get_fleet_coordinator()
    except ImportError:
        pass

    # Trace capture for across-run learning
    _trace_capture = None
    try:
        from orchestrator.trace_capture import get_trace_capture
        _trace_capture = get_trace_capture()
        _trace_capture.record_event("production_started", {
            "num_clips": len(concepts),
            "num_workers": num_workers,
            "fleet_mode": _fleet_coordinator is not None,
        })
    except Exception:
        pass

    if _fleet_coordinator is not None:
        # ── Fleet mode: enqueue all clips, generate via coordinator ──
        logger.info(
            "Fleet mode: enqueueing %d clips into work queue", len(concepts),
        )
        queued = []
        for c in concepts:
            queued.append(_QueuedClip(
                clip_id=f"scene_{_safe_int(c.get('scene_num', 0)):03d}_phrase_{_safe_int(c.get('phrase_idx', 0)):03d}",
                scene_num=_safe_int(c.get("scene_num", 0)),
                phrase_idx=_safe_int(c.get("phrase_idx", 0)),
                prompt=c.get("prompt", ""),
                negative_prompt=c.get("negative_prompt", ""),
                duration=c.get("duration", 5.0),
                lora_id=c.get("lora_id", "documentary-realism"),
                lora_weight=c.get("lora_weight", 0.7),
            ))
        _fleet_coordinator.enqueue_clips(queued)

        # Generate clips via ThreadPoolExecutor but report results to coordinator
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_concept = {
                executor.submit(_generate_one_clip, c): c for c in concepts
            }
            for future in as_completed(future_to_concept):
                c = future_to_concept[future]
                clip_id = f"scene_{_safe_int(c.get('scene_num', 0)):03d}_phrase_{_safe_int(c.get('phrase_idx', 0)):03d}"
                try:
                    result = future.result()
                    _collect_result(result, results)
                    # Report success to fleet coordinator
                    _fleet_coordinator.report_completed(
                        clip_id=clip_id,
                        output_path=result.get("_output_path", "") if isinstance(result, dict) else "",
                        gen_time=result.get("gen_time", 0) if isinstance(result, dict) else 0,
                        qa_quality=result.get("qa_quality", "") if isinstance(result, dict) else "",
                        qa_reason=result.get("qa_reason", "") if isinstance(result, dict) else "",
                        worker_id="local",
                    )
                except RuntimeError:
                    # Report failure to fleet coordinator
                    _fleet_coordinator.report_failed(
                        clip_id=clip_id,
                        worker_id="local",
                        error=str(future.exception()) if future.exception() else "RuntimeError",
                        category="video_generation",
                    )
                    raise
                except Exception as e:
                    _fleet_coordinator.report_failed(
                        clip_id=clip_id,
                        worker_id="local",
                        error=str(e),
                        category="video_generation",
                    )
                    err_msg = f"Error producing scene {c.get('scene_num')} phrase {c.get('phrase_idx')}: {e}"
                    logger.error(err_msg)
                    errors.append(err_msg)
    elif num_workers > 1:
        # ── Legacy parallel mode (no fleet coordinator) ──
        logger.info("Parallel video generation: %d workers, %d clips", num_workers, len(concepts))
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_concept = {
                executor.submit(_generate_one_clip, c): c for c in concepts
            }
            for future in as_completed(future_to_concept):
                try:
                    _collect_result(future.result(), results)
                except RuntimeError:
                    raise  # Video generation failures are fatal — never swallow
                except Exception as e:
                    c = future_to_concept[future]
                    err_msg = f"Error producing scene {c.get('scene_num')} phrase {c.get('phrase_idx')}: {e}"
                    logger.error(err_msg)
                    errors.append(err_msg)
    else:
        # Sequential fallback (single worker)
        for concept in concepts:
            try:
                _collect_result(_generate_one_clip(concept), results)
            except RuntimeError:
                raise  # Video generation failures are fatal — never swallow
            except Exception as e:
                err_msg = f"Error producing scene {concept.get('scene_num')} phrase {concept.get('phrase_idx')}: {e}"
                logger.error(err_msg)
                errors.append(err_msg)

    # Process results: probe + add to OTIO timeline (must be sequential for OTIO)
    for result in sorted(results, key=lambda r: (r.get("scene_num", 0), r.get("phrase_idx", 0), r.get("_sub_idx", -1))):
        scene_num = result.get("scene_num", 0)
        phrase_idx = result.get("phrase_idx", 0)
        duration = result.get("duration", 5.0)
        lora_id = result.get("lora_id", "documentary-realism")
        output_path = result.get("_output_path") or result.get("output_path", "")

        if result.get("skipped"):
            # Still need to add skipped clips to OTIO timeline
            try:
                probe_result_json = probe_clip(mp4_path=output_path)
                probe_result = json.loads(probe_result_json)
                actual_duration = probe_result.get("duration", duration * 1.15)
                skip_sub_idx = result.get("_sub_idx")
                clip_result_json = add_video_clip(
                    scene_num=scene_num,
                    phrase_idx=phrase_idx,
                    mp4_path=output_path,
                    duration=duration,
                    source_range=duration,
                    available_range=actual_duration,
                    lora_id=lora_id,
                    sub_idx=skip_sub_idx,
                    tool_context=_MockToolContext(state),
                )
                clip_result = json.loads(clip_result_json)
                if "error" in clip_result:
                    from recovery import escalate_pipeline_error
                    _clip_msg = (
                        f"OTIO VIOLATION: failed to add video clip "
                        f"scene {scene_num} phrase {phrase_idx}: {clip_result['error']}"
                    )
                    escalate_pipeline_error(
                        operation_name="production_video_clip",
                        error_msg=_clip_msg,
                        severity="critical",
                        default_action="abort",
                        agent_policy_type="otio",
                    )
                    raise RuntimeError(_clip_msg)
                skipped_clips += 1
                total_clips += 1
            except RuntimeError:
                raise  # OTIO violations are fatal — never swallow
            except Exception as e:
                err_msg = f"Error adding skipped scene {scene_num} phrase {phrase_idx} to timeline: {e}"
                logger.error(err_msg)
                errors.append(err_msg)
            continue

        if result.get("status") == "error":
            errors.append(f"scene_{scene_num}_phrase_{phrase_idx}: {result.get('error')}")
            continue

        try:
            probe_result_json = probe_clip(mp4_path=output_path)
            probe_result = json.loads(probe_result_json)
            actual_duration = probe_result.get("duration", duration * 1.15)

            # ── OTIO CONTRACT VALIDATION ──────────────────────────────
            # The produced clip's actual duration must be >= the OTIO
            # source_range (the narration duration it must cover).
            # If it's shorter, it violates the contract.  We log a
            # warning but still add it — the gatekeeper will catch
            # hard violations.  The clip's available_range records the
            # actual duration so the assembler can trim correctly.
            _DURATION_TOLERANCE = 0.5  # seconds
            if actual_duration < (duration - _DURATION_TOLERANCE):
                logger.warning(
                    "OTIO CONTRACT WARNING: scene %d phrase %d produced %.2fs "
                    "but source_range requires %.2fs (deficit=%.2fs)",
                    scene_num, phrase_idx, actual_duration, duration,
                    duration - actual_duration,
                )

            # B2 upload already happened inside generate_video_clip().
            # Gatekeeper runs AFTER all clips are in B2 + OTIO (audit trail).
            # For sub-clips (from split concepts), use the sub-clip's own
            # duration as expected_dur — NOT the full phrase duration.
            result_sub_idx = result.get("_sub_idx")
            if result_sub_idx is not None:
                # Sub-clip: expected duration is the sub-clip's own duration
                expected_dur = duration
            else:
                scene_phrases = narr_durations.get(scene_num, [])
                expected_dur = scene_phrases[phrase_idx][1] if phrase_idx < len(scene_phrases) else duration
            _deferred_gk_clips.append({
                "mp4_path": output_path,
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "source_range": duration,
                "expected_duration": expected_dur,
            })

            clip_result_json = add_video_clip(
                scene_num=scene_num,
                phrase_idx=phrase_idx,
                mp4_path=output_path,
                duration=duration,
                source_range=duration,
                available_range=actual_duration,
                lora_id=lora_id,
                sub_idx=result_sub_idx,
                tool_context=_MockToolContext(state),
            )
            clip_result = json.loads(clip_result_json)
            if "error" in clip_result:
                from recovery import escalate_pipeline_error
                _clip_msg = (
                    f"OTIO VIOLATION: failed to add video clip "
                    f"scene {scene_num} phrase {phrase_idx}: {clip_result['error']}"
                )
                escalate_pipeline_error(
                    operation_name="production_video_clip",
                    error_msg=_clip_msg,
                    severity="critical",
                    default_action="abort",
                )
                raise RuntimeError(_clip_msg)
            total_clips += 1
        except RuntimeError:
            raise  # OTIO violations are fatal — never swallow
        except Exception as e:
            err_msg = f"Error adding scene {scene_num} phrase {phrase_idx} to timeline: {e}"
            logger.error(err_msg)
            errors.append(err_msg)

    # Upload production artifacts to B2 — artifacts FIRST, then gatekeeper, then stage marker.
    from tools.b2_checkpoint import upload_stage_marker, upload_pipeline_state, upload_timeline, upload_gatekeeper_report
    _b2_ok = upload_pipeline_state(safe_state_dict(state))
    tp = state.get("_timeline_path", "")
    if tp and os.path.exists(tp):
        upload_timeline(tp)
    # NOTE: stage marker is uploaded AFTER gatekeeper validation below.
    # Uploading it here would let a rejected stage be skipped on restart.

    # GATEKEEPER: batch validation AFTER all artifacts are in B2.
    # Every video clip is already uploaded (inside generate_video_clip)
    # and written to OTIO.  Now validate and upload the audit report.
    all_gk_checks = []
    for clip_info in _deferred_gk_clips:
        gk_checks = check_video_clip(
            mp4_path=clip_info["mp4_path"],
            scene_num=clip_info["scene_num"],
            phrase_idx=clip_info["phrase_idx"],
            source_range=clip_info["source_range"],
            expected_duration=clip_info["expected_duration"],
            stage="production",
        )
        all_gk_checks.extend(gk_checks)

    # Upload gatekeeper audit report to B2 (audit trail)
    if all_gk_checks:
        audit_report = format_audit_report(all_gk_checks, "production")
        upload_gatekeeper_report(audit_report, "production")

    # NOW evaluate rejects — everything is safely in B2
    _gk_rejected = False
    if has_rejects(all_gk_checks):
        _gk_rejected = True
        rejects = [c for c in all_gk_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        from recovery import escalate_pipeline_error
        response = escalate_pipeline_error(
            operation_name="production_gatekeeper",
            error_msg=(
                f"GATEKEEPER REJECT (production stage, {len(rejects)} reject(s) — "
                f"audit report uploaded to B2): {reject_msgs}"
            ),
            severity="critical",
            default_action="abort",
            diagnosis_hint="Video clips failed quality checks after production.",
            agent_policy_type="production",
            pipeline_state=safe_state_dict(state),
            diagnostic_data={
                "rejects": [{"message": c.message, "verdict": c.verdict.value} for c in rejects],
                "total_checks": len(all_gk_checks),
            },
        )
        if response.get("action") not in ("skip", "retry_with_fix", "amend"):
            raise RuntimeError(
                f"GATEKEEPER REJECT (production stage, {len(rejects)} reject(s) — "
                f"audit report uploaded to B2): {reject_msgs}"
            )
        logger.warning(
            "Production gatekeeper rejection resolved with action=%s — continuing",
            response.get("action"),
        )

    # Stage marker AFTER gatekeeper passes — rejected stages must NOT be
    # marked complete, otherwise they'd be skipped on pipeline restart.
    if _b2_ok and not _gk_rejected:
        upload_stage_marker("production")

    summary_parts = [
        f"Production complete: {total_clips} video clips generated and added to timeline.",
    ]
    if skipped_clips:
        summary_parts.append(f"Skipped {skipped_clips} already-generated clips (resume).")
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:3])}")
    summary_parts.append(f"Workers used: {num_workers}.")

    logger.info("Deterministic production: %d clips generated", total_clips)

    # TIMELINE GUARDIAN: run explicitly after production.
    # When before_agent_callback returns Content (which we always do for
    # deterministic production), ADK skips the after_agent_callback.
    # The timeline guardian MUST run after production — it's non-negotiable.
    from callbacks.timeline_guardian import timeline_guardian_callback
    try:
        timeline_guardian_callback(callback_context)
        logger.info("Timeline Guardian passed after production")
    except RuntimeError as e:
        # Guardian found violations — this is fatal
        logger.error("Timeline Guardian FAILED after production: %s", e)
        raise

    # INFRA: notify stage complete
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_complete("production")

    # APPROVAL GATE: mark clips ready for human review.
    # When before_agent_callback returns Content (which we always do for
    # deterministic production), ADK skips _production_after_with_gate
    # (pipeline.py:132-142) which is the only other place this is called.
    # Without this, the assembly stage blocks on wait_for_approval("clips")
    # for 2 hours before timing out.
    from callbacks.approval_gate import mark_stage_ready
    mark_stage_ready("clips")
    logger.info("Production: marked clips stage ready for approval")

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text="\n".join(summary_parts))],
    )


# ---------------------------------------------------------------------------
# Deterministic assembly
# ---------------------------------------------------------------------------

def deterministic_assembly_callback(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Before assembler_agent: assemble the final documentary deterministically.

    Reads the OTIO timeline, trims video clips, muxes audio+video per scene,
    and concatenates everything into the final output.

    If the assembly stage was already completed in B2, skip entirely.
    """
    state = callback_context.state
    stages_complete = state.get("_b2_stages_complete", [])
    if "assembly" in stages_complete:
        logger.info("B2: assembly stage already complete, skipping")
        state["pipeline_phase"] = "assembly"
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="Assembly stage restored from B2 checkpoint — skipped.")],
        )

    # OTIO GATE: refuse to proceed if a previous stage flagged a violation
    if state.get("otio_violation"):
        from recovery import escalate_pipeline_error
        _otio_gate_msg = f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
        escalate_pipeline_error(
            operation_name="assembly_otio_gate",
            error_msg=_otio_gate_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="A previous stage flagged an OTIO violation.",
            agent_policy_type="otio",
        )
        raise RuntimeError(_otio_gate_msg)

    # CONTRACT: validate preconditions before starting assembly stage
    from contracts import ASSEMBLY_CONTRACT, validate_preconditions
    validate_preconditions(ASSEMBLY_CONTRACT, safe_state_dict(state))

    # INFRA: notify stage start + check if pipeline is paused
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("assembly")
    check_infra_pause()

    state["pipeline_phase"] = "assembly"

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        _otio_msg = (
            f"OTIO VIOLATION: timeline not found at '{timeline_path}' "
            f"— cannot assemble without OTIO timeline"
        )
        from recovery import escalate_pipeline_error
        escalate_pipeline_error(
            operation_name="assembly_otio_violation",
            error_msg=_otio_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="Timeline file missing — assembly cannot proceed.",
            agent_policy_type="otio",
        )
        raise RuntimeError(_otio_msg)

    import opentimelineio as otio
    from tools.otio_tools import _otio_lock
    from tools.assembly_tools import trim_clip, mux_audio_video, concat_clips
    from tools.video_tools import probe_clip
    from gatekeeper import check_stage_handoff, has_rejects, intervention_window

    # --- Local helper for assembly OTIO violations -----------------------
    # These are structural integrity errors — "skip" is never safe.
    # The escalation is for dashboard visibility and audit trail only.
    def _escalate_otio(msg: str) -> None:
        from recovery import escalate_pipeline_error as _esc
        _esc(
            operation_name="assembly_otio_violation",
            error_msg=msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint=msg[:300],
            agent_policy_type="otio",
        )
        raise RuntimeError(msg)

    # GATEKEEPER: stage handoff check (production → assembly)
    handoff_checks = check_stage_handoff("production", "assembly", safe_state_dict(state))
    if has_rejects(handoff_checks):
        rejects = [c for c in handoff_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        from recovery import escalate_pipeline_error
        response = escalate_pipeline_error(
            operation_name="assembly_handoff_gatekeeper",
            error_msg=f"GATEKEEPER BLOCKED assembly start: {reject_msgs}",
            severity="critical",
            default_action="abort",
            diagnosis_hint="Production stage output failed gatekeeper checks before assembly.",
            agent_policy_type="production",
        )
        if response.get("action") not in ("skip", "retry_with_fix", "amend"):
            raise RuntimeError(
                f"GATEKEEPER BLOCKED assembly start: {reject_msgs}"
            )
        logger.warning(
            "Assembly handoff gatekeeper rejection resolved with action=%s — continuing",
            response.get("action"),
        )
    if not intervention_window("assembly_start", handoff_checks):
        raise RuntimeError("GATEKEEPER: user halted pipeline at assembly start")

    assembly_dir = "/tmp/documentary-pipeline/assembly"
    output_dir = "/tmp/documentary-pipeline/output"
    os.makedirs(assembly_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Read timeline
    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    video_track = None
    narration_track = None
    for track in timeline.tracks:
        if track.name == "V1_Video":
            video_track = track
        elif track.name == "A1_Narration":
            narration_track = track

    if video_track is None or narration_track is None:
        missing = []
        if video_track is None:
            missing.append("V1_Video")
        if narration_track is None:
            missing.append("A1_Narration")
        _escalate_otio(
            f"OTIO VIOLATION: required track(s) missing: {', '.join(missing)} "
            f"— timeline is damaged"
        )

    # Collect video clips by scene
    video_clips_by_scene = {}
    for item in video_track:
        if isinstance(item, otio.schema.Clip):
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            if sn not in video_clips_by_scene:
                video_clips_by_scene[sn] = []
            video_clips_by_scene[sn].append(item)

    # Collect narration clips by scene, split by language in dual mode
    language = state.get("language", "en")
    is_dual = language == "dual_ru_en"

    narration_clips_by_scene = {}  # primary language (RU in dual mode)
    alt_narration_clips_by_scene = {}  # alternate language (EN in dual mode)
    for item in narration_track:
        if isinstance(item, otio.schema.Clip):
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            voice = meta.get("voice", "")

            if is_dual:
                # Route to primary (RU) or alternate (EN) based on voice suffix
                if voice.endswith("_EN"):
                    if sn not in alt_narration_clips_by_scene:
                        alt_narration_clips_by_scene[sn] = []
                    alt_narration_clips_by_scene[sn].append(item)
                else:
                    # _RU clips or clips without suffix go to primary
                    if sn not in narration_clips_by_scene:
                        narration_clips_by_scene[sn] = []
                    narration_clips_by_scene[sn].append(item)
            else:
                if sn not in narration_clips_by_scene:
                    narration_clips_by_scene[sn] = []
                narration_clips_by_scene[sn].append(item)

    # ── PURE OTIO RENDERER ──────────────────────────────────────────
    # The assembler walks the A1_Narration and V1_Video tracks item
    # by item, rendering each OTIO item (Clip or Gap) faithfully:
    #
    #   Clip  → trim media to source_range
    #   Gap   → render as silence (audio) / freeze-frame (video)
    #
    # NO ad-hoc pauses, NO ad-hoc black frames, NO duration
    # calculations.  The OTIO is the immutable contract created
    # during the audio stage.  This assembler merely renders it.
    # ──────────────────────────────────────────────────────────────

    def _render_audio_track(
        narration_track_items: list,
        lang_suffix: str,
    ) -> str:
        """Render the A1_Narration track into a single WAV file.

        Walks every OTIO item in order:
          - Clip → use the WAV file referenced by media_reference
          - Gap  → generate silence of gap duration

        Returns path to the combined audio file.
        """
        audio_segments = []
        for idx, item in enumerate(narration_track_items):
            if isinstance(item, otio.schema.Clip):
                a_path = ""
                if item.media_reference and hasattr(item.media_reference, "target_url"):
                    a_path = item.media_reference.target_url

                if not a_path or not os.path.exists(a_path):
                    _escalate_otio(
                        f"OTIO VIOLATION: narration clip {item.name} references "
                        f"missing file: {a_path}"
                    )
                if not item.source_range:
                    _escalate_otio(
                        f"OTIO VIOLATION: narration clip {item.name} has no "
                        f"source_range — timeline is damaged"
                    )
                audio_segments.append(a_path)

            elif isinstance(item, otio.schema.Gap):
                gap_dur = item.source_range.duration.to_seconds() if item.source_range else 0
                if gap_dur > 0:
                    silence_path = _generate_silence(
                        gap_dur,
                        os.path.join(
                            assembly_dir,
                            f"otio_silence{lang_suffix}_{idx:03d}.wav",
                        ),
                    )
                    if not silence_path:
                        _escalate_otio(
                            f"OTIO VIOLATION: failed to generate silence for "
                            f"gap {item.name} ({gap_dur:.2f}s)"
                        )
                    audio_segments.append(silence_path)

        if not audio_segments:
            _escalate_otio(
                f"OTIO VIOLATION: narration track{lang_suffix} has no renderable items"
            )

        if len(audio_segments) == 1:
            return audio_segments[0]

        combined_audio = os.path.join(
            assembly_dir, f"otio_audio_combined{lang_suffix}.wav",
        )
        concat_result = json.loads(concat_clips(
            clip_paths=",".join(audio_segments),
            output_path=combined_audio,
        ))
        if "error" in concat_result:
            _escalate_otio(
                f"OTIO VIOLATION: audio concat failed: {concat_result['error']}"
            )
        logger.info(
            "Rendered %d audio items into %s",
            len(audio_segments), combined_audio,
        )
        return combined_audio

    def _render_video_track(
        video_track_items: list,
        lang_suffix: str,
    ) -> str:
        """Render the V1_Video track into a single MP4 file.

        Walks every OTIO item in order:
          - Clip → trim to source_range
          - Gap  → render as freeze-frame (hold last frame of
                   preceding clip) for a natural visual hold

        Returns path to the combined video file.
        """
        video_segments = []
        last_clip_path = None  # for freeze-frame generation

        for idx, item in enumerate(video_track_items):
            if isinstance(item, otio.schema.Clip):
                v_path = ""
                if item.media_reference and hasattr(item.media_reference, "target_url"):
                    v_path = item.media_reference.target_url
                if not v_path or not os.path.exists(v_path):
                    _escalate_otio(
                        f"OTIO VIOLATION: video clip {item.name} references "
                        f"missing file: {v_path}"
                    )
                if not item.source_range:
                    _escalate_otio(
                        f"OTIO VIOLATION: video clip {item.name} has no "
                        f"source_range — timeline is damaged"
                    )

                src_start = item.source_range.start_time.to_seconds()
                src_dur = item.source_range.duration.to_seconds()
                if src_dur <= 0:
                    _escalate_otio(
                        f"OTIO VIOLATION: video clip {item.name} has "
                        f"source_range duration={src_dur:.3f}s — must be >0"
                    )

                trimmed_path = os.path.join(
                    assembly_dir,
                    f"otio_vclip{lang_suffix}_{idx:03d}_trimmed.mp4",
                )
                trim_res = json.loads(trim_clip(
                    input_path=v_path,
                    start_sec=src_start,
                    duration_sec=src_dur,
                    output_path=trimmed_path,
                ))
                if "error" in trim_res:
                    _escalate_otio(
                        f"OTIO VIOLATION: failed to trim {item.name} to "
                        f"source_range (start={src_start:.2f}, dur={src_dur:.2f}): "
                        f"{trim_res['error']}"
                    )

                # Post-trim verification
                verify_res = json.loads(probe_clip(mp4_path=trimmed_path))
                actual_dur = verify_res.get("duration", 0)
                if actual_dur <= 0:
                    _escalate_otio(
                        f"OTIO VIOLATION: trimmed clip {trimmed_path} "
                        f"has zero duration after ffprobe verification"
                    )
                if abs(actual_dur - src_dur) > 0.5:
                    _escalate_otio(
                        f"OTIO VIOLATION: trimmed clip {item.name} actual "
                        f"duration ({actual_dur:.2f}s) deviates from OTIO "
                        f"source_range ({src_dur:.2f}s) by "
                        f"{abs(actual_dur - src_dur):.2f}s — trim failed"
                    )

                video_segments.append(trimmed_path)
                last_clip_path = trimmed_path

            elif isinstance(item, otio.schema.Gap):
                gap_dur = item.source_range.duration.to_seconds() if item.source_range else 0
                if gap_dur > 0:
                    gap_meta = item.metadata.get("documentary", {})
                    gap_render_type = gap_meta.get("type", "freeze_frame")

                    gap_video_path = os.path.join(
                        assembly_dir,
                        f"otio_vgap{lang_suffix}_{idx:03d}.mp4",
                    )

                    if gap_render_type == "freeze_frame" and last_clip_path:
                        # Hold the last frame of the preceding clip
                        freeze_path = _generate_freeze_frame_video(
                            last_clip_path, gap_dur, gap_video_path,
                        )
                        if not freeze_path:
                            # Fallback to black if freeze-frame fails
                            freeze_path = _generate_black_video(
                                gap_dur, gap_video_path,
                            )
                        if not freeze_path:
                            _escalate_otio(
                                f"OTIO VIOLATION: failed to generate video gap "
                                f"{item.name} ({gap_dur:.2f}s)"
                            )
                        video_segments.append(freeze_path)
                    else:
                        # No preceding clip or explicit black type
                        black_path = _generate_black_video(
                            gap_dur, gap_video_path,
                        )
                        if not black_path:
                            _escalate_otio(
                                f"OTIO VIOLATION: failed to generate black video "
                                f"gap {item.name} ({gap_dur:.2f}s)"
                            )
                        video_segments.append(black_path)

        if not video_segments:
            _escalate_otio(
                f"OTIO VIOLATION: video track{lang_suffix} has no renderable items"
            )

        if len(video_segments) == 1:
            return video_segments[0]

        combined_video = os.path.join(
            assembly_dir, f"otio_video_combined{lang_suffix}.mp4",
        )
        concat_result = json.loads(concat_clips(
            clip_paths=",".join(video_segments),
            output_path=combined_video,
        ))
        if "error" in concat_result:
            _escalate_otio(
                f"OTIO VIOLATION: video concat failed: {concat_result['error']}"
            )
        logger.info(
            "Rendered %d video items into %s",
            len(video_segments), combined_video,
        )
        return combined_video

    def _assemble_language_track(
        narration_items: list,
        video_items: list,
        lang_suffix: str,
    ) -> tuple:
        """Assemble a single language track by rendering OTIO faithfully.

        This is a PURE OTIO RENDERER — it walks the timeline items in
        order, rendering Clips and Gaps exactly as specified.  No ad-hoc
        pauses, no ad-hoc black frames, no duration adjustments.

        Returns (final_path, errors) tuple.
        """
        track_errors = []
        try:
            combined_audio_raw = _render_audio_track(narration_items, lang_suffix)
            combined_video = _render_video_track(video_items, lang_suffix)

            # Loudness normalization (EBU R128) — different TTS voices
            # produce clips at varying volume levels.  Without normalization,
            # the final documentary has jarring volume shifts between narrators.
            # This was identified as R7 in the deep architecture audit.
            from tools.assembly_tools import normalize_audio_loudness
            normalized_audio_path = os.path.join(
                assembly_dir, f"otio_audio_normalized{lang_suffix}.wav",
            )
            norm_result = json.loads(normalize_audio_loudness(
                input_path=combined_audio_raw,
                output_path=normalized_audio_path,
            ))
            if norm_result.get("status") in ("normalized", "copied_without_normalization"):
                combined_audio = normalized_audio_path
                logger.info(
                    "Audio loudness normalization%s: %s",
                    lang_suffix, norm_result.get("status"),
                )
            else:
                # Normalization failed entirely — use raw audio
                combined_audio = combined_audio_raw
                logger.warning(
                    "Audio loudness normalization%s failed, using raw: %s",
                    lang_suffix, norm_result.get("error", "unknown"),
                )

            # Verify duration alignment (informational — OTIO is truth)
            audio_probe = json.loads(probe_clip(mp4_path=combined_audio))
            video_probe = json.loads(probe_clip(mp4_path=combined_video))
            audio_dur = audio_probe.get("duration", 0)
            video_dur = video_probe.get("duration", 0)
            diff = video_dur - audio_dur

            combined_video_for_mux = combined_video
            if diff > 0.5:
                # Video longer than audio — trim to match
                trimmed = os.path.join(
                    assembly_dir, f"otio_final_vtrim{lang_suffix}.mp4",
                )
                trim_res = json.loads(trim_clip(
                    input_path=combined_video,
                    start_sec=0,
                    duration_sec=audio_dur,
                    output_path=trimmed,
                ))
                if "error" not in trim_res:
                    combined_video_for_mux = trimmed

            elif diff < -0.5:
                logger.warning(
                    "Video (%.2fs) shorter than audio (%.2fs) by %.2fs%s "
                    "— may be due to LTX-2.3 10s cap",
                    video_dur, audio_dur, abs(diff), lang_suffix,
                )

            # Mux audio + video
            muxed_path = os.path.join(
                assembly_dir, f"otio_muxed{lang_suffix}.mp4",
            )
            mux_result = json.loads(mux_audio_video(
                audio_path=combined_audio,
                video_path=combined_video_for_mux,
                output_path=muxed_path,
            ))
            if "error" in mux_result:
                _escalate_otio(
                    f"OTIO VIOLATION: mux failed: {mux_result['error']}"
                )

            return muxed_path, track_errors

        except RuntimeError:
            raise
        except Exception as e:
            track_errors.append(f"Assembly{lang_suffix} error: {e}")
            return "", track_errors

    # ── Render primary language track ────────────────────────────────
    # Collect all items from A1_Narration and V1_Video tracks
    # (already read into narration_track and video_track above).
    # For dual language mode, filter narration items by language.
    primary_narr_items = []
    for item in narration_track:
        if isinstance(item, otio.schema.Clip):
            meta = item.metadata.get("documentary", {})
            voice = meta.get("voice", "")
            if is_dual and voice.endswith("_EN"):
                continue  # skip EN clips for primary (RU) track
            primary_narr_items.append(item)
        elif isinstance(item, otio.schema.Gap):
            primary_narr_items.append(item)

    video_items = list(video_track)  # all items including Gaps

    primary_suffix = "_ru" if is_dual else ""
    primary_path, errors = _assemble_language_track(
        primary_narr_items, video_items, primary_suffix,
    )

    final_name = "final_documentary_ru.mp4" if is_dual else "final_documentary.mp4"
    final_path = os.path.join(output_dir, final_name)
    if primary_path:
        # Simple copy/rename — no ad-hoc pauses or transitions
        import shutil
        shutil.copy2(primary_path, final_path)

    # ── Render alternate language track (EN) in dual mode ────────────
    alt_final_path = ""
    if is_dual and alt_narration_clips_by_scene:
        alt_narr_items = []
        for item in narration_track:
            if isinstance(item, otio.schema.Clip):
                meta = item.metadata.get("documentary", {})
                voice = meta.get("voice", "")
                if voice.endswith("_EN"):
                    alt_narr_items.append(item)
            elif isinstance(item, otio.schema.Gap):
                alt_narr_items.append(item)

        alt_path, alt_errors = _assemble_language_track(
            alt_narr_items, video_items, "_en",
        )
        errors.extend(alt_errors)

        alt_final_path = os.path.join(output_dir, "final_documentary_en.mp4")
        if alt_path:
            import shutil
            shutil.copy2(alt_path, alt_final_path)

    scene_count = len(set(
        item.metadata.get('documentary', {}).get('scene_num', 0)
        for item in video_track
        if isinstance(item, otio.schema.Clip)
    ))
    summary_parts = [
        f"Assembly complete: {scene_count} scenes assembled via pure OTIO renderer.",
    ]
    if os.path.exists(final_path):
        probe_result = json.loads(probe_clip(mp4_path=final_path))
        summary_parts.append(
            f"Primary documentary: {final_path} "
            f"(duration={probe_result.get('duration', 0):.1f}s, "
            f"resolution={probe_result.get('resolution', 'unknown')})"
        )
    if alt_final_path and os.path.exists(alt_final_path):
        alt_probe = json.loads(probe_clip(mp4_path=alt_final_path))
        summary_parts.append(
            f"Alternate (EN) documentary: {alt_final_path} "
            f"(duration={alt_probe.get('duration', 0):.1f}s)"
        )
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:5])}")

    # Upload final outputs + assembly stage marker to B2
    from tools.b2_checkpoint import upload_final_output, upload_stage_marker, upload_pipeline_state
    _b2_ok = True
    if os.path.exists(final_path):
        _b2_ok = upload_final_output(final_path) and _b2_ok
    if alt_final_path and os.path.exists(alt_final_path):
        _b2_ok = upload_final_output(alt_final_path) and _b2_ok
    _b2_ok = upload_pipeline_state(safe_state_dict(state)) and _b2_ok
    # Only mark stage complete if critical artifacts uploaded
    if _b2_ok:
        upload_stage_marker("assembly")

    logger.info("Deterministic assembly: %d scenes, final=%s", scene_count, final_path)

    # INFRA: notify stage complete
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_complete("assembly")

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text="\n".join(summary_parts))],
    )


# ---------------------------------------------------------------------------
# Mock tool context for direct function calls
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Silence generation helper for inter-voice / inter-scene pauses
# ---------------------------------------------------------------------------

def _generate_freeze_frame_video(
    source_clip_path: str,
    duration_sec: float,
    output_path: str,
) -> str:
    """Generate a video that holds the last frame of the source clip.

    This is used for rendering video Gaps in the OTIO timeline — instead
    of a jarring black screen, the viewer sees a natural freeze-frame
    hold of the preceding clip's final frame.

    Returns the output path on success, empty string on failure.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # Step 1: Extract the last frame from the source clip
    last_frame_path = output_path.replace(".mp4", "_lastframe.png")
    extract_cmd = [
        "ffmpeg", "-y",
        "-sseof", "-0.1",  # seek to 0.1s before end
        "-i", source_clip_path,
        "-frames:v", "1",
        "-update", "1",
        last_frame_path,
    ]
    try:
        result = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0 or not os.path.exists(last_frame_path):
            logger.warning("Last-frame extraction failed: %s", result.stderr[:200])
            return ""
    except Exception as e:
        logger.warning("Last-frame extraction error: %s", e)
        return ""

    # Step 2: Create a video from the still frame
    loop_cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", last_frame_path,
        "-c:v", "libx264",
        "-t", str(duration_sec),
        "-pix_fmt", "yuv420p",
        "-r", "24",
        output_path,
    ]
    try:
        result = subprocess.run(loop_cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and os.path.exists(output_path):
            # Clean up temp frame
            try:
                os.remove(last_frame_path)
            except OSError:
                pass
            return output_path
        logger.warning("Freeze-frame video generation failed: %s", result.stderr[:200])
    except Exception as e:
        logger.warning("Freeze-frame video generation error: %s", e)
    return ""


def _generate_silence(duration_sec: float, output_path: str) -> str:
    """Generate a silent WAV file of the given duration using ffmpeg.

    Returns the output path on success, empty string on failure.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=24000:cl=mono:d={duration_sec}",
        "-t", str(duration_sec),
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
        logger.warning("Silence generation failed: %s", result.stderr[:200])
    except Exception as e:
        logger.warning("Silence generation error: %s", e)
    return ""


def _generate_black_video(duration_sec: float, output_path: str, width: int = 512, height: int = 320) -> str:
    """Generate a black video of the given duration for inter-scene transitions.

    Returns the output path on success, empty string on failure.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}:r=24:d={duration_sec}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-t", str(duration_sec),
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
        logger.warning("Black video generation failed: %s", result.stderr[:200])
    except Exception as e:
        logger.warning("Black video generation error: %s", e)
    return ""


class _MockToolContext:
    """Minimal mock of ADK tool_context for direct function calls."""

    def __init__(self, state: dict):
        self.state = state
