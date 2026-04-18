"""ARCH-D3: Shared escalation-policy config (typed).

Both content ladders -- audio (permissive) and video (strict one-shot) --
read their per-tier retry budgets from this module rather than hardcoding
numbers.  This is the data-structure encoding of diagrams 2, 3, and 4 of
``docs/ARCHITECTURE_DIAGRAMS.md``:

* Diagram 2 -- audio content ladder is *permissive* at the low tiers
  because narration reconciliation IS the mechanism by which the
  authoritative OTIO is born.
* Diagram 3 -- video content ladder is *strict one-shot per tier*:
  each tier gets exactly one attempt; a second failure at the same tier
  is not permitted -- escalate immediately.
* Diagram 4 -- the audio/video asymmetry is a shared-shape, medium-
  calibrated budget table.

The configs here are the single source of truth for per-tier budgets.
``server/recovery.py`` reads from this module when constructing the
per-medium agent policies; it MUST NOT hardcode budget numbers.

Fail loud: ``LadderBudgetConfig.__post_init__`` refuses any config whose
shape violates its discipline (e.g. a STRICT_ONE_SHOT ladder with a
non-SINGLE budget at any tier).  This is the runtime check that the
tests in ``server/tests/test_escalation_policy.py`` pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from recovery import (
    AUDIO_PERMISSIVE_BUDGETS,
    RecoveryBudget,
    RecoveryLevel,
)

__all__ = [
    "LadderDiscipline",
    "LadderConfigError",
    "LadderBudgetConfig",
    "AUDIO_LADDER_CONFIG",
    "VIDEO_LADDER_CONFIG",
    "LADDER_CONFIGS",
    "get_ladder_config",
    "STRICT_DISCIPLINE_VALUE",
]


# ---------------------------------------------------------------------------
# Discipline enum
# ---------------------------------------------------------------------------

class LadderDiscipline(str, Enum):
    """How a content ladder spends its per-tier budget.

    PERMISSIVE -- a tier may attempt its recovery more than once; the
        per-tier ``RecoveryBudget`` label (WIDE / GENEROUS / ...) sets
        the attempt count.  Used by the audio content ladder because
        reconciliation IS the authoritative-OTIO mechanism (diagram 2).

    STRICT_ONE_SHOT -- every tier gets *exactly one* attempt.  A second
        failure at the same tier is forbidden -- the ladder escalates to
        the next tier immediately.  Used by the video content ladder
        because video is expensive and bounded by an OTIO that is
        already law (diagram 3).
    """

    PERMISSIVE = "permissive"
    STRICT_ONE_SHOT = "strict_one_shot"


# String constant form usable across modules without importing the enum
# (avoids circular imports in ``recovery.py``).
STRICT_DISCIPLINE_VALUE: str = LadderDiscipline.STRICT_ONE_SHOT.value


class LadderConfigError(ValueError):
    """Raised when a ``LadderBudgetConfig`` violates its declared discipline.

    Fail-loud on construction -- a STRICT_ONE_SHOT ladder with a
    WIDE budget somewhere is a spec violation (ARCH-D2), not a
    runtime surprise.
    """


# Every tier a content ladder must cover.  Missing a tier here would
# silently fall back to defaults during recovery -- exactly what D3 is
# supposed to prevent.
_ALL_LEVELS: frozenset[RecoveryLevel] = frozenset(RecoveryLevel)


# ---------------------------------------------------------------------------
# LadderBudgetConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LadderBudgetConfig:
    """Per-medium recovery-ladder configuration.

    Immutable data class carrying:

    * ``ladder_id`` -- stable string identifier (e.g. ``audio_content``).
    * ``medium`` -- which pipeline medium this ladder serves
      (``"audio"`` or ``"video"``).
    * ``discipline`` -- ``PERMISSIVE`` or ``STRICT_ONE_SHOT`` (see above).
    * ``budgets`` -- mapping from every ``RecoveryLevel`` to the
      canonical ``RecoveryBudget`` label for that tier.  The label's
      ``IntEnum`` value is the attempt count.

    Invariants (checked in ``__post_init__``, raise ``LadderConfigError``):

    1. ``budgets`` covers every ``RecoveryLevel`` (L0-L4).
    2. All keys are ``RecoveryLevel`` members; all values are
       ``RecoveryBudget`` members.
    3. Budgets are monotone non-increasing as the tier climbs: cheap
       retries at low tiers, strict gates at high tiers.
    4. If ``discipline == STRICT_ONE_SHOT``, every budget is
       ``RecoveryBudget.SINGLE``.  Diagram 3 forbids second attempts.
    """

    ladder_id: str
    medium: str
    discipline: LadderDiscipline
    budgets: Mapping[RecoveryLevel, RecoveryBudget]

    def __post_init__(self) -> None:
        if not isinstance(self.ladder_id, str) or not self.ladder_id:
            raise LadderConfigError(
                "LadderBudgetConfig.ladder_id must be a non-empty string"
            )
        if not isinstance(self.medium, str) or not self.medium:
            raise LadderConfigError(
                "LadderBudgetConfig.medium must be a non-empty string"
            )
        if not isinstance(self.discipline, LadderDiscipline):
            raise LadderConfigError(
                f"LadderBudgetConfig.discipline must be a LadderDiscipline, "
                f"got {type(self.discipline).__name__}"
            )
        if not isinstance(self.budgets, Mapping):
            raise LadderConfigError(
                f"LadderBudgetConfig.budgets must be a mapping, "
                f"got {type(self.budgets).__name__}"
            )

        # ── Coverage: every RecoveryLevel must have a budget ──────────
        missing = _ALL_LEVELS - set(self.budgets.keys())
        if missing:
            raise LadderConfigError(
                f"Ladder '{self.ladder_id}' missing budgets for tiers: "
                f"{sorted(m.name for m in missing)}. "
                "Per ARCH-D3 every tier must have an explicit budget."
            )

        # ── Type check keys/values ────────────────────────────────────
        for level, label in self.budgets.items():
            if not isinstance(level, RecoveryLevel):
                raise LadderConfigError(
                    f"Ladder '{self.ladder_id}' budget key must be a "
                    f"RecoveryLevel, got {type(level).__name__}: {level!r}"
                )
            if not isinstance(label, RecoveryBudget):
                raise LadderConfigError(
                    f"Ladder '{self.ladder_id}' budget value for "
                    f"{level.name} must be a RecoveryBudget, "
                    f"got {type(label).__name__}: {label!r}"
                )

        # ── Monotone non-increasing budget as tier climbs ─────────────
        ordered_levels = sorted(self.budgets.keys(), key=int)
        previous_value: int | None = None
        for level in ordered_levels:
            current = int(self.budgets[level])
            if previous_value is not None and current > previous_value:
                raise LadderConfigError(
                    f"Ladder '{self.ladder_id}' budget is non-monotone: "
                    f"tier {level.name} has {current} attempts but the "
                    f"previous tier had {previous_value}. "
                    "Budgets must shrink as the ladder climbs."
                )
            previous_value = current

        # ── Discipline enforcement ────────────────────────────────────
        if self.discipline == LadderDiscipline.STRICT_ONE_SHOT:
            bad = [
                (level.name, label.name)
                for level, label in self.budgets.items()
                if label is not RecoveryBudget.SINGLE
            ]
            if bad:
                raise LadderConfigError(
                    f"Ladder '{self.ladder_id}' is STRICT_ONE_SHOT but has "
                    f"non-SINGLE budgets: {bad}. "
                    "Per ARCH-D2 / diagram 3, each tier gets exactly one "
                    "attempt -- a second failure at the same tier is not "
                    "permitted."
                )

        # ── Freeze the mapping so callers cannot mutate canonical config
        object.__setattr__(
            self, "budgets", MappingProxyType(dict(self.budgets))
        )

    # ── Accessor helpers ──────────────────────────────────────────────

    def attempts_for(self, level: RecoveryLevel) -> int:
        """Return the attempt count (budget label's int value) for a tier.

        For STRICT_ONE_SHOT ladders, always 1 by construction.
        """
        return int(self.budgets[level])

    def label_for(self, level: RecoveryLevel) -> RecoveryBudget:
        """Return the canonical ``RecoveryBudget`` label for a tier."""
        return self.budgets[level]

    def is_strict_one_shot(self) -> bool:
        """True if this ladder forbids second attempts at the same tier."""
        return self.discipline == LadderDiscipline.STRICT_ONE_SHOT


# ---------------------------------------------------------------------------
# Canonical configs -- the single source of truth for content-ladder budgets
# ---------------------------------------------------------------------------

# Audio content ladder (ARCH-D1, diagrams 2 + 4) -- permissive at the low
# tiers; reconciliation IS the authoritative-OTIO mechanism.  Reuses the
# AUDIO_PERMISSIVE_BUDGETS mapping defined in ``recovery.py`` so that D1's
# existing dashboard wiring keeps working unchanged.
AUDIO_LADDER_CONFIG: LadderBudgetConfig = LadderBudgetConfig(
    ladder_id="audio_content",
    medium="audio",
    discipline=LadderDiscipline.PERMISSIVE,
    budgets=dict(AUDIO_PERMISSIVE_BUDGETS),
)


# Video content ladder (ARCH-D2, diagrams 3 + 4) -- STRICT one-shot per
# tier.  Every tier holds ``RecoveryBudget.SINGLE`` (attempt count = 1);
# a second failure at the same tier is forbidden.
VIDEO_LADDER_CONFIG: LadderBudgetConfig = LadderBudgetConfig(
    ladder_id="video_content",
    medium="video",
    discipline=LadderDiscipline.STRICT_ONE_SHOT,
    budgets={
        RecoveryLevel.FIX: RecoveryBudget.SINGLE,
        RecoveryLevel.RETRY: RecoveryBudget.SINGLE,
        RecoveryLevel.CREATIVE: RecoveryBudget.SINGLE,
        RecoveryLevel.COLLABORATIVE: RecoveryBudget.SINGLE,
        RecoveryLevel.HUMAN: RecoveryBudget.SINGLE,
    },
)


# Registry keyed by medium.  Exposed so future ladders (e.g. text, image)
# can be added by dropping a ``LadderBudgetConfig`` in here without
# touching the policy factories in ``recovery.py``.
LADDER_CONFIGS: Mapping[str, LadderBudgetConfig] = MappingProxyType({
    AUDIO_LADDER_CONFIG.medium: AUDIO_LADDER_CONFIG,
    VIDEO_LADDER_CONFIG.medium: VIDEO_LADDER_CONFIG,
})


def get_ladder_config(medium: str) -> LadderBudgetConfig:
    """Return the canonical ladder config for a medium.

    Raises ``LadderConfigError`` for unknown media -- we fail loud rather
    than silently falling back to a permissive default.
    """
    try:
        return LADDER_CONFIGS[medium]
    except KeyError as exc:
        raise LadderConfigError(
            f"No ladder config for medium '{medium}'. "
            f"Known media: {sorted(LADDER_CONFIGS.keys())}"
        ) from exc
