"""Real LLM-backed scenario tools — slice 9c-LLM-scenario.

Mirrors :mod:`server.strands_agents.playground.pipeline_live_real_workers`
but for the scenario stage rather than audio/video dispatch. The
production orchestrator (:func:`server.strands_agents.pipeline.build_documentary_orchestrator`)
applies these overrides on top of the placeholder tool set so a
configured ``STRANDS_MODEL`` results in actual scene narration coming
from a real LLM, not a hard-coded ``{"status": "placeholder", ...}``
echo.

Why a separate module instead of importing :mod:`scenario_agent`:
``scenario_agent`` decorates its tools with the **Strands** ``@tool``
decorator, which produces objects incompatible with the LangChain
``BaseTool`` interface that ``deepagents.create_deep_agent`` expects
(see PR #361 trajectory tests). To keep one source of truth on the
prompt + protocol we re-use :func:`scenario_llm.make_generator` /
:func:`scenario_llm.make_refiner` directly — those return plain
callables that work with either decorator. The structural-check and
OTIO logic come from the same shared ``tools/`` modules, so the
deterministic checks stay in lock-step with
:mod:`scenario_agent.evaluate_scenario` /
:mod:`scenario_agent.create_timeline`.

Gate: the override set is empty unless ``model_id`` resolves to a
truthy string (default = ``STRANDS_MODEL`` env var). With no model
configured the orchestrator continues to use placeholders so CI stays
hermetic and credential-free.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_core.tools import tool  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


_FALLBACK_PLACEHOLDER_REASON = (
    "scenario_llm helpers unavailable; "
    "real LLM scenario tools disabled (slice 9c-LLM-scenario gate)"
)


def _resolve_model_id(model_id: str | None) -> str | None:
    """Return the configured scenario LLM model id, or ``None``.

    Order of precedence:
    1. Explicit ``model_id`` argument.
    2. ``SCENARIO_LLM_MODEL_ID`` env var (allows scenario-only override).
    3. ``STRANDS_MODEL`` env var (the orchestrator's own model).

    Empty / whitespace strings are treated as unset.
    """
    candidates = (
        model_id,
        os.environ.get("SCENARIO_LLM_MODEL_ID"),
        os.environ.get("STRANDS_MODEL"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _build_generate_tool(model_id: str) -> Any:
    """Closure factory for the LLM-backed ``generate_scenario`` tool."""
    from .scenario_llm import make_generator

    generator = make_generator(model_id=model_id)

    @tool
    def generate_scenario(
        topic: str,
        num_scenes: int = 5,
        style: str = "documentary",
        language: str = "en",
    ) -> dict[str, Any]:
        """Generate a documentary scenario via real LLM (slice 9c-LLM-scenario).

        Calls :func:`scenario_llm.make_generator`'s closure, which hits
        litellm with the strict-JSON schema prompt and returns
        ``{scenes, visual_style, style_lock}``.

        Args:
            topic: Documentary subject.
            num_scenes: Target scene count.
            style: Dominant style descriptor (e.g. ``"cinematic_documentary"``).
            language: IETF language tag (e.g. ``"en"``, ``"en-US"``).
        """
        logger.info(
            "topic=<%s>, num_scenes=<%d>, style=<%s>, language=<%s>, "
            "model=<%s> | scenario generate (real LLM)",
            topic,
            num_scenes,
            style,
            language,
            model_id,
        )
        result = generator(topic, int(num_scenes), style, language)
        scenes = result.get("scenes") if isinstance(result, dict) else None
        scene_count = len(scenes) if isinstance(scenes, list) else 0
        logger.info(
            "topic=<%s>, scenes=<%d> | scenario generate ok",
            topic,
            scene_count,
        )
        return result

    return generate_scenario


def _build_refine_tool(model_id: str) -> Any:
    """Closure factory for the LLM-backed ``refine_scenario`` tool."""
    from .scenario_llm import make_refiner

    refiner = make_refiner(model_id=model_id)

    @tool
    def refine_scenario(
        scenes: list[dict[str, Any]],
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        """Refine ``scenes`` against evaluator ``feedback`` via real LLM.

        Same contract as the placeholder: returns
        ``{"scenes": [...]}`` with the same length as the input.
        """
        scene_count = len(scenes) if isinstance(scenes, list) else 0
        issue_count = (
            len(feedback.get("issues") or []) if isinstance(feedback, dict) else 0
        )
        logger.info(
            "scenes=<%d>, issues=<%d>, model=<%s> | scenario refine (real LLM)",
            scene_count,
            issue_count,
            model_id,
        )
        result = refiner(scenes, feedback)
        return result

    return refine_scenario


def _build_evaluate_tool() -> Any:
    """Closure factory for the deterministic ``evaluate_scenario`` tool."""
    from tools.scenario_evaluator_checks import (
        EvaluatorReport,
        run_all_structural_checks,
    )

    @tool
    def evaluate_scenario(
        scenes: list[dict[str, Any]],
        style_lock: dict[str, Any] | None = None,
        target_duration_sec: float = 300.0,
    ) -> dict[str, Any]:
        """Run deterministic structural checks on ``scenes``.

        Wraps :func:`tools.scenario_evaluator_checks.run_all_structural_checks`
        so the orchestrator sees the same verdict shape the scenario
        agent uses internally.
        """
        scenario = {"scenes": scenes, "style_lock": style_lock or {}}
        report: EvaluatorReport = run_all_structural_checks(
            scenario,
            target_duration_sec=float(target_duration_sec),
        )
        issues = [r.as_dict() for r in report.results if not r.passed]
        suggestions = [r.details for r in report.results if not r.passed and r.details]
        logger.info(
            "rating=<%s>, issues=<%d>, scenes=<%d> | scenario evaluate",
            report.overall,
            len(issues),
            len(scenes) if isinstance(scenes, list) else 0,
        )
        return {
            "rating": report.overall,
            "issues": issues,
            "suggestions": suggestions,
        }

    return evaluate_scenario


def _build_create_timeline_tool() -> Any:
    """Closure factory for the deterministic ``create_timeline`` tool."""
    from tools import otio_tools  # type: ignore[attr-defined]

    @tool
    def create_timeline(scenes: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist an OTIO timeline for ``scenes`` and return its path."""
        if not scenes:
            raise ValueError("scenes must be a non-empty list")
        title = ""
        if isinstance(scenes[0], dict):
            title = str(scenes[0].get("title") or "documentary").strip()
        topic = (title or "documentary")[:60]
        raw = otio_tools.create_timeline(topic=topic, num_scenes=len(scenes))
        path_info = json.loads(raw) if isinstance(raw, str) else raw
        total = 0.0
        for s in scenes:
            if not isinstance(s, dict):
                continue
            dur = s.get("duration_sec") or s.get("duration") or 0.0
            try:
                total += float(dur)
            except (TypeError, ValueError):
                continue
        return {
            "timeline_path": path_info.get("timeline_path", ""),
            "total_duration_sec": total,
            "num_scenes": len(scenes),
        }

    return create_timeline


def build_real_scenario_tools(
    *,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Return ``{tool_name: tool}`` overrides for real LLM scenario.

    Empty dict means "fall back to placeholders". This is the contract
    the orchestrator relies on so a missing model id silently degrades
    to the pre-9c behaviour without loud failures or partial wiring.

    Args:
        model_id: Litellm model identifier (e.g. ``"openai/gpt-4o"``,
            ``"bedrock/anthropic.claude-3-5-sonnet"``). When ``None``
            falls through to env vars; when no env var is set either,
            returns an empty override set.

    Returns:
        Possibly-empty dict mapping tool names to LangChain ``@tool``
        callables. Caller passes through
        :func:`apply_real_scenario_overrides` to swap placeholders by
        ``.name``.
    """
    resolved = _resolve_model_id(model_id)
    if resolved is None:
        logger.debug("model_id=<unset> | scenario LLM tools disabled")
        return {}

    try:
        gen = _build_generate_tool(resolved)
        ref = _build_refine_tool(resolved)
        ev = _build_evaluate_tool()
        ct = _build_create_timeline_tool()
    except ImportError as exc:
        logger.warning(
            "error=<%r> | %s",
            exc,
            _FALLBACK_PLACEHOLDER_REASON,
        )
        return {}

    overrides = {
        "generate_scenario": gen,
        "refine_scenario": ref,
        "evaluate_scenario": ev,
        "create_timeline": ct,
    }
    logger.info(
        "model_id=<%s>, overrides=<%s> | real scenario tools built",
        resolved,
        sorted(overrides.keys()),
    )
    return overrides


def apply_real_scenario_overrides(
    base_tools: list[Any],
    overrides: dict[str, Any],
) -> list[Any]:
    """Replace placeholder scenario tools by ``.name`` match.

    Mirrors
    :func:`server.strands_agents.playground.pipeline_live_real_workers.apply_real_worker_overrides`.
    Passes non-matching tools through unchanged. Preserves order so
    the orchestrator's tool-picking heuristics see a stable surface.

    When the override set includes ``create_timeline`` and the base
    list does not yet expose it (placeholder set has no
    ``create_timeline``), the override is appended at the end. This
    keeps the override set complete without forcing ``_placeholders``
    to grow a new tool just for the gate.
    """
    if not overrides:
        return list(base_tools)
    out: list[Any] = []
    matched: set[str] = set()
    for tool_obj in base_tools:
        name = getattr(tool_obj, "name", None)
        if name in overrides:
            out.append(overrides[name])
            matched.add(name)
        else:
            out.append(tool_obj)
    for name, tool_obj in overrides.items():
        if name not in matched:
            out.append(tool_obj)
    return out


__all__ = [
    "apply_real_scenario_overrides",
    "build_real_scenario_tools",
]
