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
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def extract_json_array(text: str) -> Optional[list]:
    """Extract a JSON array from text that may contain preamble/markdown fences.

    Handles:
    - Pure JSON arrays
    - JSON wrapped in ```json ... ``` fences
    - JSON preceded by preamble text ("I apologize...", "Here are the scenes:")
    - Multiple JSON blocks (returns the first valid array)
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

    # Strategy 2: Extract from markdown code fences
    fence_pattern = re.compile(r'```(?:json)?\s*\n(.*?)```', re.DOTALL)
    for match in fence_pattern.finditer(text):
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, list):
                return result
        except (json.JSONDecodeError, ValueError):
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
                try:
                    result = json.loads(text[start_idx:i + 1])
                    if isinstance(result, list):
                        return result
                except (json.JSONDecodeError, ValueError):
                    start_idx = None
                    continue

    return None


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

    # Strategy 2: Extract from markdown fences
    fence_pattern = re.compile(r'```(?:json)?\s*\n(.*?)```', re.DOTALL)
    for match in fence_pattern.finditer(text):
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
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
                try:
                    result = json.loads(text[start_idx:i + 1])
                    if isinstance(result, dict):
                        return result
                except (json.JSONDecodeError, ValueError):
                    start_idx = None
                    continue

    return None


# ---------------------------------------------------------------------------
# Post-scenario: clean up state["scenes"] to pure JSON
# ---------------------------------------------------------------------------

def clean_scenes_after_scenario(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """After scenario_director: extract clean JSON from state['scenes'].

    The LLM often wraps JSON in preamble text and markdown fences.
    This callback extracts the pure JSON array and stores it back.
    """
    state = callback_context.state
    raw_scenes = state.get("scenes", "")
    if not raw_scenes:
        logger.warning("No scenes in state after scenario director")
        return None

    scenes = extract_json_array(str(raw_scenes))
    if scenes:
        state["scenes"] = json.dumps(scenes)
        logger.info("Cleaned scenes JSON: %d scenes extracted", len(scenes))
    else:
        logger.error("Failed to extract JSON array from scenes state")

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
    """
    state = callback_context.state
    state["pipeline_phase"] = "audio"

    # Parse scenes
    raw_scenes = state.get("scenes", "[]")
    scenes = extract_json_array(str(raw_scenes))
    if not scenes:
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="ERROR: No valid scenes JSON in state")],
        )

    language = state.get("language", "en")
    timeline_path = state.get("_timeline_path", "")

    # Import tool functions directly (not via FunctionTool wrappers)
    from tools.tts_tools import generate_narration
    from tools.whisperx_tools import align_narration
    from tools.otio_tools import add_narration_clip

    alignment_data = {}
    total_clips = 0
    errors = []

    for scene in scenes:
        scene_num = scene.get("scene_num", 0)
        voices = scene.get("voices", [])

        for voice_block in voices:
            voice = voice_block.get("voice", "V1")
            text = voice_block.get("text", "")
            if not text:
                continue

            if language == "dual_ru_en":
                # Split into RU and EN blocks
                for lang_code, lang_tag in [("ru", "[RU]"), ("en", "[EN]")]:
                    # Extract language-specific text
                    lang_text = _extract_lang_text(text, lang_tag)
                    if not lang_text:
                        continue

                    voice_suffix = f"{voice}_{lang_code.upper()}"

                    try:
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
                                err_msg = f"OTIO error scene {scene_num} {voice_suffix}: {clip_result['error']}"
                                logger.error(err_msg)
                                errors.append(err_msg)
                            else:
                                total_clips += 1

                            # Run alignment
                            align_result_json = align_narration(
                                wav_path=wav_path,
                                text=lang_text,
                                language=lang_code,
                            )
                            align_key = f"scene_{scene_num:03d}_{voice_suffix}"
                            alignment_data[align_key] = json.loads(align_result_json)

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
                    )
                    result = json.loads(result_json)
                    wav_path = result.get("wav_path", "")
                    duration = result.get("duration", 0)

                    if wav_path and duration > 0:
                        clip_result_json = add_narration_clip(
                            scene_num=scene_num,
                            voice=voice,
                            wav_path=wav_path,
                            duration=duration,
                            tool_context=_MockToolContext(state),
                        )
                        clip_result = json.loads(clip_result_json)
                        if "error" in clip_result:
                            err_msg = f"OTIO error scene {scene_num} {voice}: {clip_result['error']}"
                            logger.error(err_msg)
                            errors.append(err_msg)
                        else:
                            total_clips += 1

                        align_result_json = align_narration(
                            wav_path=wav_path,
                            text=text,
                            language=lang_code,
                        )
                        align_key = f"scene_{scene_num:03d}_{voice}"
                        alignment_data[align_key] = json.loads(align_result_json)

                except Exception as e:
                    err_msg = f"Error processing scene {scene_num} {voice}: {e}"
                    logger.error(err_msg)
                    errors.append(err_msg)

    # Store alignment data in state
    state["whisperx_alignment"] = json.dumps(alignment_data)

    summary_parts = [
        f"Audio generation complete: {total_clips} narration clips added to timeline.",
        f"Alignment data: {len(alignment_data)} entries.",
    ]
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:3])}")

    logger.info("Deterministic audio: %d clips, %d alignments", total_clips, len(alignment_data))

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text="\n".join(summary_parts))],
    )


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

