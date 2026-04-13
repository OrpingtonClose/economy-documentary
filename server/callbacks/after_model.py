"""
After-model callback -- semaphore release + reasoning capture + dashboard tracking.

Ported from MiroThinker. Adapted for documentary pipeline (no boxed answer
extraction -- documentary pipeline uses OTIO timeline as output).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext

from callbacks.before_model import release_llm_semaphore_if_held
from dashboard import get_active_collector

logger = logging.getLogger(__name__)

_TIMELINE_DIR = os.environ.get("TIMELINE_DIR", "/tmp/documentary-pipeline/timelines")
_SCENES_BACKUP = os.path.join(_TIMELINE_DIR, "_scenes_backup.json")
_VISUAL_STYLE_BACKUP = os.path.join(_TIMELINE_DIR, "_visual_style_backup.json")


def after_model_callback(
    callback_context: CallbackContext, llm_response: Any
) -> Optional[Any]:
    """ADK after_model_callback.

    1. Releases the LLM concurrency semaphore.
    2. Captures reasoning content from thinking models.
    3. Records LLM end in dashboard.
    """
    state = callback_context.state

    # Release LLM concurrency semaphore
    release_llm_semaphore_if_held(state)

    # Extract text and reasoning from LlmResponse.content
    response_text = ""
    reasoning_text = ""
    if llm_response and getattr(llm_response, "content", None):
        content = llm_response.content
        if hasattr(content, "parts") and content.parts:
            for part in content.parts:
                if hasattr(part, "thought") and part.thought:
                    if hasattr(part, "text") and part.text:
                        reasoning_text += part.text
                elif hasattr(part, "thinking") and part.thinking:
                    reasoning_text += str(part.thinking)
                elif hasattr(part, "text") and part.text:
                    response_text += part.text

    if not response_text and not reasoning_text:
        return None

    # -- Reasoning content capture ---------------------------------------------
    if reasoning_text:
        if "reasoning_traces" not in state:
            state["reasoning_traces"] = []
        agent_name = getattr(callback_context, "agent_name", "unknown")
        state["reasoning_traces"].append(
            {
                "agent": agent_name,
                "reasoning": reasoning_text,
                "response_preview": response_text,
            }
        )
        logger.info(
            "Reasoning content captured from %s: %d chars",
            agent_name,
            len(reasoning_text),
        )

    # -- Scene + visual_style capture (scenario_generator only) ---------------
    # ADK output_key only saves the *final* text response.  When the generator
    # outputs scenes and then calls create_timeline, the post-tool response is
    # often empty → output_key silently discards the scenes.  We capture them
    # from every LLM response and persist to disk + state so downstream agents
    # always have them.
    #
    # STREAMING FIX: after_model fires per-chunk during streaming.  Individual
    # chunks rarely contain complete JSON.  We accumulate the full generator
    # text in state["_generator_accumulated_text"] so that downstream callbacks
    # (_save_generator_scenes, clean_scenes_after_scenario) can parse the
    # complete text even when no single chunk contained the full scenes array.
    agent_name = getattr(callback_context, "agent_name", "unknown")
    if agent_name == "scenario_generator" and response_text:
        # Accumulate full generator output across streaming chunks
        prev = state.get("_generator_accumulated_text", "") or ""
        accumulated = prev + response_text
        state["_generator_accumulated_text"] = accumulated

        from callbacks.deterministic_steps import extract_json_array, extract_json_object

        # --- Capture visual_style (JSON object) --------------------------------
        # Try on accumulated text (more likely to contain complete JSON)
        vs_obj = extract_json_object(accumulated)
        if vs_obj and "style" in vs_obj and "avoid" in vs_obj:
            vs_json = json.dumps(vs_obj, ensure_ascii=False)
            state["visual_style"] = vs_json
            os.makedirs(os.path.dirname(_VISUAL_STYLE_BACKUP) or ".", exist_ok=True)
            with open(_VISUAL_STYLE_BACKUP, "w") as f:
                f.write(vs_json)
            logger.info(
                "Captured visual_style from scenario_generator → state + %s (style=%s)",
                _VISUAL_STYLE_BACKUP, vs_obj.get("style", "unknown"),
            )

        # --- Capture scenes (JSON array of objects with scene_num) ---------------
        # Try on accumulated text — complete JSON is only available after enough
        # streaming chunks have been collected.
        scenes = _extract_scenes_array(accumulated)
        if scenes and len(scenes) >= 2:  # At least 2 scenes = plausible
            scenes_json = json.dumps(scenes, ensure_ascii=False)
            # Persist to state immediately (survives within LoopAgent scope)
            state["scenes"] = scenes_json
            # Persist to disk (survives LoopAgent state scoping)
            os.makedirs(os.path.dirname(_SCENES_BACKUP) or ".", exist_ok=True)
            with open(_SCENES_BACKUP, "w") as f:
                f.write(scenes_json)
            logger.info(
                "Captured %d scenes from scenario_generator → state + %s",
                len(scenes), _SCENES_BACKUP,
            )

    # Record LLM end in dashboard
    _c = get_active_collector()
    if _c:
        _c.llm_end(agent_name, 0.0, len(response_text) // 4)

    return None


def _extract_scenes_array(text: str) -> list | None:
    """Extract the scenes JSON array from text that may contain other arrays.

    The LLM response often contains multiple JSON arrays (realism_anchors,
    avoid list inside visual_style object).  This function finds ALL arrays
    and returns the one that looks like scenes — an array of dicts where at
    least one dict has a ``scene_num`` key.
    """
    import re

    if not text or not text.strip():
        return None

    candidates: list[list] = []

    # Strategy 1: Look inside markdown fences first
    fence_pattern = re.compile(r'```(?:json)?\s*\n?(.*?)```', re.DOTALL)
    for match in fence_pattern.finditer(text):
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, list):
                candidates.append(result)
            elif isinstance(result, dict):
                # Check for a "scenes" key inside fenced JSON
                for key in ("scenes",):
                    if key in result and isinstance(result[key], list):
                        candidates.append(result[key])
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 2: Find all [...] blocks in the text
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
                        candidates.append(result)
                except (json.JSONDecodeError, ValueError):
                    pass
                start_idx = None

    # Pick the candidate that looks like a scenes array
    for candidate in candidates:
        if len(candidate) >= 2 and all(isinstance(item, dict) for item in candidate):
            if any("scene_num" in item for item in candidate):
                return candidate

    # Fallback: return the largest array of dicts (likely scenes)
    dict_arrays = [c for c in candidates if len(c) >= 2 and all(isinstance(item, dict) for item in c)]
    if dict_arrays:
        return max(dict_arrays, key=len)

    return None
