"""LLM-backed helper for visual concept proposals.

The Strands :mod:`visual_concepter` agent exposes a ``propose_concept``
tool that delegates to a helper registered via
:func:`set_visual_concepter_helpers`. This module provides a litellm-
backed implementation of that helper so the documentary orchestrator
can produce real per-scene cinematography (shot_type, camera_movement,
mood, palette, prompt, negative_prompt) instead of a placeholder
caption.

litellm is already a transitive dependency of Strands, so no new
package is introduced. The helper speaks strict JSON only — a single
chat-completion call with ``response_format={"type":"json_object"}``
and a tight schema prompt. On invalid JSON or a missing required key
the helper raises so the run surfaces as a hard error rather than a
silent placeholder concept.

The schema mirrors the contract of
:func:`strands_agents.visual_concepter.propose_concept`: the returned
dict carries ``shot_type`` (from
:data:`strands_agents.visual_concepter.SHOT_TYPES`),
``camera_movement`` (from
:data:`strands_agents.visual_concepter.CAMERA_MOVEMENTS`),
``prompt`` (the LTX-2.3 prompt text), ``negative_prompt``,
``duration_sec`` (clamped to [1.0, 10.0]), and ``ltx_params`` (steps,
resolution, seed). The downstream tool then layers
``style_lock.dominant_style`` / ``positive_fragment`` onto the prompt
deterministically and stamps ``phrase_id`` / ``scene_id`` from the
input phrase.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import litellm

logger = logging.getLogger(__name__)


#: System prompt for the proposer. The shape lines up with
#: :func:`strands_agents.visual_concepter.propose_concept`'s expected
#: helper return value: a single concept dict for a single phrase.
_PROPOSE_SYSTEM = """\
You are the Visual Concepter for a short-form documentary pipeline.
Given ONE phrase plus the project-wide style_lock and visual_style,
propose ONE LTX-2.3 shot concept that will render as a 1-10 second
video clip.

Return a JSON OBJECT with EXACTLY these top-level keys:
- shot_type: one of [extreme_close_up, close_up, medium_close_up,
  medium, medium_wide, wide, extreme_wide, establishing, detail,
  macro, aerial, over_shoulder, two_shot, cutaway, insert].
- camera_movement: one of [locked, tripod_locked, dolly_in, dolly_out,
  crane_up, crane_down, pan_left, pan_right, truck_left, truck_right,
  orbit, handheld, graphic_overlay].
- prompt: a single rich LTX-2.3 prompt string. Cinematic,
  photographic, evocative. 1-3 sentences. Mention concrete subjects,
  composition, lighting, time-of-day, palette tones. Honor
  style_lock.dominant_style. Include the
  style_lock.positive_fragment somewhere if provided.
- negative_prompt: a comma-separated string of tokens to avoid. Always
  include style_lock.forbidden_styles if provided.
- duration_sec: float seconds, clamped to [1.0, 10.0]. Match the
  phrase's time_span if it has one.
- ltx_params: {steps: 30, resolution: [1280, 720], seed: null}. Keep
  these defaults unless the phrase explicitly demands otherwise.

Hard constraints:
- phrase_type == "data" phrases MUST use a data-suitable shot_type
  (insert, cutaway, detail, macro, aerial) and a camera_movement of
  "graphic_overlay" or "locked".
- Do NOT mention any token from style_lock.forbidden_styles in the
  prompt.
- Return STRICT JSON only. No markdown fences. No prose outside the
  object.
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


_REQUIRED_KEYS: tuple[str, ...] = (
    "shot_type",
    "camera_movement",
    "prompt",
    "negative_prompt",
    "duration_sec",
)


def make_concept_proposer(
    *,
    model_id: str,
    extra: dict[str, Any] | None = None,
) -> Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """Return a concept-proposer helper bound to ``model_id``.

    The returned callable matches the signature
    :func:`strands_agents.visual_concepter.set_visual_concepter_helpers`
    expects: ``(phrase, style_lock, visual_style) -> concept``. The
    ``concept`` dict carries ``shot_type``, ``camera_movement``,
    ``prompt``, ``negative_prompt``, ``duration_sec``, and
    ``ltx_params``. ``phrase_id`` / ``scene_id`` are stamped by
    :func:`propose_concept` from the input phrase.

    Args:
        model_id: LiteLLM-compatible model id (e.g.
            ``"openai/gpt-4o"`` or
            ``"bedrock/anthropic.claude-3-5-sonnet"``).
        extra: Extra kwargs forwarded to ``litellm.completion`` (e.g.
            ``{"temperature": 0.4}``). ``None`` means defaults.

    Returns:
        A callable that takes ``(phrase, style_lock, visual_style)``
        and returns a single concept dict.
    """
    client_kwargs = dict(extra or {})

    def _propose(
        phrase: dict[str, Any],
        style_lock: dict[str, Any],
        visual_style: dict[str, Any],
    ) -> dict[str, Any]:
        user = (
            "Phrase (JSON):\n"
            + json.dumps(phrase, ensure_ascii=False)
            + "\nStyle lock (JSON):\n"
            + json.dumps(style_lock, ensure_ascii=False)
            + "\nVisual style (JSON):\n"
            + json.dumps(visual_style, ensure_ascii=False)
            + "\nProduce the JSON concept object described in the "
            "system prompt."
        )
        logger.debug(
            "model_id=<%s>, phrase_id=<%s>, phrase_type=<%s> | visual_llm "
            "propose concept",
            model_id,
            phrase.get("phrase_id"),
            phrase.get("phrase_type"),
        )
        resp = litellm.completion(
            model=model_id,
            messages=[
                {"role": "system", "content": _PROPOSE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            **client_kwargs,
        )
        parsed = _parse_completion(resp)
        for key in _REQUIRED_KEYS:
            if key not in parsed:
                raise RuntimeError(
                    f"concept proposer missing {key!r} key: {parsed!r}"
                )
        return parsed

    return _propose


__all__ = ["make_concept_proposer"]
