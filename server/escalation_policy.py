"""Escalation-policy module — ladder configurations, recovery context, and policy.

Defines the three escalation ladders (audio, video, infra) and the
``EscalationPolicy`` class that selects the correct ladder for a given
failure class and manages level transitions.

Ladder discipline:
    * **Permissive** ladders grant multiple attempts at lower tiers,
      tapering as the ladder climbs.  Used for audio and infra because
      cheap retries at L0/L1 are the primary reconciliation mechanism.
    * **Strict** ladders grant exactly one attempt per tier — a second
      failure at the same tier forces an immediate escalation.  Used
      for video because generation is expensive and the OTIO timeline
      is already law by the time video runs.

References:
    * Diagram 2 — audio content ladder (permissive)
    * Diagram 3 — video content ladder (strict one-shot)
    * Diagram 8 — infra ladder (permissive)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# RecoveryLevel enum
# ---------------------------------------------------------------------------

class RecoveryLevel(Enum):
    """Canonical recovery tiers, ordered from cheapest to most expensive.

    The integer values are stable and monotone — code may compare them
    numerically to determine relative tier height.
    """

    FIX = 0
    RETRY = 1
    CREATIVE = 2
    COLLABORATIVE = 3
    HUMAN = 4


# ---------------------------------------------------------------------------
# LadderConfig dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LadderConfig:
    """Configuration for a single escalation ladder.

    Attributes:
        name: Human-readable identifier (e.g. ``"audio_content"``).
        discipline: ``"permissive"`` or ``"strict"``.  A strict ladder
            must have a budget of 1 at every level; a permissive ladder
            may grant multiple attempts at lower tiers.
        level_budgets: Mapping from level number (0–4) to the maximum
            number of attempts allowed at that level before escalation.
        level_names: Mapping from level number (0–4) to a human-readable
            name for the level (e.g. ``"Fix"``).
    """

    name: str
    discipline: str
    level_budgets: dict[int, int]
    level_names: dict[int, str]

    def __post_init__(self) -> None:
        if self.discipline not in ("permissive", "strict"):
            raise ValueError(
                f"LadderConfig.discipline must be 'permissive' or 'strict', "
                f"got {self.discipline!r}"
            )
        expected_levels = {0, 1, 2, 3, 4}
        if set(self.level_budgets.keys()) != expected_levels:
            raise ValueError(
                f"LadderConfig.level_budgets must cover levels 0–4, "
                f"got keys {sorted(self.level_budgets.keys())}"
            )
        if set(self.level_names.keys()) != expected_levels:
            raise ValueError(
                f"LadderConfig.level_names must cover levels 0–4, "
                f"got keys {sorted(self.level_names.keys())}"
            )
        if self.discipline == "strict":
            for level, budget in self.level_budgets.items():
                if budget != 1:
                    raise ValueError(
                        f"Strict ladder '{self.name}' must have budget 1 at "
                        f"every level, but level {level} has budget {budget}"
                    )


# ---------------------------------------------------------------------------
# Canonical ladder configs
# ---------------------------------------------------------------------------

AUDIO_LADDER_CONFIG: LadderConfig = LadderConfig(
    name="audio_content",
    discipline="permissive",
    level_budgets={
        0: 8,   # FIX          — reseed TTS, rephrase, adjust silence
        1: 4,   # RETRY        — audio-understanding consultation, multi-shot params
        2: 2,   # CREATIVE     — alternative voices, TTS providers
        3: 1,   # COLLABORATIVE — coordinate across agents
        4: 1,   # HUMAN        — human decision
    },
    level_names={
        0: "Fix",
        1: "Retry",
        2: "Creative",
        3: "Collaborative",
        4: "Human",
    },
)
"""Audio content ladder — permissive (diagram 2).

Narration reconciliation IS the mechanism by which the authoritative
OTIO is born, so low tiers are given generous budgets to converge.
"""

VIDEO_LADDER_CONFIG: LadderConfig = LadderConfig(
    name="video_content",
    discipline="strict",
    level_budgets={
        0: 1,   # FIX          — domain-informed prompt rewrite
        1: 1,   # RETRY        — different generation strategy
        2: 1,   # CREATIVE     — alternative approach
        3: 1,   # COLLABORATIVE — may reshape clip plan, duration-preserving
        4: 1,   # HUMAN        — human decision
    },
    level_names={
        0: "Fix",
        1: "Retry",
        2: "Creative",
        3: "Collaborative",
        4: "Human",
    },
)
"""Video content ladder — strict one-shot (diagram 3).

Video generation is expensive and bounded by an OTIO that is already
law.  Each tier gets exactly one attempt; a second failure at the same
tier forces an immediate escalation.
"""

INFRA_LADDER_CONFIG: LadderConfig = LadderConfig(
    name="infra",
    discipline="permissive",
    level_budgets={
        0: 4,   # FIX          — retry on different healthy worker
        1: 2,   # RETRY        — recycle suspect worker, redispatch
        2: 1,   # CREATIVE     — scale fleet, hot-swap GPU tier, different region
        3: 1,   # COLLABORATIVE — coordinate with content ladder, down-spec params
        4: 1,   # HUMAN        — human decision
    },
    level_names={
        0: "Fix",
        1: "Retry",
        2: "Creative",
        3: "Collaborative",
        4: "Human",
    },
)
"""Infra ladder — permissive (diagram 8).

