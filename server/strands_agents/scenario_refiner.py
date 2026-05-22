"""Scenario Refiner agent (Strands).

Adjusts ``scenes[]`` narration and per-scene duration targets when the
timing evaluator reports that the movie is out of budget. Ports
``server/agents/scenario_refiner.py`` (ADK ``Agent`` with
before/after-agent callbacks) to a single Strands :class:`Agent` with
four ``@tool`` callables plus a :class:`SkipIfTimingPassed` hook that
turns the refiner into a no-op once timing passes.

Design decisions:

* ``adjust_scene_durations`` and ``validate_pronunciation_hints`` are
  fully deterministic — they preserve structure, so unit tests can
  exercise them directly via ``.__wrapped__(...)``.
* ``tweak_voice_text`` is LLM-backed because rewriting narration to
  hit a target duration requires natural-language reasoning. The tool
  delegates to a module-level helper that production code wires to a
  real LLM and tests monkeypatch with a deterministic fake (mirrors
  the Component 01 pattern in :mod:`strands_agents.scenario_agent`).
* ``persist_refined_scenes`` writes the final ``scenes`` JSON onto
  ``agent.state`` so the orchestrator (or a direct caller) can pick
  them up via :data:`SCENARIO_CONTRACT.produced_state`.
* :class:`SkipIfTimingPassed` cancels every tool call when
  ``state["timing_passed"]`` is truthy — shadow runs and replays
  remain safe even if an orchestrator forgets the pre-check.

See ``docs/strands-migration/components/03-scenario-refiner.md``.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable, Literal

from strands import Agent, ToolContext, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

from contracts import SCENARIO_CONTRACT
from strands_agents.hooks import ContractEnforcer, RevisionTagger, SkipIfTimingPassed

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are the Scenario Refiner for a documentary timing feedback loop.

The upstream timing evaluator reported that the current scenes do not
hit their duration budget. Your job is to adjust the scenes so the
next audio-render iteration will pass the timing gate, while
preserving every structural field (pronunciation_hints, voices[].voice_id,
hook_spec, outro_spec).

You have four tools:

1. adjust_scene_durations(scenes, per_scene_targets)
   Rewrite per-scene target_duration_sec. Use this when the narration
   is already well paced but the per-scene targets are inconsistent
   with the spoken runtime — the audio agent will re-render against
   the new targets.

2. tweak_voice_text(scenes, scene_id, direction, delta_sec)
   Shorten or lengthen the narration in a specific scene. Use this
   when a scene is materially over or under budget and the target is
   correct; prefer adjust_scene_durations otherwise.

3. validate_pronunciation_hints(scenes)
   Confirm every scene retains its pronunciation_hints. Always call
   this after any rewrite.

4. persist_refined_scenes(scenes)
   Call this LAST to commit the refined scenes list.

Hard constraints:

* Never invent new scenes; never drop existing ones.
* Preserve hook_spec on scene 1 and outro_spec on the final scene.
* Preserve voices[].voice_id assignments per scene.
* Never introduce rhetorical questions.
* Target ~2.5 words per second of spoken narration (150 wpm).

Stop only after persist_refined_scenes returns successfully.
"""


# ---------------------------------------------------------------------------
# Helpers registry (LLM-backed text rewriter, test-injectable)
# ---------------------------------------------------------------------------


_TextRewriter = Callable[[str, Literal["shorten", "lengthen"], float], str]
_TEXT_REWRITER: _TextRewriter | None = None


def set_refiner_helpers(*, text_rewriter: _TextRewriter | None = None) -> None:
    """Register a test-time or production text rewriter.

    When no helper is registered :func:`tweak_voice_text` raises
    :class:`ScenarioRefinerHelperNotConfigured` so missing wiring is
    surfaced loudly rather than silently passing text through.

    Args:
        text_rewriter: Callable implementing
            ``(text, direction, delta_sec) -> rewritten_text`` where
            ``direction`` is ``"shorten"`` or ``"lengthen"`` and
            ``delta_sec`` is roughly how many seconds of speech to
            add or remove (assume ~2.5 words / second). Pass ``None``
            to clear the registry.
    """
    global _TEXT_REWRITER
    _TEXT_REWRITER = text_rewriter


