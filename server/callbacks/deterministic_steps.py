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
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

logger = logging.getLogger(__name__)


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
        state["scenes"] = json.dumps(scenes, ensure_ascii=False)
        logger.info("Cleaned scenes JSON: %d scenes extracted", len(scenes))

        # Upload scenario artifacts to B2 immediately
        from tools.b2_checkpoint import upload_scenario, upload_stage_marker, upload_pipeline_state, upload_timeline
        vs_raw = str(state.get("visual_style", ""))
        _b2_ok = upload_scenario(json.dumps(scenes, ensure_ascii=False), vs_raw)
        _b2_ok = upload_pipeline_state(state.to_dict()) and _b2_ok
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
        raise RuntimeError(
            f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
        )

    # CONTRACT: validate preconditions before starting audio stage
    from contracts import AUDIO_CONTRACT, validate_preconditions
    validate_preconditions(AUDIO_CONTRACT, state.to_dict())

    # INFRA: notify stage start + check if pipeline is paused
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("audio")
    check_infra_pause()

    state["pipeline_phase"] = "audio"

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
    from tools.otio_tools import add_narration_clip, add_narration_gap, add_video_gap
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

    alignment_data = {}
    total_clips = 0
    errors = []
    # Deferred gatekeeper: collect clip info for batch validation AFTER
    # all artifacts are uploaded to B2 and written to OTIO (audit trail).
    _deferred_gk_clips: list[dict] = []

    for scene_idx, scene in enumerate(scenes):
        scene_num = scene.get("scene_num", 0)
        voices = scene.get("voices", [])
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
                            # B2 upload already happened inside generate_narration().
                            # Gatekeeper runs AFTER all clips are in B2 + OTIO (audit trail).
                            _deferred_gk_clips.append({
                                "wav_path": wav_path,
                                "scene_num": scene_num,
                                "voice": voice_suffix,
                                "duration": duration,
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
                                raise RuntimeError(
                                    f"OTIO VIOLATION: failed to add narration clip "
                                    f"scene {scene_num} {voice_suffix}: {clip_result['error']}"
                                )
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
                # Single language mode
                lang_code = language if language in ("ru", "en") else "en"
                try:
                    result_json = generate_narration(
                        scene_num=scene_num,
                        voice_role=voice,
                        text=text,
                        language=lang_code,
                    )
                    result = json.loads(result_json)
                    wav_path = result.get("wav_path", "")
                    duration = result.get("duration", 0)

                    if wav_path and duration > 0:
                        # B2 upload already happened inside generate_narration().
                        # Gatekeeper runs AFTER all clips are in B2 + OTIO (audit trail).
                        _deferred_gk_clips.append({
                            "wav_path": wav_path,
                            "scene_num": scene_num,
                            "voice": voice,
                            "duration": duration,
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
                            raise RuntimeError(
                                f"OTIO VIOLATION: failed to add narration clip "
                                f"scene {scene_num} {voice}: {clip_result['error']}"
                            )
                        total_clips += 1

                        align_result_json = align_narration(
                            wav_path=wav_path,
                            text=text,
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
            # Insert immediately after each voice clip (except the last)
            # so the OTIO track order is: V1, gap, V2, gap, V3.
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
                add_video_gap(
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
            add_video_gap(
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
    _b2_ok = upload_pipeline_state(state.to_dict())
    tp = state.get("_timeline_path", "")
    if tp and os.path.exists(tp):
        upload_timeline(tp)
    # NOTE: stage marker is uploaded AFTER gatekeeper validation below.
    # Uploading it here would let a rejected stage be skipped on restart.

    # GATEKEEPER: batch validation AFTER all artifacts are in B2.
    # Every narration clip is already uploaded (inside generate_narration)
    # and written to OTIO.  Now validate and upload the audit report.
    all_gk_checks = []
    for clip_info in _deferred_gk_clips:
        gk_checks = check_narration_clip(
            wav_path=clip_info["wav_path"],
            scene_num=clip_info["scene_num"],
            voice=clip_info["voice"],
            duration=clip_info["duration"],
            stage="audio",
        )
        all_gk_checks.extend(gk_checks)

    # Upload gatekeeper audit report to B2 (audit trail)
    if all_gk_checks:
        audit_report = format_audit_report(all_gk_checks, "audio")
        upload_gatekeeper_report(audit_report, "audio")

    # NOW evaluate rejects — everything is safely in B2
    if has_rejects(all_gk_checks):
        rejects = [c for c in all_gk_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        raise RuntimeError(
            f"GATEKEEPER REJECT (audio stage, {len(rejects)} reject(s) — "
            f"audit report uploaded to B2): {reject_msgs}"
        )

    # Stage marker AFTER gatekeeper passes — rejected stages must NOT be
    # marked complete, otherwise they'd be skipped on pipeline restart.
    if _b2_ok:
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
    """Normalize visual concept durations to match narration timing.

    ARCHITECTURE RULE: The sum of video concept durations for a scene MUST
    equal the sum of narration durations for that scene.  The LLM visual
    director may create concepts with durations based on semantic content
    shifts, which do NOT sum to narration duration.  This function scales
    them proportionally so the total matches while preserving relative
    proportions (the LLM still controls WHERE visual breaks happen).

    Individual concept durations are capped at 10.0s (LTX-2.3 limit).
    When a concept exceeds 10s after scaling, it is split into multiple
    sub-concepts of ≤10s each.

    Args:
        concepts: List of visual concept dicts from the LLM.
        narr_durations: Dict from get_narration_durations_by_scene().

    Returns:
        New list of concepts with normalized durations.
    """
    # Group concepts by scene
    by_scene: dict[int, list[dict]] = {}
    for c in concepts:
        sn = c.get("scene_num", 0)
        by_scene.setdefault(sn, []).append(c)

    _LTX_CAP = 10.0
    normalized: list[dict] = []

    for sn, scene_concepts in sorted(by_scene.items()):
        scene_phrases = narr_durations.get(sn, [])
        if not scene_phrases:
            # No narration data — keep concepts as-is (fallback)
            normalized.extend(scene_concepts)
            continue

        target_dur = sum(dur for _, dur in scene_phrases)
        current_dur = sum(c.get("duration", 5.0) for c in scene_concepts)

        if current_dur <= 0 or target_dur <= 0:
            normalized.extend(scene_concepts)
            continue

        scale = target_dur / current_dur

        scene_normalized: list[dict] = []
        for c in scene_concepts:
            c_copy = dict(c)
            raw_dur = c_copy.get("duration", 5.0) * scale

            if raw_dur <= _LTX_CAP:
                c_copy["duration"] = round(raw_dur, 2)
                c_copy["end_time"] = c_copy.get("start_time", 0) + c_copy["duration"]
                scene_normalized.append(c_copy)
            else:
                # Split into sub-concepts of ≤10s
                remaining = raw_dur
                sub_idx = 0
                while remaining > 0.5:
                    chunk = min(remaining, _LTX_CAP)
                    sub = dict(c_copy)
                    sub["duration"] = round(chunk, 2)
                    if sub_idx > 0:
                        sub["prompt"] = c_copy.get("prompt", "") + f" (continuation {sub_idx + 1})"
                    scene_normalized.append(sub)
                    remaining -= chunk
                    sub_idx += 1

        # Re-index phrase_idx sequentially to avoid collisions after splitting
        for idx, c in enumerate(scene_normalized):
            c["phrase_idx"] = idx
        normalized.extend(scene_normalized)

        # Log normalization
        new_dur = sum(c.get("duration", 0) for c in normalized if c.get("scene_num") == sn)
        logger.info(
            "Scene %d: normalized visual concepts %.2fs → %.2fs (target=%.2fs, scale=%.2f)",
            sn, current_dur, new_dur, target_dur, scale,
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

    # ── ARCHITECTURE: normalize concept durations to match narration ──
    # The LLM controls WHERE visual breaks happen.  This normalizer
    # scales durations so they sum to the narration duration per scene.
    from tools.otio_tools import get_narration_durations_by_scene
    narr_durations = get_narration_durations_by_scene(
        tool_context=_MockToolContext(state),
    )
    if narr_durations:
        concepts = _normalize_concept_durations(concepts, narr_durations)
        # Store normalized concepts back into state so production uses them
        state["visual_concepts"] = json.dumps(concepts)
        logger.info("Normalized %d visual concepts to match narration timing", len(concepts))

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
            scene_prefix = f"scene_{sn:03d}_"
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
            # Build a map of scene_num -> concepts
            scene_concepts = {}
            for concept in concepts:
                sn = concept.get("scene_num", 0)
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
    _b2_ok = upload_pipeline_state(callback_context.state.to_dict()) and _b2_ok
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
        raise RuntimeError(
            f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
        )

    # CONTRACT: validate preconditions before starting production stage
    from contracts import PRODUCTION_CONTRACT, validate_preconditions
    validate_preconditions(PRODUCTION_CONTRACT, state.to_dict())

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

    from tools.video_tools import generate_video_clip, probe_clip
    from tools.otio_tools import add_video_clip, get_narration_durations_by_scene
    from gatekeeper import check_video_clip, check_stage_handoff, has_rejects, intervention_window, format_audit_report

    # GATEKEEPER: stage handoff check (visual_direction → production)
    handoff_checks = check_stage_handoff("visual_direction", "production", state.to_dict())
    if has_rejects(handoff_checks):
        rejects = [c for c in handoff_checks if c.verdict.value == "reject"]
        raise RuntimeError(
            f"GATEKEEPER BLOCKED production start: "
            + "; ".join(c.message for c in rejects)
        )
    if not intervention_window("production_start", handoff_checks):
        raise RuntimeError("GATEKEEPER: user halted pipeline at production start")

    # Read narration durations for gatekeeper cross-validation
    narr_durations = get_narration_durations_by_scene(
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

    def _generate_one_clip(concept: dict) -> dict:
        """Generate a single video clip (thread-safe for parallel execution)."""
        scene_num = concept.get("scene_num", 0)
        phrase_idx = concept.get("phrase_idx", 0)
        duration = min(concept.get("duration", 5.0), 10.0)
        prompt = concept.get("prompt", "")
        lora_id = concept.get("lora_id", "documentary-realism")
        lora_weight = concept.get("lora_weight", 0.75)
        clip_negative = concept.get("negative_prompt", default_negative)
        output_path = os.path.join(video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}.mp4")

        # Skip already-generated clips (resume support)
        status_path = os.path.join(video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}_status.json")
        if os.path.exists(output_path) and os.path.exists(status_path):
            try:
                with open(status_path) as sf:
                    prev_status = json.load(sf)
                prev_quality = prev_status.get("quality", "unknown")
                if prev_quality in ("good", "excellent"):
                    logger.info(
                        "Skipping scene_%03d_phrase_%03d (already generated, quality=%s)",
                        scene_num, phrase_idx, prev_quality,
                    )
                    return {"skipped": True, "output_path": output_path, "scene_num": scene_num,
                            "phrase_idx": phrase_idx, "duration": duration, "lora_id": lora_id}
            except (json.JSONDecodeError, OSError):
                pass  # re-generate if status file is corrupt

        # AG-UI: emit "generating" artifact event
        artifact_id = f"video-s{scene_num:03d}-p{phrase_idx:03d}"
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

    # Generate clips in parallel across available GPU workers
    results = []
    if num_workers > 1:
        logger.info("Parallel video generation: %d workers, %d clips", num_workers, len(concepts))
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_concept = {
                executor.submit(_generate_one_clip, c): c for c in concepts
            }
            for future in as_completed(future_to_concept):
                try:
                    results.append(future.result())
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
                results.append(_generate_one_clip(concept))
            except RuntimeError:
                raise  # Video generation failures are fatal — never swallow
            except Exception as e:
                err_msg = f"Error producing scene {concept.get('scene_num')} phrase {concept.get('phrase_idx')}: {e}"
                logger.error(err_msg)
                errors.append(err_msg)

    # Process results: probe + add to OTIO timeline (must be sequential for OTIO)
    for result in sorted(results, key=lambda r: (r.get("scene_num", 0), r.get("phrase_idx", 0))):
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
                clip_result_json = add_video_clip(
                    scene_num=scene_num,
                    phrase_idx=phrase_idx,
                    mp4_path=output_path,
                    duration=duration,
                    source_range=duration,
                    available_range=actual_duration,
                    lora_id=lora_id,
                    tool_context=_MockToolContext(state),
                )
                clip_result = json.loads(clip_result_json)
                if "error" in clip_result:
                    raise RuntimeError(
                        f"OTIO VIOLATION: failed to add video clip "
                        f"scene {scene_num} phrase {phrase_idx}: {clip_result['error']}"
                    )
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
                tool_context=_MockToolContext(state),
            )
            clip_result = json.loads(clip_result_json)
            if "error" in clip_result:
                raise RuntimeError(
                    f"OTIO VIOLATION: failed to add video clip "
                    f"scene {scene_num} phrase {phrase_idx}: {clip_result['error']}"
                )
            total_clips += 1
        except RuntimeError:
            raise  # OTIO violations are fatal — never swallow
        except Exception as e:
            err_msg = f"Error adding scene {scene_num} phrase {phrase_idx} to timeline: {e}"
            logger.error(err_msg)
            errors.append(err_msg)

    # Upload production artifacts to B2 — artifacts FIRST, then gatekeeper, then stage marker.
    from tools.b2_checkpoint import upload_stage_marker, upload_pipeline_state, upload_timeline, upload_gatekeeper_report
    _b2_ok = upload_pipeline_state(state.to_dict())
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
    if has_rejects(all_gk_checks):
        rejects = [c for c in all_gk_checks if c.verdict.value == "reject"]
        reject_msgs = "; ".join(c.message for c in rejects)
        raise RuntimeError(
            f"GATEKEEPER REJECT (production stage, {len(rejects)} reject(s) — "
            f"audit report uploaded to B2): {reject_msgs}"
        )

    # Stage marker AFTER gatekeeper passes — rejected stages must NOT be
    # marked complete, otherwise they'd be skipped on pipeline restart.
    if _b2_ok:
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
        raise RuntimeError(
            f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
        )

    # CONTRACT: validate preconditions before starting assembly stage
    from contracts import ASSEMBLY_CONTRACT, validate_preconditions
    validate_preconditions(ASSEMBLY_CONTRACT, state.to_dict())

    # INFRA: notify stage start + check if pipeline is paused
    from infra_agent import get_infra_agent, check_infra_pause
    _infra = get_infra_agent()
    if _infra:
        _infra.notify_stage_start("assembly")
    check_infra_pause()

    state["pipeline_phase"] = "assembly"

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        raise RuntimeError(
            f"OTIO VIOLATION: timeline not found at '{timeline_path}' "
            f"— cannot assemble without OTIO timeline"
        )

    import opentimelineio as otio
    from tools.otio_tools import _otio_lock
    from tools.assembly_tools import trim_clip, mux_audio_video, concat_clips
    from tools.video_tools import probe_clip
    from gatekeeper import check_stage_handoff, has_rejects, intervention_window

    # GATEKEEPER: stage handoff check (production → assembly)
    handoff_checks = check_stage_handoff("production", "assembly", state.to_dict())
    if has_rejects(handoff_checks):
        rejects = [c for c in handoff_checks if c.verdict.value == "reject"]
        raise RuntimeError(
            f"GATEKEEPER BLOCKED assembly start: "
            + "; ".join(c.message for c in rejects)
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
        raise RuntimeError(
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
                    raise RuntimeError(
                        f"OTIO VIOLATION: narration clip {item.name} references "
                        f"missing file: {a_path}"
                    )
                if not item.source_range:
                    raise RuntimeError(
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
                        raise RuntimeError(
                            f"OTIO VIOLATION: failed to generate silence for "
                            f"gap {item.name} ({gap_dur:.2f}s)"
                        )
                    audio_segments.append(silence_path)

        if not audio_segments:
            raise RuntimeError(
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
            raise RuntimeError(
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
                    raise RuntimeError(
                        f"OTIO VIOLATION: video clip {item.name} references "
                        f"missing file: {v_path}"
                    )
                if not item.source_range:
                    raise RuntimeError(
                        f"OTIO VIOLATION: video clip {item.name} has no "
                        f"source_range — timeline is damaged"
                    )

                src_start = item.source_range.start_time.to_seconds()
                src_dur = item.source_range.duration.to_seconds()
                if src_dur <= 0:
                    raise RuntimeError(
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
                    raise RuntimeError(
                        f"OTIO VIOLATION: failed to trim {item.name} to "
                        f"source_range (start={src_start:.2f}, dur={src_dur:.2f}): "
                        f"{trim_res['error']}"
                    )

                # Post-trim verification
                verify_res = json.loads(probe_clip(mp4_path=trimmed_path))
                actual_dur = verify_res.get("duration", 0)
                if actual_dur <= 0:
                    raise RuntimeError(
                        f"OTIO VIOLATION: trimmed clip {trimmed_path} "
                        f"has zero duration after ffprobe verification"
                    )
                if abs(actual_dur - src_dur) > 0.5:
                    raise RuntimeError(
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
                            raise RuntimeError(
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
                            raise RuntimeError(
                                f"OTIO VIOLATION: failed to generate black video "
                                f"gap {item.name} ({gap_dur:.2f}s)"
                            )
                        video_segments.append(black_path)

        if not video_segments:
            raise RuntimeError(
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
            raise RuntimeError(
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
            combined_audio = _render_audio_track(narration_items, lang_suffix)
            combined_video = _render_video_track(video_items, lang_suffix)

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
                raise RuntimeError(
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
    _b2_ok = upload_pipeline_state(state.to_dict()) and _b2_ok
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