Infrastructure failures are often transient (worker blips, GPU
hiccoughs), so L0 and L1 are given multiple attempts.  Higher tiers
escalate quickly because they involve fleet-level changes.
"""


# ---------------------------------------------------------------------------
# RecoveryContext dataclass
# ---------------------------------------------------------------------------

@dataclass
class RecoveryContext:
    """Snapshot of the current recovery state for a failing operation.

    Passed to recovery agents so they can reason about what has already
    been tried and what budget remains.

    Attributes:
        operation_name: Identifier of the operation that failed.
        error_msg: The error message from the most recent failure.
        attempt_num: 1-based index of the current attempt within the
            current level.
        max_attempts: Budget for the current level (from
            ``LadderConfig.level_budgets``).
        current_level: Numeric level (0–4) the ladder is currently on.
        level_name: Human-readable name of the current level.
        previous_attempts: List of dicts describing prior attempts at
            this level (keys typically include ``"attempt"``,
            ``"error"``, ``"strategy"``).
        operation_kwargs: Keyword arguments originally passed to the
            operation (may be mutated by creative strategies).
        pipeline_state: Shared mutable state carried across the
            pipeline (e.g. partial OTIO, clip plans).
        diagnostic_data: Machine-readable diagnostics gathered by
            recovery agents (worker health, GPU utilisation, etc.).
        failure_class: ``"content"`` or ``"infra"`` — determines which
            ladder the escalation policy selects.
    """

    operation_name: str
    error_msg: str
    attempt_num: int
    max_attempts: int
    current_level: int
    level_name: str
    previous_attempts: list[dict[str, Any]]
    operation_kwargs: dict[str, Any] = field(default_factory=dict)
    pipeline_state: dict[str, Any] = field(default_factory=dict)
    diagnostic_data: dict[str, Any] = field(default_factory=dict)
    failure_class: str = "content"


# ---------------------------------------------------------------------------
# EscalationPolicy class
# ---------------------------------------------------------------------------

class EscalationPolicy:
    """Manages ladder selection and level transitions for a pipeline medium.

    An ``EscalationPolicy`` pairs a *content* ladder with an *infra*
    ladder.  When a failure occurs, the policy selects the correct
    ladder based on the failure class (``"content"`` or ``"infra"``) and
    decides whether the current level has been exhausted and the ladder
    should escalate.

    Args:
        content_ladder: The ``LadderConfig`` to use for content failures.
        infra_ladder: The ``LadderConfig`` to use for infra failures.
    """

    def __init__(self, content_ladder: LadderConfig, infra_ladder: LadderConfig) -> None:
        self._content_ladder = content_ladder
        self._infra_ladder = infra_ladder

    # ── Ladder selection ───────────────────────────────────────────────

    def select_ladder(self, failure_class: str) -> LadderConfig:
        """Return the ladder appropriate for the given failure class.

        Args:
            failure_class: ``"content"`` selects the content ladder;
                ``"infra"`` selects the infra ladder.

        Raises:
            ValueError: If *failure_class* is neither ``"content"`` nor
                ``"infra"``.
        """
        if failure_class == "content":
            return self._content_ladder
        if failure_class == "infra":
            return self._infra_ladder
        raise ValueError(
            f"failure_class must be 'content' or 'infra', got {failure_class!r}"
        )

    # ── Budget queries ─────────────────────────────────────────────────

    def get_budget(self, failure_class: str, level: int) -> int:
        """Return the maximum attempts allowed at *level* for *failure_class*.

        Args:
            failure_class: ``"content"`` or ``"infra"``.
            level: Recovery level (0–4).

        Raises:
            ValueError: If *level* is not in 0–4.
        """
        ladder = self.select_ladder(failure_class)
        if level not in ladder.level_budgets:
            raise ValueError(
                f"level must be 0–4, got {level}"
            )
        return ladder.level_budgets[level]

    # ── Escalation decision ────────────────────────────────────────────

    def should_escalate(
        self,
        failure_class: str,
        level: int,
        attempts_at_level: int,
    ) -> bool:
        """Return ``True`` if the ladder should escalate past *level*.

        Escalation occurs when the number of attempts already made at
        *level* meets or exceeds the budget for that level.

        Args:
            failure_class: ``"content"`` or ``"infra"``.
            level: Current recovery level (0–4).
            attempts_at_level: How many attempts have already been made
                at this level (including the one that just failed).
        """
        budget = self.get_budget(failure_class, level)
        return attempts_at_level >= budget

    # ── Level transition ───────────────────────────────────────────────

    def next_level(self, current_level: int) -> int | None:
        """Return the next level up the ladder, or ``None`` if already at HUMAN.

        The ladder always climbs — there is no mechanism for de-escalation.

        Args:
            current_level: The level the ladder is currently on (0–4).

        Returns:
            The next integer level, or ``None`` if *current_level* is 4
            (HUMAN — the top of the ladder).
        """
        if current_level >= 4:
            return None
        return current_level + 1


# ---------------------------------------------------------------------------
# Default policy instances
# ---------------------------------------------------------------------------

DEFAULT_AUDIO_POLICY: EscalationPolicy = EscalationPolicy(
    content_ladder=AUDIO_LADDER_CONFIG,
    infra_ladder=INFRA_LADDER_CONFIG,
)
"""Default escalation policy for the audio pipeline.

Content failures use the permissive audio ladder; infra failures use
the permissive infra ladder.
"""

DEFAULT_VIDEO_POLICY: EscalationPolicy = EscalationPolicy(
    content_ladder=VIDEO_LADDER_CONFIG,
    infra_ladder=INFRA_LADDER_CONFIG,
)
"""Default escalation policy for the video pipeline.

Content failures use the strict one-shot video ladder; infra failures
use the permissive infra ladder.
"""
