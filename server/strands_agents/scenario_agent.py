"""Scenario Director agent (Strands).

Single :class:`Agent` with four tools exposing the
``generate -> evaluate -> refine -> create_timeline`` cycle the ADK
``LoopAgent`` implemented with an external graph. Here the LLM decides
when to loop; the agent's system prompt encodes the protocol and a
:class:`SlidingWindowConversationManager` keeps the evaluate/refine
context bounded (see ``docs/strands-migration/components/01-scenario-agent.md``).

Two tools are LLM-backed (:func:`generate_scenario`,
:func:`refine_scenario`) and delegate to module-level helpers that unit
tests can monkeypatch via :func:`set_scenario_helpers`. The remaining
two (:func:`evaluate_scenario`, :func:`create_timeline`) are
deterministic and wrap the existing
``server/tools/scenario_evaluator_checks.py`` +
``server/tools/otio_tools.py`` logic so the scenario flow keeps a single
source of truth for structural rules and OTIO invariants.

Usage::

    from strands_agents.scenario_agent import build_scenario_agent

    agent = build_scenario_agent()
    result = agent("Produce a 7-minute documentary about inflation.")
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

from contracts import SCENARIO_CONTRACT
from strands_agents.hooks import ContractEnforcer, RevisionTagger
from tools import otio_tools
from tools.scenario_evaluator_checks import (
    EvaluatorReport,
    run_all_structural_checks,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are the Scenario Director for a documentary pipeline.

Produce a scenes list (and its companion visual_style + style_lock)
from a topic description. You have four tools:

1. generate_scenario(topic, num_scenes, style, language)
   Call this FIRST. Returns {scenes, visual_style, style_lock}.
2. evaluate_scenario(scenes, style_lock, target_duration_sec)
   Call this after EVERY generate_scenario or refine_scenario.
   Returns {rating, issues, suggestions} where rating is one of
   POOR | FAIR | GOOD | EXCELLENT.
3. refine_scenario(scenes, feedback)
   Call this when rating is POOR or FAIR, or when any issues remain.
   Returns {scenes} with the same cardinality but adjusted values.
4. create_timeline(scenes)
   Call this LAST, once rating is GOOD or EXCELLENT with no remaining
   issues. Returns {timeline_path, total_duration_sec}.

Hard constraints every generation must satisfy:

* Pick ONE dominant_style for the whole documentary.
* Scene 1 emits a hook_spec; the final scene emits an outro_spec.
* Total spoken duration within +/-10 percent of the target.
* No rhetorical questions; SSML <break> tags carry pacing.
* Pronunciation hints travel with every scene that names an acronym,
  brand, or proper noun the TTS is likely to mispronounce.

Stop only after create_timeline returns successfully.
"""


# ---------------------------------------------------------------------------
# Helpers registry (LLM-backed generator + refiner, test-injectable)
# ---------------------------------------------------------------------------


_GENERATOR: Callable[[str, int, str, str], dict[str, Any]] | None = None
_REFINER: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]] | None = None


