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
                "reasoning": reasoning_text[:5000],
                "response_preview": response_text[:500],
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
    agent_name = getattr(callback_context, "agent_name", "unknown")
    if agent_name == "scenario_generator" and response_text:
        from callbacks.deterministic_steps import extract_json_array, extract_json_object

        # --- Capture visual_style (JSON object) --------------------------------
        # The scenario director outputs visual_style as a JSON object.
        # We look for it in the response text and persist to state + disk.
        vs_obj = extract_json_object(response_text)
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

        # --- Capture scenes (JSON array) ---------------------------------------
        scenes = extract_json_array(response_text)
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