def write_visual_metadata_to_otio(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """After visual_director: write prompt/LoRA metadata to OTIO gaps.

    The visual director's LLM outputs visual_concepts to state but doesn't
    update the OTIO timeline gaps with the metadata. This callback does that.
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
        # Still run timeline guardian
        from callbacks.timeline_guardian import timeline_guardian_callback
        return timeline_guardian_callback(callback_context)

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        logger.error("Timeline not found at %s", timeline_path)
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
    """
    state = callback_context.state
    state["pipeline_phase"] = "production"

    raw_concepts = state.get("visual_concepts", "")
    concepts = extract_json_array(str(raw_concepts))
    if not concepts:
        obj = extract_json_object(str(raw_concepts))
        if obj and "visual_concepts" in obj:
            concepts = obj["visual_concepts"]

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
                    "duration": scene.get("duration_sec", 30),
                    "prompt": f"Documentary footage: {scene.get('title', 'scene')}. {scene.get('visual_notes', '')}",
                    "lora_id": "documentary-realism",
                    "lora_weight": 0.75,
                })

    if not concepts:
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="ERROR: No visual concepts found")],
        )

    from tools.video_tools import generate_video_clip, probe_clip
    from tools.otio_tools import add_video_clip

    video_dir = os.environ.get("VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video")
    total_clips = 0
    errors = []

    for concept in concepts:
        scene_num = concept.get("scene_num", 0)
        phrase_idx = concept.get("phrase_idx", 0)
        duration = concept.get("duration", 5.0)
        prompt = concept.get("prompt", "")
        lora_id = concept.get("lora_id", "documentary-realism")
        lora_weight = concept.get("lora_weight", 0.75)

        output_path = os.path.join(video_dir, f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}.mp4")

        try:
            # Generate video
            gen_result_json = generate_video_clip(
                prompt=prompt,
                duration_sec=duration,
                lora_id=lora_id,
                lora_weight=lora_weight,
                output_path=output_path,
            )
            gen_result = json.loads(gen_result_json)

            if gen_result.get("status") == "error":
                errors.append(f"scene_{scene_num}_phrase_{phrase_idx}: {gen_result.get('error')}")
                continue

            # Probe clip
            probe_result_json = probe_clip(mp4_path=output_path)
            probe_result = json.loads(probe_result_json)
            actual_duration = probe_result.get("duration", duration * 1.15)

            # Add to OTIO timeline
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
                errors.append(f"OTIO error scene {scene_num} phrase {phrase_idx}: {clip_result['error']}")
            else:
                total_clips += 1

        except Exception as e:
            err_msg = f"Error producing scene {scene_num} phrase {phrase_idx}: {e}"
            logger.error(err_msg)
            errors.append(err_msg)

    summary_parts = [
        f"Production complete: {total_clips} video clips generated and added to timeline.",
    ]
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:3])}")

    logger.info("Deterministic production: %d clips generated", total_clips)

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
    """
    state = callback_context.state
    state["pipeline_phase"] = "assembly"

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="ERROR: Timeline not found")],
        )

    import opentimelineio as otio
    from tools.otio_tools import _otio_lock
    from tools.assembly_tools import trim_clip, mux_audio_video, concat_clips
    from tools.video_tools import probe_clip

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
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="ERROR: Missing V1_Video or A1_Narration track")],
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

    # Collect narration clips by scene
    narration_clips_by_scene = {}
    for item in narration_track:
        if isinstance(item, otio.schema.Clip):
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            if sn not in narration_clips_by_scene:
                narration_clips_by_scene[sn] = []
            narration_clips_by_scene[sn].append(item)

    muxed_paths = []
    errors = []
    scene_nums = sorted(set(list(video_clips_by_scene.keys()) + list(narration_clips_by_scene.keys())))

    for scene_num in scene_nums:
        v_clips = video_clips_by_scene.get(scene_num, [])
        a_clips = narration_clips_by_scene.get(scene_num, [])

        if not v_clips:
            errors.append(f"Scene {scene_num}: no video clips")
            continue
        if not a_clips:
            errors.append(f"Scene {scene_num}: no narration clips")
            continue

        # Get the first video clip's media path
        first_video = v_clips[0]
        video_path = ""
        if first_video.media_reference and hasattr(first_video.media_reference, "target_url"):
            video_path = first_video.media_reference.target_url

        # Get the first narration clip's media path and duration
        first_audio = a_clips[0]
        audio_path = ""
        audio_duration = 0
        if first_audio.media_reference and hasattr(first_audio.media_reference, "target_url"):
            audio_path = first_audio.media_reference.target_url
        if first_audio.source_range:
            audio_duration = first_audio.source_range.duration.to_seconds()

        if not video_path or not audio_path:
            errors.append(f"Scene {scene_num}: missing media paths")
            continue

        try:
            # Trim video to match narration duration
            trimmed_path = os.path.join(assembly_dir, f"scene_{scene_num:03d}_trimmed.mp4")
            trim_result = json.loads(trim_clip(
                input_path=video_path,
                start_sec=0,
                duration_sec=audio_duration,
                output_path=trimmed_path,
            ))
            if "error" in trim_result:
                errors.append(f"Scene {scene_num} trim: {trim_result['error']}")
                continue

            # Mux audio + video
            muxed_path = os.path.join(assembly_dir, f"scene_{scene_num:03d}_muxed.mp4")
            mux_result = json.loads(mux_audio_video(
                audio_path=audio_path,
                video_path=trimmed_path,
                output_path=muxed_path,
            ))
            if "error" in mux_result:
                errors.append(f"Scene {scene_num} mux: {mux_result['error']}")
                continue

            muxed_paths.append(muxed_path)

        except Exception as e:
            errors.append(f"Scene {scene_num} assembly: {e}")

    # Concatenate all muxed scenes
    final_path = os.path.join(output_dir, "final_documentary.mp4")
    if muxed_paths:
        try:
            concat_result = json.loads(concat_clips(
                clip_paths=",".join(muxed_paths),
                output_path=final_path,
            ))
            if "error" in concat_result:
                errors.append(f"Concat: {concat_result['error']}")
        except Exception as e:
            errors.append(f"Concat error: {e}")

    summary_parts = [
        f"Assembly complete: {len(muxed_paths)} scenes assembled.",
    ]
    if os.path.exists(final_path):
        probe_result = json.loads(probe_clip(mp4_path=final_path))
        summary_parts.append(
            f"Final documentary: {final_path} "
            f"(duration={probe_result.get('duration', 0):.1f}s, "
            f"resolution={probe_result.get('resolution', 'unknown')})"
        )
    if errors:
        summary_parts.append(f"Errors: {len(errors)} - {'; '.join(errors[:5])}")

    logger.info("Deterministic assembly: %d scenes, final=%s", len(muxed_paths), final_path)

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text="\n".join(summary_parts))],
    )


# ---------------------------------------------------------------------------
# Mock tool context for direct function calls
# ---------------------------------------------------------------------------

class _MockToolContext:
    """Minimal mock of ADK tool_context for direct function calls."""

    def __init__(self, state: dict):
        self.state = state
