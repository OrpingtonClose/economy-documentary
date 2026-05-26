"""Real LLM-backed visual concept tool for the documentary orchestrator.

Mirrors the env-gated overlay pattern established by
:mod:`strands_agents._real_scenario_tools` (slice 9c-LLM-scenario) and
:mod:`strands_agents.playground.pipeline_live_real_workers` (slice
9d-wire). When ``STRANDS_MODEL`` or ``VISUAL_LLM_MODEL_ID`` is set in
the environment, :func:`build_real_visual_tools` produces a
``{tool_name: tool}`` dict that, applied via
:func:`apply_real_visual_overrides`, adds a real-LLM
``propose_visual_concept`` tool to the orchestrator's tool list. The
orchestrator can then call it per scene before
``launch_visual_production`` to construct a rich, style-locked LTX
prompt instead of synthesising one from a sparse hand-built concept
dict.

When no model id resolves the tool builder returns ``{}`` and the
overlay is a no-op, so CI stays hermetic / GPU-free / API-key-free.

This module deliberately does NOT replace any placeholder tool: the
visual concepter is purely additive. The orchestrator calls
``propose_visual_concept`` (when available) to obtain the
``visual_concept`` dict + ``prompt`` string, then passes both to
``launch_visual_production``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.tools import tool  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

_FALLBACK_PLACEHOLDER_REASON = (
    "visual_llm import failed; visual LLM tools disabled, "
    "orchestrator falls back to placeholder visual concept builder"
)


# ---------------------------------------------------------------------------
# Model id resolution
# ---------------------------------------------------------------------------


def _resolve_model_id(model_id: str | None) -> str | None:
    """Return the configured visual-concepter model id, or ``None``.

    Resolution order (first non-empty wins):
    1. The explicit ``model_id`` argument.
    2. The ``VISUAL_LLM_MODEL_ID`` environment variable.
    3. The ``STRANDS_MODEL`` environment variable.

    Empty / whitespace-only strings are treated as unset so a typo in
    a CI env var does not silently flip the overlay on.
    """
    candidates: tuple[str | None, ...] = (
        model_id,
        os.environ.get("VISUAL_LLM_MODEL_ID"),
        os.environ.get("STRANDS_MODEL"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


# ---------------------------------------------------------------------------
# Tool builders
# ---------------------------------------------------------------------------


def _build_propose_concept_tool(model_id: str) -> Any:
    """Build a ``propose_visual_concept`` tool bound to ``model_id``.

    The tool wraps :func:`visual_llm.make_concept_proposer` and
    returns a ``{visual_concept, prompt, scene_id}`` envelope so the
    orchestrator can pass ``prompt`` straight into
    ``launch_visual_production``.

    Args:
        model_id: LiteLLM-compatible model id (already resolved by
            :func:`_resolve_model_id`).

    Returns:
        A LangChain ``@tool``-decorated callable.
    """
    from .visual_llm import make_concept_proposer

    proposer = make_concept_proposer(model_id=model_id)

    @tool
    def propose_visual_concept(
        scene_id: str,
        phrase: dict[str, Any],
        style_lock: dict[str, Any] | None = None,
        visual_style: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Propose one LTX-2.3 visual concept for one scene phrase.

        Calls the configured LLM to produce a structured concept dict
        (shot_type, camera_movement, prompt, negative_prompt,
        duration_sec, ltx_params) plus a ready-to-dispatch ``prompt``
        string. Use the returned ``prompt`` as the ``prompt``
        argument to ``launch_visual_production`` and the returned
        ``visual_concept`` as the ``visual_concept`` argument.

        Args:
            scene_id: Identifier of the scene this concept belongs
                to. Echoed back in the envelope for traceability.
            phrase: One phrase dict from the scenario's
                content-analysis stage (carries ``phrase_id``,
                ``phrase_type``, ``time_span``, ``text``).
            style_lock: Project-wide style lock (dominant_style,
                forbidden_styles, positive_fragment,
                negative_fragment). ``None`` is treated as ``{}``.
            visual_style: Project-wide visual style (style,
                realism_anchors, avoid, palette, camera_language,
                reference_genre). ``None`` is treated as ``{}``.

        Returns:
            ``{"scene_id": ..., "visual_concept": {...},
            "prompt": "..."}``. ``visual_concept`` is the full LLM
            response; ``prompt`` is the LTX-ready string lifted out
            of it so the orchestrator does not need to reach into
            the dict.

        Raises:
            RuntimeError: If the LLM returns invalid JSON or a
                payload missing a required key. The orchestrator
                surfaces this as a stage failure rather than a
                silent placeholder concept.
        """
        logger.info(
            "scene_id=<%s>, phrase_id=<%s>, model_id=<%s> | propose visual concept",
            scene_id,
            phrase.get("phrase_id"),
            model_id,
        )
        concept = proposer(
            phrase,
            style_lock or {},
            visual_style or {},
        )
        prompt_str = concept.get("prompt", "")
        if not isinstance(prompt_str, str) or not prompt_str.strip():
            raise RuntimeError(
                f"propose_visual_concept: LLM returned empty prompt: {concept!r}"
            )
        logger.info(
            "scene_id=<%s>, phrase_id=<%s>, shot_type=<%s>, "
            "camera_movement=<%s> | visual concept proposed",
            scene_id,
            phrase.get("phrase_id"),
            concept.get("shot_type"),
            concept.get("camera_movement"),
        )
        return {
            "scene_id": scene_id,
            "visual_concept": concept,
            "prompt": prompt_str.strip(),
        }

    return propose_visual_concept


