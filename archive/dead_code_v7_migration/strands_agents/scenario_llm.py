"""LLM-backed helpers for the scenario agent.

The Strands :mod:`scenario_agent` exposes four tools, two of which are
LLM-dependent (:func:`generate_scenario`, :func:`refine_scenario`) and
delegate to helpers registered via :func:`set_scenario_helpers`. This
module provides litellm-backed implementations of those helpers so the
playground's ``scenario_task`` can actually produce scenes instead of
returning a replay stub.

litellm is already a transitive dependency of Strands, so no new
package is introduced. The helpers speak strict JSON only — a single
chat-completion call with ``response_format={"type":"json_object"}``
and a tight schema prompt. On invalid JSON or a missing ``scenes``
key the helper raises so the run surfaces as a ``TASK_ERROR`` rather
than a silent empty scenario.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import litellm  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


#: System prompt for the generator. The shape must line up with
#: :func:`tools.scenario_evaluator_checks.run_all_structural_checks`
#: so the scenario agent's ``evaluate_scenario`` tool has a chance of
#: returning a passing rating without a refine pass.
_GENERATE_SYSTEM = """\
You are the scenario generator for a short-form documentary pipeline.
You will be called ONCE to propose the full scenes list plus a single
movie-level visual_style and style_lock.

Return a JSON OBJECT with EXACTLY these top-level keys:
- scenes: list of scene objects; length MUST equal num_scenes.
- visual_style: {style, realism_anchors[], avoid[], palette, camera_language, reference_genre}.
- style_lock: {dominant_style, forbidden_styles[], positive_fragment, negative_fragment}.

Each scene object MUST include:
- scene_num: 1-based integer, contiguous.
- title: short scene title.
- duration_sec: float seconds; SUM across all scenes must be within +/-10% of the user's target.
- narration: spoken script, ~150 words per minute pacing. No rhetorical questions.
- pronunciation_hints: list of {text, ipa} entries for acronyms / brands / proper nouns (may be empty).
- visual_notes: brief description of on-screen imagery, respecting style_lock.dominant_style.
- dopamine_hook: one short phrase.

Scene 1 MUST also include a hook_spec object; the FINAL scene MUST
also include an outro_spec object.

Hard constraints:
- Pick ONE dominant_style for the whole documentary.
- Every visual_notes must respect it.
- No rhetorical questions anywhere in narration.
- Return STRICT JSON only. No markdown fences. No prose outside the object.
"""


_REFINE_SYSTEM = """\
You are the scenario refiner. Input is the current scenes list plus an
evaluator report with issues and suggestions. Return the same scenes
list with values adjusted to address every issue. Preserve scene count
and contiguous scene_num ordering.

Return a JSON OBJECT with EXACTLY one top-level key:
- scenes: the refined scenes list.

Return STRICT JSON only. No markdown fences. No prose outside the object.
"""


def _parse_completion(resp: Any) -> dict[str, Any]:
    """Extract the content string from a litellm response and parse JSON."""
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected litellm response shape: {resp!r}") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"empty LLM response: {content!r}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned non-JSON payload: {content!r}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM returned non-object payload: {parsed!r}")
    return parsed


def make_generator(
    model_id: str,
    extra: dict[str, Any] | None = None,
) -> Callable[[str, int, str, str], dict[str, Any]]:
    """Return a generator helper bound to ``model_id``.

    The returned callable matches the signature
    :func:`strands_agents.scenario_agent.set_scenario_helpers` expects:
    ``(topic, num_scenes, style, language) -> {scenes, visual_style, style_lock}``.
    """
    client_kwargs = dict(extra or {})

    def _generate(
        topic: str,
        num_scenes: int,
        style: str,
        language: str,
    ) -> dict[str, Any]:
        user = (
            f"Topic: {topic}\n"
            f"num_scenes: {num_scenes}\n"
            f"style_hint: {style}\n"
            f"language: {language}\n"
            "Produce the JSON object described in the system prompt."
        )
        logger.debug("scenario_llm generate model=%s topic=%r", model_id, topic)
        resp = litellm.completion(
            model=model_id,
            messages=[
                {"role": "system", "content": _GENERATE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            **client_kwargs,
        )
        parsed = _parse_completion(resp)
        if "scenes" not in parsed:
            raise RuntimeError(f"generator missing 'scenes' key: {parsed!r}")
        return parsed

    return _generate


def make_refiner(
    *,
    model_id: str,
    extra: dict[str, Any] | None = None,
) -> Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]:
    """Return a refiner helper bound to ``model_id``."""
    client_kwargs = dict(extra or {})

    def _refine(
        scenes: list[dict[str, Any]],
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        user = (
            "Current scenes (JSON):\n"
            + json.dumps(scenes, ensure_ascii=False)
            + "\nEvaluator feedback (JSON):\n"
            + json.dumps(feedback, ensure_ascii=False)
            + "\nReturn the refined scenes list."
        )
        logger.debug(
            "scenario_llm refine model=%s scenes=%d issues=%d",
            model_id,
            len(scenes),
            len(feedback.get("issues") or []),
        )
        resp = litellm.completion(
            model=model_id,
            messages=[
                {"role": "system", "content": _REFINE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            **client_kwargs,
        )
        parsed = _parse_completion(resp)
        if "scenes" not in parsed:
            raise RuntimeError(f"refiner missing 'scenes' key: {parsed!r}")
        return parsed

    return _refine