def set_scenario_helpers(
    *,
    generator: Callable[[str, int, str, str], dict[str, Any]] | None = None,
    refiner: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    """Register test-time or production helpers for the LLM-backed tools.

    When no helper is registered the corresponding tool raises
    :class:`ScenarioHelperNotConfigured` so missing wiring shows up
    loudly rather than silently returning a stub.

    Args:
        generator: Callable implementing ``(topic, num_scenes, style, language)
            -> {"scenes": [...], "visual_style": {...}, "style_lock": {...}}``.
        refiner: Callable implementing ``(scenes, feedback) -> {"scenes": [...]}``.
    """
    global _GENERATOR, _REFINER
    if generator is not None:
        _GENERATOR = generator
    if refiner is not None:
        _REFINER = refiner


def clear_scenario_helpers() -> None:
    """Reset injected helpers. Primarily for test isolation."""
    global _GENERATOR, _REFINER
    _GENERATOR = None
    _REFINER = None


class ScenarioHelperNotConfigured(RuntimeError):
    """Raised when an LLM-backed tool is invoked with no helper wired in."""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def generate_scenario(
    topic: str,
    num_scenes: int,
    style: str,
    language: str,
) -> dict[str, Any]:
    """Produce the initial scenario — scenes, visual_style, style_lock.

    Delegates to the injected generator helper. The top-level agent's
    system prompt dictates the scene schema; the helper is expected to
    call an LLM or equivalent model and return the parsed JSON.

    Args:
        topic: Documentary subject.
        num_scenes: Target scene count (hard bound, not a hint).
        style: Dominant style descriptor (e.g.
            ``"cinematic_documentary"``, ``"hand_drawn_animation"``).
        language: IETF language tag (e.g. ``"en-US"``).

    Returns:
        Mapping with ``scenes``, ``visual_style``, ``style_lock``.

    Raises:
        ScenarioHelperNotConfigured: When no generator helper is wired.
    """
    if _GENERATOR is None:
        raise ScenarioHelperNotConfigured(
            "generator helper not configured; call set_scenario_helpers"
        )
    logger.debug(
        "topic=<%s>, num_scenes=<%d>, style=<%s>, language=<%s> | generating scenario",
        topic,
        num_scenes,
        style,
        language,
    )
    return _GENERATOR(topic, num_scenes, style, language)


@tool
def evaluate_scenario(
    scenes: list[dict[str, Any]],
    style_lock: dict[str, Any],
    target_duration_sec: float,
) -> dict[str, Any]:
    """Run the deterministic structural checks on ``scenes``.

    Wraps :func:`tools.scenario_evaluator_checks.run_all_structural_checks`
    and packages the verdict cap + failing-check details into the shape
    the orchestrating LLM consumes.

    Args:
        scenes: The candidate scene list to evaluate.
        style_lock: The style-lock dict the scenes were generated under.
        target_duration_sec: The user-requested total documentary length.
            Used for the duration-target structural check.

    Returns:
        ``{"rating": "EXCELLENT|GOOD|FAIR|POOR",
           "issues": [...],
           "suggestions": [...]}``.
    """
    scenario = {"scenes": scenes, "style_lock": style_lock}
    report: EvaluatorReport = run_all_structural_checks(
        scenario,
        target_duration_sec=target_duration_sec,
    )
    issues = [r.as_dict() for r in report.results if not r.passed]
    suggestions = [r.details for r in report.results if not r.passed and r.details]
    logger.debug(
        "rating=<%s>, issues=<%d> | evaluated scenario",
        report.overall,
        len(issues),
    )
    return {
        "rating": report.overall,
        "issues": issues,
        "suggestions": suggestions,
    }


@tool
def refine_scenario(
    scenes: list[dict[str, Any]],
    feedback: dict[str, Any],
) -> dict[str, Any]:
    """Adjust ``scenes`` in response to evaluator feedback.

    Delegates to the injected refiner helper. The helper preserves
    scene cardinality and field schema; it only changes values.

    Args:
        scenes: Current scene list to refine.
        feedback: Result of :func:`evaluate_scenario` — the refiner
            reads ``issues`` and ``suggestions`` to decide what to edit.

    Returns:
        ``{"scenes": [...]}`` with the same length as ``scenes``.

    Raises:
        ScenarioHelperNotConfigured: When no refiner helper is wired.
    """
    if _REFINER is None:
        raise ScenarioHelperNotConfigured(
            "refiner helper not configured; call set_scenario_helpers"
        )
    logger.debug(
        "scene_count=<%d>, issue_count=<%d> | refining scenario",
        len(scenes),
        len(feedback.get("issues", [])),
    )
    return _REFINER(scenes, feedback)


@tool
def create_timeline(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Produce an OTIO timeline from ``scenes`` and return the file path.

    Delegates to :func:`tools.otio_tools.create_timeline` so the
    scenario flow shares a single implementation of the OTIO track
    layout with the rest of the pipeline. Idempotent on disk when the
    topic-derived path already exists.

    Args:
        scenes: The finalised scene list. Must be non-empty; the topic
            is derived from ``scenes[0]["title"]`` when present.

    Returns:
        ``{"timeline_path": str, "total_duration_sec": float,
           "num_scenes": int}``.
    """
    if not scenes:
        raise ValueError("scenes must be a non-empty list")
    topic = _derive_topic(scenes)
    raw = otio_tools.create_timeline(topic=topic, num_scenes=len(scenes))
    path_info = json.loads(raw) if isinstance(raw, str) else raw
    total_duration = _sum_duration(scenes)
    return {
        "timeline_path": path_info.get("timeline_path", ""),
        "total_duration_sec": total_duration,
        "num_scenes": len(scenes),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_topic(scenes: list[dict[str, Any]]) -> str:
    title = str(scenes[0].get("title") or "documentary")
    # Strip path-unfriendly chars so otio_tools.create_timeline's filename
    # derivation stays well-behaved; the upstream helper does the same
    # sanitisation but we want a predictable topic for logs.
    safe = title.strip() or "documentary"
    return safe[:60]


def _sum_duration(scenes: list[dict[str, Any]]) -> float:
    total = 0.0
    for s in scenes:
        if not isinstance(s, dict):
            continue
        dur = s.get("duration_sec") or s.get("duration") or 0.0
        try:
            total += float(dur)
        except (TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_scenario_agent(
    *,
    model: Any = None,
    window_size: int = 20,
    enforce_contract: bool = True,
    tag_revisions: bool = False,
) -> Agent:
    """Return a configured scenario :class:`Agent`.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``. When
            ``None`` the SDK falls through to its default (Bedrock
            Claude). Callers typically pass an ``OpenAIModel`` or a
            model-string like ``"openai/gpt-4o"``.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Twenty covers
            roughly three generate/evaluate/refine cycles without
            dropping the original topic instruction.
        enforce_contract: When True, wire
            :class:`ContractEnforcer` for :data:`SCENARIO_CONTRACT`.
        tag_revisions: When True, wire
            :class:`RevisionTagger` with ``output_key="scenes"``. Off
            by default because the preference ledger must be seeded
            before the agent runs; downstream integrations toggle this
            once the pipeline ledger is wired in.

    Returns:
        Configured :class:`Agent` ready for ``.__call__`` invocations.
    """
    hooks: list[Any] = []
    if enforce_contract:
        hooks.append(ContractEnforcer(SCENARIO_CONTRACT))
    if tag_revisions:
        hooks.append(RevisionTagger("scenes", stage="scenario"))

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            generate_scenario,
            evaluate_scenario,
            refine_scenario,
            create_timeline,
        ],
        conversation_manager=SlidingWindowConversationManager(window_size=window_size),
        hooks=hooks,
        name="scenario_director",
    )