def build_real_visual_tools(model_id: str | None = None) -> dict[str, Any]:
    """Build the real-LLM visual tool overlay.

    Returns ``{}`` when no model id resolves (CI / unset env), so the
    orchestrator falls back to constructing visual concepts manually.
    Returns ``{"propose_visual_concept": <tool>}`` when a model id
    resolves.

    Args:
        model_id: Optional explicit model id. When ``None`` the
            resolver consults ``VISUAL_LLM_MODEL_ID`` and
            ``STRANDS_MODEL`` env vars in that order.

    Returns:
        A dict suitable for :func:`apply_real_visual_overrides`.
    """
    resolved = _resolve_model_id(model_id)
    if resolved is None:
        logger.debug(
            "model_id=<%s> | no visual LLM configured, overlay is no-op",
            "<unset>",
        )
        return {}

    try:
        propose = _build_propose_concept_tool(resolved)
    except ImportError as exc:
        logger.warning(
            "error=<%r> | %s",
            exc,
            _FALLBACK_PLACEHOLDER_REASON,
        )
        return {}

    logger.info("model_id=<%s> | building real visual tool overlay", resolved)
    return {
        "propose_visual_concept": propose,
    }


def apply_real_visual_overrides(
    base_tools: list[Any],
    overrides: dict[str, Any],
) -> list[Any]:
    """Apply the visual-overlay tools onto an orchestrator tool list.

    The visual overlay is purely additive — there is no placeholder
    ``propose_visual_concept`` to replace. Tools from ``overrides``
    that share a name with an existing base tool replace the base
    tool by ``.name`` (preserving insertion order). Tools from
    ``overrides`` whose name is absent from ``base_tools`` are
    appended at the end.

    Mirrors :func:`_real_scenario_tools.apply_real_scenario_overrides`
    so the two layers compose without tearing the tool list shape.

    Args:
        base_tools: The orchestrator's current tool list (e.g. the
            output of :func:`build_default_tools` or the result of a
            prior overlay).
        overrides: ``{tool_name: tool}`` dict; usually the output of
            :func:`build_real_visual_tools`.

    Returns:
        A new list of tools with overlay applied. The input list is
        not mutated.
    """
    if not overrides:
        return list(base_tools)
    seen: set[str] = set()
    result: list[Any] = []
    for base_tool in base_tools:
        name = getattr(base_tool, "name", None)
        if isinstance(name, str) and name in overrides:
            result.append(overrides[name])
            seen.add(name)
        else:
            result.append(base_tool)
    for name, override in overrides.items():
        if name not in seen:
            result.append(override)
    return result


__all__ = [
    "apply_real_visual_overrides",
    "build_real_visual_tools",
]