def clear_refiner_helpers() -> None:
    """Reset injected helpers. Primarily for test isolation."""
    global _TEXT_REWRITER
    _TEXT_REWRITER = None


class ScenarioRefinerHelperNotConfigured(RuntimeError):
    """Raised when an LLM-backed tool is invoked with no helper wired in."""


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _find_scene_index(scenes: list[dict[str, Any]], scene_id: int) -> int:
    target = int(scene_id)
    for index, scene in enumerate(scenes):
        for key in ("id", "scene_num", "scene_id"):
            # Guard against keys present but set to None so we fall through
            # to the next candidate instead of raising TypeError from int(None).
            # Mirrors _scene_num in audio_tool.py / content_analyst.py.
            if key not in scene or scene[key] is None:
                continue
            try:
                candidate = int(scene[key])
            except (TypeError, ValueError):
                continue
            if candidate == target:
                return index
    raise ValueError(f"scene_id=<{scene_id}> not found in scenes")


def _voice_text(voice: dict[str, Any]) -> str:
    for key in ("text", "line", "narration"):
        value = voice.get(key)
        if isinstance(value, str):
            return value
    return ""


def _set_voice_text(voice: dict[str, Any], text: str) -> None:
    for key in ("text", "line", "narration"):
        if key in voice:
            voice[key] = text
            return
    voice["text"] = text


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def adjust_scene_durations(
    scenes: list[dict[str, Any]],
    per_scene_targets: dict[str, float],
) -> dict[str, Any]:
    """Replace ``target_duration_sec`` per scene while preserving structure.

    Args:
        scenes: The current scenes list. Each entry must carry an ``id``
            (or ``scene_num`` / ``scene_id``) used to match against
            ``per_scene_targets``.
        per_scene_targets: Mapping of ``{scene_id: new_target_seconds}``.
            Scene IDs are accepted as strings or ints; unknown IDs are
            ignored (logged) so the tool is forgiving when a caller
            drops a scene from its planning set.

    Returns:
        A dict ``{"scenes": [...], "updated_scene_ids": [...]}`` where
        ``scenes`` is the mutated list and ``updated_scene_ids`` is the
        deterministic order of scene IDs that had their target changed.

    Raises:
        ValueError: If ``scenes`` is empty or any target is non-positive.
    """
    if not scenes:
        raise ValueError("scenes=<empty> | cannot adjust empty scenes list")
    normalised: dict[int, float] = {}
    for raw_id, raw_target in per_scene_targets.items():
        target = float(raw_target)
        if target <= 0:
            raise ValueError(
                f"scene_id=<{raw_id}>, target=<{target}> | target must be positive"
            )
        normalised[int(raw_id)] = target

    out_scenes = copy.deepcopy(scenes)
    updated: list[int] = []
    for scene in out_scenes:
        scene_id_raw = scene.get("id", scene.get("scene_num", scene.get("scene_id")))
        if scene_id_raw is None:
            continue
        scene_id = int(scene_id_raw)
        if scene_id in normalised:
            scene["target_duration_sec"] = normalised[scene_id]
            updated.append(scene_id)

    if len(updated) != len(normalised):
        missing = sorted(set(normalised) - set(updated))
        logger.warning(
            "missing_scene_ids=<%s> | adjust_scene_durations: unknown scenes, skipped",
            missing,
        )

    return {"scenes": out_scenes, "updated_scene_ids": updated}


