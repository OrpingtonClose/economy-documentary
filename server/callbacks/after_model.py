"""
After-model callback -- semaphore release + reasoning capture + dashboard tracking.

Ported from MiroThinker. Adapted for documentary pipeline (no boxed answer
extraction -- documentary pipeline uses OTIO timeline as output).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext

from callbacks.before_model import release_llm_semaphore_if_held
from dashboard import get_active_collector

logger = logging.getLogger(__name__)


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

    # Record LLM end in dashboard
    _c = get_active_collector()
    if _c:
        agent_name = getattr(callback_context, "agent_name", "")
        _c.llm_end(agent_name, 0.0, len(response_text) // 4)

    return None
