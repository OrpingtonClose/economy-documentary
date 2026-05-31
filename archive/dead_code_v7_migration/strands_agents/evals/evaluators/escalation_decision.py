"""EscalationDecisionEvaluator — LLM-as-judge on recovery correctness.

Scores whether a recovery decision
(``fix`` / ``retry`` / ``skip`` / ``escalate`` / ``abort``) was
appropriate given the diagnostic context. Used by components 10
(production supervisor) and 13 (escalation supervisor).

Input shape
-----------
``EvaluationData`` with:

* ``actual_output``: dict with keys:

    * ``action`` (required): one of ``"fix"``, ``"retry"``, ``"skip"``,
      ``"escalate"``, ``"abort"``.
    * ``reasoning`` (optional): short string from the agent explaining
      why it chose that action.
    * ``state_patches`` (optional): dict that will be applied if
      ``action == "fix"``; forwarded to the judge as evidence the fix
      is plausible.

* ``metadata[`diagnostic`]`` (required): dict describing the failure
  symptom — error class, retry count to date, whether the failure is
  deterministic vs. transient, affected artifact id, etc.
* ``metadata[`rubric_override`]`` (optional): additional instructions
  to tighten or relax the rubric per experiment.

Output
------
One :class:`EvaluationOutput` with:

* ``score``: ``CORRECT`` → 1.0, ``REASONABLE`` → 0.5,
  ``HARMFUL`` → 0.0.
* ``test_pass``: ``score >= 0.5``.
* ``label``: the verdict label.
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

_VALID_ACTIONS = frozenset({"fix", "retry", "skip", "escalate", "abort"})

_SCORE_MAPPING: dict[str, float] = {
    "CORRECT": 1.0,
    "REASONABLE": 0.5,
    "HARMFUL": 0.0,
}

_SYSTEM_PROMPT = """You are a staff engineer reviewing an AI agent's
recovery decision for a video-production pipeline. Classify the
decision using EXACTLY ONE of the following labels:

* CORRECT — the decision is the best available action given the diagnostic.
* REASONABLE — the decision is defensible but suboptimal
  (e.g. retry where a targeted fix would have been cheaper).
* HARMFUL — the decision is likely to make things worse or mask a
  real fault (e.g. retry for a deterministic failure; abort when a
  trivial fix exists; skip past a hard contract violation).

Decision space:

* fix — apply ``state_patches`` and re-run; only valid when the patch
  plausibly addresses the symptom.
* retry — re-run unchanged; only valid for transient / probabilistic
  failures not already retried to exhaustion.
* skip — accept the failure and continue; only valid when the artifact
  is non-critical and downstream stages can cope.
* escalate — hand off to the next level; valid when the current agent
  cannot plausibly fix the failure.
* abort — stop the pipeline; valid only for catastrophic or
  contract-violating failures.

Return structured output with a short reasoning string (<= 150 words)
and the single-label verdict.
"""


class EscalationDecisionRating(BaseModel):
    """Structured output for the escalation-decision judge."""

    reasoning: str = Field(
        description="Step-by-step reasoning (<= 150 words) against the rubric.",
    )
    verdict: str = Field(
        description="CORRECT, REASONABLE, or HARMFUL.",
    )


class EscalationDecisionEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """LLM-as-judge on recovery-action correctness.

    Args:
        model: Judge model (Strands :class:`Model` or string id).
            Defaults to :data:`None`.
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
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        diagnostic = metadata.get("diagnostic") or {}
        action = str(actual.get("action", "")).strip().lower()

        if not action:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="no 'action' key in actual_output",
                    label="HARMFUL",
                )
            ]
        if action not in _VALID_ACTIONS:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=f"unknown recovery action: {action!r}",
                    label="HARMFUL",
                )
            ]
        if not diagnostic:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="no metadata['diagnostic'] supplied — cannot judge",
                    label="HARMFUL",
                )
            ]

        prompt = _format_prompt(
            diagnostic=diagnostic,
            action=action,
            reasoning=actual.get("reasoning"),
            state_patches=actual.get("state_patches"),
            rubric_override=metadata.get("rubric_override"),
        )
        agent = Agent(
            model=self._model,
            system_prompt=self._system_prompt,
            callback_handler=None,
        )
        try:
            result = agent(prompt, structured_output_model=EscalationDecisionRating)
        except Exception as exc:
            logger.warning(
                "error=<%s> | escalation judge failed; returning HARMFUL",
                exc,
            )
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=f"judge error: {exc}",
                    label="HARMFUL",
                )
            ]

        rating = cast(EscalationDecisionRating, result.structured_output)
        return [_rating_to_output(rating)]


def _format_prompt(
    *,
    diagnostic: dict[str, Any],
    action: str,
    reasoning: Any,
    state_patches: Any,
    rubric_override: str | None,
) -> str:
    parts: list[str] = []
    if rubric_override:
        parts.append(f"# Extra rubric from the caller:\n{rubric_override}")
    parts.append(
        "# Diagnostic context\n"
        + json.dumps(diagnostic, indent=2, sort_keys=True, default=str)
    )
    parts.append(f"# Proposed action\n{action}")
    if reasoning:
        parts.append(f"# Agent's stated reasoning\n{reasoning}")
    if state_patches:
        parts.append(
            "# Proposed state patches (if action == fix)\n"
            + json.dumps(state_patches, indent=2, sort_keys=True, default=str)
        )
    parts.append(
        "# Task\nClassify the decision as CORRECT, REASONABLE, or HARMFUL."
    )
    return "\n\n".join(parts)


def _rating_to_output(rating: EscalationDecisionRating) -> EvaluationOutput:
    label = rating.verdict.strip().upper()
    score = _SCORE_MAPPING.get(label, 0.0)
    return EvaluationOutput(
        score=score,
        test_pass=score >= 0.5,
        reason=rating.reasoning,
        label=label if label in _SCORE_MAPPING else "HARMFUL",
    )
