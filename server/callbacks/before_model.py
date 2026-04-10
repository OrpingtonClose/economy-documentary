"""
Before-model callback -- LLM concurrency gate + context-length safety net.

Ported from MiroThinker. Adapted for the documentary pipeline:
1. LLM concurrency semaphore -- limits parallel LLM calls.
2. Context-length safety net -- forces wrap-up if context is too large.
3. Hard context truncation -- truncates old function responses as fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, List, Optional

from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

from dashboard import get_active_collector

logger = logging.getLogger(__name__)

# -- LLM concurrency gate ------------------------------------------------------
_MAX_CONCURRENT_LLM = int(os.environ.get("MAX_CONCURRENT_LLM", "2"))
_llm_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LLM)


def release_llm_semaphore_if_held(state: dict) -> None:
    """Safety release -- call from after_model or any error-cleanup path."""
    if state.get("_llm_sem_held"):
        _llm_semaphore.release()
        state["_llm_sem_held"] = False


_CHARS_PER_TOKEN = 2.8
_OVERHEAD_TOKENS = int(os.environ.get("CONTEXT_OVERHEAD_TOKENS", "15000"))
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "128000"))
HARD_CONTEXT_LIMIT = int(os.environ.get("HARD_CONTEXT_LIMIT", "160000"))


def _estimate_tokens(contents: List[genai_types.Content]) -> int:
    """Rough token estimate based on total character count."""
    total_chars = 0
    for content in contents:
        if content.parts:
            for part in content.parts:
                if hasattr(part, "text") and part.text:
                    total_chars += len(part.text)
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    total_chars += len(getattr(fc, "name", "") or "")
                    args = getattr(fc, "args", None)
                    if args:
                        total_chars += len(str(args))
                if hasattr(part, "function_response") and part.function_response:
                    fr = part.function_response
                    total_chars += len(getattr(fr, "name", "") or "")
                    resp = getattr(fr, "response", None)
                    if resp:
                        total_chars += len(str(resp))
    return int(total_chars / _CHARS_PER_TOKEN) + _OVERHEAD_TOKENS


async def before_model_callback(
    callback_context: CallbackContext, llm_request: Any
) -> Optional[genai_types.Content]:
    """ADK before_model_callback (async).

    1. Acquires the LLM concurrency semaphore.
    2. Checks context length and signals force_end if too large.
    """
    await _llm_semaphore.acquire()
    callback_context.state["_llm_sem_held"] = True
    logger.debug(
        "LLM semaphore acquired (%d/%d slots used)",
        _MAX_CONCURRENT_LLM - _llm_semaphore._value,
        _MAX_CONCURRENT_LLM,
    )

    contents: Optional[List[genai_types.Content]] = getattr(
        llm_request, "contents", None
    )
    if not contents:
        return None

    state = callback_context.state

    # -- Context length check ---------------------------------------------------
    estimated = _estimate_tokens(contents)

    if estimated > MAX_CONTEXT_TOKENS:
        if not state.get("force_end"):
            state["force_end"] = True
            logger.warning(
                "Context length estimate (%d tokens) exceeds threshold (%d). "
                "Setting force_end=True.",
                estimated,
                MAX_CONTEXT_TOKENS,
            )
            _c = get_active_collector()
            if _c:
                _c.force_end(estimated)

        wrap_up_text = (
            "[SYSTEM] Your context window is nearly full. You MUST produce "
            "your final output NOW without making any more tool calls. "
            "Summarize what you have completed and provide your best result."
        )
        force_end_msg = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=wrap_up_text)],
        )
        contents.append(force_end_msg)

    # -- Hard context truncation ------------------------------------------------
    if estimated > HARD_CONTEXT_LIMIT:
        _truncate_old_responses(contents, HARD_CONTEXT_LIMIT)
        new_est = _estimate_tokens(contents)
        logger.warning(
            "Hard context truncation: %d -> %d estimated tokens (limit %d)",
            estimated,
            new_est,
            HARD_CONTEXT_LIMIT,
        )
        estimated = new_est

    # Record LLM start in dashboard
    _c = get_active_collector()
    if _c:
        agent_name = getattr(callback_context, "agent_name", "")
        _c.llm_start(agent_name, estimated)

    return None


def _truncate_old_responses(
    contents: List[genai_types.Content],
    target_tokens: int,
) -> None:
    """Truncate old function_response parts to fit within *target_tokens*."""
    fr_parts: list[tuple[int, int, int]] = []
    for ci, content in enumerate(contents):
        if not content.parts:
            continue
        for pi, part in enumerate(content.parts):
            if hasattr(part, "function_response") and part.function_response:
                resp = getattr(part.function_response, "response", None)
                char_count = len(str(resp)) if resp else 0
                fr_parts.append((ci, pi, char_count))

    if len(fr_parts) <= 4:
        return

    truncatable = fr_parts[:-4]
    current_est = _estimate_tokens(contents)
    _TRUNCATION_MARKER = "[content truncated to fit context window]"

    for ci, pi, char_count in truncatable:
        if current_est <= target_tokens:
            break
        part = contents[ci].parts[pi]
        fr = part.function_response
        if fr and getattr(fr, "response", None):
            saved_chars = char_count - len(_TRUNCATION_MARKER)
            if saved_chars > 100:
                fr.response = {"result": _TRUNCATION_MARKER}
                current_est -= int(saved_chars / _CHARS_PER_TOKEN)
                logger.debug(
                    "Truncated function_response at content[%d].parts[%d] "
                    "(saved ~%d tokens)",
                    ci,
                    pi,
                    int(saved_chars / _CHARS_PER_TOKEN),
                )
