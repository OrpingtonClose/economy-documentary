"""VisualCoherenceEvaluator — LLM-as-judge on visual concept coherence.

Scores a list of per-scene visual concepts against a shared
``style_lock`` along three axes: style consistency, camera variety, and
narrative-visual alignment. Uses the :data:`CritiqueRating` vocabulary
already in use by the critique store (``EXCELLENT`` / ``GOOD`` /
``FAIR`` / ``POOR`` / ``UNKNOWN``) so downstream dashboards can pivot
on a single rating domain.

Input shape
-----------
``EvaluationData`` with:

* ``actual_output``: dict with keys ``visual_concepts``
  (``list[dict]``) and optional ``style_lock`` (``dict``). Per-scene
  concepts should carry at least ``scene_num``, ``visual_direction``,
  and ``camera``.
* ``input``: the original user prompt, forwarded to the judge as topic
  context.
* ``metadata[`rubric_override`]`` (optional): replacement rubric text
  for bespoke experiments.

Output
------
One :class:`EvaluationOutput` with:

* ``score``: ``EXCELLENT`` → 1.0, ``GOOD`` → 0.75, ``FAIR`` → 0.5,
  ``POOR`` → 0.25, ``UNKNOWN`` → 0.0.
* ``test_pass``: ``score >= 0.5`` (soft threshold per
  ``THRESHOLDS.md`` — visual coherence is not a hard gate, only a
  regression signal).
* ``label``: the raw rating string.
* ``reason``: the judge's reasoning.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from pydantic import BaseModel, Field
from strands import Agent
from strands.models.model import Model
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

_SCORE_MAPPING: dict[str, float] = {
    "EXCELLENT": 1.0,
    "GOOD": 0.75,
    "FAIR": 0.5,
    "POOR": 0.25,
    "UNKNOWN": 0.0,
}

_SYSTEM_PROMPT = """You are a senior cinematography lead reviewing a
documentary's per-scene visual plan. Rate the overall coherence of the
visual concepts across scenes using EXACTLY ONE of the following labels:

* EXCELLENT — consistent style, varied camera work, tight narrative alignment.
* GOOD — mostly consistent and well-aligned, with minor issues.
* FAIR — noticeable drift in style OR repetitive camera work OR weak alignment.
* POOR — inconsistent style, monotonous camera work, or concepts unrelated to narration.
* UNKNOWN — not enough information to rate.

Assess three axes:

1. Style consistency — concepts honour the shared ``style_lock``
   (palette, grade, lens feel, era cues).
2. Camera variety — scenes are not all identical coverage; angles,
   lens lengths, and motion read as deliberate variation.
3. Narrative-visual alignment — each scene's visual supports the
   narration beat (no decorative drift, no topical mismatch).

Return structured output with a short reasoning string (<= 150 words)
and the single-label rating.
"""


class VisualCoherenceRating(BaseModel):
    """Structured output schema for the visual-coherence judge."""

    reasoning: str = Field(
        description="Step-by-step reasoning (<= 150 words) across style, camera, and alignment.",
    )
    rating: str = Field(
        description="EXCELLENT, GOOD, FAIR, POOR, or UNKNOWN.",
    )


class VisualCoherenceEvaluator(Evaluator[str, dict[str, Any]]):
    """LLM-as-judge on visual concept coherence across scenes.

    Args:
        model: Judge model (Strands :class:`Model` or string id).
            Defaults to :data:`None`, which lets the Strands
            :class:`Agent` pick its default (Bedrock).
        system_prompt: Optional override of the default rubric prompt.
    """

    def __init__(
        self,
        model: Model | str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._system_prompt = system_prompt or _SYSTEM_PROMPT

    def evaluate(
        self,
        evaluation_case: EvaluationData[str, dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        concepts = actual.get("visual_concepts") or []
        style_lock = actual.get("style_lock") or {}
        metadata = evaluation_case.metadata or {}
        topic = evaluation_case.input or metadata.get("topic") or ""

        if not concepts:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="no visual_concepts supplied",
                    label="UNKNOWN",
                )
            ]

        prompt = _format_prompt(
            topic=str(topic),
            concepts=concepts,
            style_lock=style_lock,
            rubric_override=metadata.get("rubric_override"),
        )
        agent = Agent(
            model=self._model,
            system_prompt=self._system_prompt,
            callback_handler=None,
        )
        try:
            result = agent(prompt, structured_output_model=VisualCoherenceRating)
        except Exception as exc:
            logger.warning(
                "error=<%s> | visual coherence judge failed; returning UNKNOWN",
                exc,
            )
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=f"judge error: {exc}",
                    label="UNKNOWN",
                )
            ]

        rating_obj = cast(VisualCoherenceRating, result.structured_output)
        return [_rating_to_output(rating_obj)]


def _format_prompt(
    *,
    topic: str,
    concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
    rubric_override: str | None,
) -> str:
    parts: list[str] = []
    if rubric_override:
        parts.append(f"# Extra rubric from the caller:\n{rubric_override}")
    parts.append(f"# Topic\n{topic or '(no topic supplied)'}")
    parts.append(
        "# Style lock\n" + json.dumps(style_lock, indent=2, sort_keys=True, default=str)
    )
    parts.append(
        "# Visual concepts (one object per scene)\n"
        + json.dumps(concepts, indent=2, sort_keys=True, default=str)
    )
    parts.append(
        "# Task\nRate the overall coherence. Return reasoning and a rating."
    )
    return "\n\n".join(parts)


def _rating_to_output(rating: VisualCoherenceRating) -> EvaluationOutput:
    label = rating.rating.strip().upper()
    score = _SCORE_MAPPING.get(label, 0.0)
    return EvaluationOutput(
        score=score,
        test_pass=score >= 0.5,
        reason=rating.reasoning,
        label=label if label in _SCORE_MAPPING else "UNKNOWN",
    )
