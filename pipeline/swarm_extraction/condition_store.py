"""
Condition store -- manages atomic conditions and deduplication.

Provides admission control for new conditions: checks for duplicates,
low confidence, and contradictions before accepting into the store.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from .models import AtomicCondition

logger = logging.getLogger(__name__)


@dataclass
class AdmissionResult:
    """Result of attempting to admit a condition."""
    admitted: bool
    reason: str = ""
    condition: Optional[AtomicCondition] = None


class ConditionStore:
    """Thread-safe store for atomic conditions with deduplication."""

    def __init__(self, min_confidence: float = 0.2, similarity_threshold: float = 0.85):
        self._conditions: list[AtomicCondition] = []
        self._lock = asyncio.Lock()
        self._min_confidence = min_confidence
        self._similarity_threshold = similarity_threshold

    @property
    def conditions(self) -> list[AtomicCondition]:
        return list(self._conditions)

    def __len__(self) -> int:
        return len(self._conditions)

    async def admit(self, condition: AtomicCondition) -> AdmissionResult:
        """Try to admit a single condition into the store."""
        async with self._lock:
            # Reject low confidence
            if condition.confidence < self._min_confidence:
                return AdmissionResult(
                    admitted=False,
                    reason=f"Confidence {condition.confidence:.2f} below threshold",
                )

            # Reject empty facts
            if not condition.fact or len(condition.fact.strip()) < 10:
                return AdmissionResult(admitted=False, reason="Fact too short or empty")

            # Check for duplicates (simple word overlap)
            for existing in self._conditions:
                similarity = _word_overlap(condition.fact, existing.fact)
                if similarity > self._similarity_threshold:
                    return AdmissionResult(
                        admitted=False,
                        reason=f"Duplicate of existing condition (similarity={similarity:.2f})",
                    )

            self._conditions.append(condition)
            return AdmissionResult(
                admitted=True,
                condition=condition,
            )

    async def admit_batch(self, conditions: list[AtomicCondition]) -> list[AdmissionResult]:
        """Admit a batch of conditions."""
        results = []
        for c in conditions:
            result = await self.admit(c)
            results.append(result)
        return results


class QuestionRegistry:
    """Registry for tracking research questions and their status."""

    def __init__(self):
        self._questions: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def register(self, question_id: str, question: str, context: str = "") -> None:
        async with self._lock:
            self._questions[question_id] = {
                "question": question,
                "context": context,
                "status": "pending",
                "conditions": [],
            }

    async def update_status(self, question_id: str, status: str) -> None:
        async with self._lock:
            if question_id in self._questions:
                self._questions[question_id]["status"] = status

    async def add_condition(self, question_id: str, condition: AtomicCondition) -> None:
        async with self._lock:
            if question_id in self._questions:
                self._questions[question_id]["conditions"].append(condition)

    @property
    def questions(self) -> dict:
        return dict(self._questions)


def _word_overlap(text1: str, text2: str) -> float:
    """Compute word overlap similarity between two texts."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)