@tool
def tweak_voice_text(
    scenes: list[dict[str, Any]],
    scene_id: int,
    direction: Literal["shorten", "lengthen"],
    delta_sec: float,
) -> dict[str, Any]:
    """Rewrite a single scene's narration to adjust spoken runtime.

    Preserves the scene structure (voice order, voice_id, pronunciation
    hints, hook_spec, outro_spec) and only rewrites the ``text`` field
    on each voice block. Delegates the actual rewrite to the injected
    text-rewriter helper.

    Args:
        scenes: The current scenes list.
        scene_id: The 1-based (or 0-based — whichever ``scenes`` uses)
            scene identifier to target.
        direction: ``"shorten"`` or ``"lengthen"``.
        delta_sec: Approximate seconds of speech to add or remove.

    Returns:
        ``{"scenes": [...], "scene_id": scene_id, "changed_voice_count": int}``.

    Raises:
        ScenarioRefinerHelperNotConfigured: If no helper has been
            registered via :func:`set_refiner_helpers`.
        ValueError: On unknown scene_id or non-positive delta.
    """
    if _TEXT_REWRITER is None:
        raise ScenarioRefinerHelperNotConfigured(
            "set_refiner_helpers(text_rewriter=...) must be called before tweak_voice_text"
        )
    if direction not in ("shorten", "lengthen"):
        raise ValueError(f"direction=<{direction}> | must be 'shorten' or 'lengthen'")
    if delta_sec <= 0:
        raise ValueError(f"delta_sec=<{delta_sec}> | must be positive")

    out_scenes = copy.deepcopy(scenes)
    index = _find_scene_index(out_scenes, scene_id)
    scene = out_scenes[index]
    voices = scene.get("voices", [])
    if not voices:
        raise ValueError(f"scene_id=<{scene_id}> | has no voices[] to rewrite")

    per_voice_delta = delta_sec / len(voices)
    changed = 0
    for voice in voices:
        original = _voice_text(voice)
        if not original.strip():
            continue
        rewritten = _TEXT_REWRITER(original, direction, per_voice_delta)
        if rewritten and rewritten != original:
            _set_voice_text(voice, rewritten)
            changed += 1

    return {"scenes": out_scenes, "scene_id": scene_id, "changed_voice_count": changed}


@tool
def validate_pronunciation_hints(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify every scene retains its ``pronunciation_hints`` dict.

    A scene "retains hints" when the key exists (even if empty) — we do
    not require non-empty hints because not every scene mentions an
    acronym. The intent of this check is to catch silent drops during
    refinement, not to force synthesis of hints.

    Args:
        scenes: The current scenes list.

    Returns:
        ``{"ok": bool, "missing_on": [scene_id, ...]}``.
    """
    missing: list[int] = []
    for scene in scenes:
        if "pronunciation_hints" not in scene:
            scene_id = scene.get(
                "id", scene.get("scene_num", scene.get("scene_id", len(missing)))
            )
            missing.append(int(scene_id))
    return {"ok": not missing, "missing_on": missing}


@tool(context=True)
def persist_refined_scenes(
    scenes: list[dict[str, Any]],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Commit the refined ``scenes`` list onto the agent's state blackboard.

    Writes both the structured list and a JSON string form so readers
    that expect either shape (the ADK pipeline persists JSON) can pick
    it up. Also clears ``timing_passed`` to force the next iteration to
    re-render audio and re-evaluate timing.

    Args:
        scenes: The final scenes list to commit.
        tool_context: Framework-injected context; provides
            ``tool_context.agent.state``.

    Returns:
        ``{"scenes": [...], "persisted": True, "scene_count": int}``.
    """
    # Snapshot so the persisted list is independent of the caller's copy
    # and of the returned value — mirrors persist_content_analysis /
    # persist_coherence_report / persist_visual_concepts.
    snapshot = copy.deepcopy(scenes)
    state = tool_context.agent.state
    state.set("scenes", snapshot)
    state.set("scenes_json", json.dumps(snapshot, ensure_ascii=False))
    state.set("timing_passed", False)
    state.set("_audio_needs_regeneration", True)
    logger.info(
        "scene_count=<%d> | scenario-refiner: refined scenes persisted",
        len(snapshot),
    )
    return {"scenes": snapshot, "persisted": True, "scene_count": len(snapshot)}


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------
