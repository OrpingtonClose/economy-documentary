"""Tests for ARCH-D1: audio-ladder permissive budget labels.

The audio content ladder is deliberately permissive at the low tiers because
narration reconciliation IS the mechanism by which the authoritative OTIO
is born (diagrams 2 + 4 of ``docs/ARCHITECTURE_DIAGRAMS.md``).  These tests
pin the canonical ``RecoveryBudget`` labels and the ``RecoveryPolicy``
wiring so the audio ladder cannot silently regress to a stricter shape.

Fail loud: if anyone flips L0 from WIDE to something narrower, or drops
the permissive budget labels off the audio policy, these tests break
immediately rather than discovering it in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make ``server/`` importable as a flat module root for the tests.
_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from recovery import (  # noqa: E402
    AUDIO_PERMISSIVE_BUDGETS,
    RecoveryBudget,
    RecoveryLevel,
    RecoveryPolicy,
    _make_audio_agent_policy,
    _make_video_agent_policy,
)


# ---------------------------------------------------------------------------
# RecoveryBudget enum — canonical labels and monotone ordering
# ---------------------------------------------------------------------------

class TestRecoveryBudgetEnum:
    """The canonical budget label vocabulary from ARCH-D (diagrams 2 + 4)."""

    def test_all_five_labels_exist(self) -> None:
        """L0 WIDE, L1 GENEROUS, L2 NARROW-MULTI, L3 BOUNDED, L4 SINGLE."""
        names = {m.name for m in RecoveryBudget}
        assert names == {"WIDE", "GENEROUS", "NARROW_MULTI", "BOUNDED", "SINGLE"}

    def test_budget_values_are_monotone_decreasing(self) -> None:
        """Attempt counts must shrink as the ladder climbs.

        Low tiers are cheap retries; high tiers are expensive coordination.
        If anyone inverts the ordering, reconciliation degrades to a
        single-shot video-style ladder — exactly what ARCH-D forbids.
        """
        assert RecoveryBudget.WIDE.value > RecoveryBudget.GENEROUS.value
        assert RecoveryBudget.GENEROUS.value > RecoveryBudget.NARROW_MULTI.value
        assert RecoveryBudget.NARROW_MULTI.value > RecoveryBudget.BOUNDED.value
        assert RecoveryBudget.BOUNDED.value > RecoveryBudget.SINGLE.value

    def test_wide_is_many_attempts(self) -> None:
        """``WIDE`` must genuinely mean "many" — not two or three."""
        assert RecoveryBudget.WIDE.value >= 5

    def test_single_is_exactly_one(self) -> None:
        """The human gate is a single decision, by definition."""
        assert RecoveryBudget.SINGLE.value == 1

    def test_budget_is_int_enum(self) -> None:
        """Budget labels must be usable as ints directly (attempt counts)."""
        assert int(RecoveryBudget.WIDE) == RecoveryBudget.WIDE.value
        assert isinstance(RecoveryBudget.WIDE.value, int)


# ---------------------------------------------------------------------------
# AUDIO_PERMISSIVE_BUDGETS — the canonical audio ladder mapping
# ---------------------------------------------------------------------------

class TestAudioPermissiveBudgetsMapping:
    """The per-tier budget mapping for the permissive audio ladder."""

    def test_covers_every_recovery_level(self) -> None:
        """L0–L4 all present; missing a tier would silently fall back."""
        assert set(AUDIO_PERMISSIVE_BUDGETS.keys()) == {
            RecoveryLevel.FIX,
            RecoveryLevel.RETRY,
            RecoveryLevel.CREATIVE,
            RecoveryLevel.COLLABORATIVE,
            RecoveryLevel.HUMAN,
        }

    @pytest.mark.parametrize(
        "level,expected_label",
        [
            (RecoveryLevel.FIX, RecoveryBudget.WIDE),
            (RecoveryLevel.RETRY, RecoveryBudget.GENEROUS),
            (RecoveryLevel.CREATIVE, RecoveryBudget.NARROW_MULTI),
            (RecoveryLevel.COLLABORATIVE, RecoveryBudget.BOUNDED),
            (RecoveryLevel.HUMAN, RecoveryBudget.SINGLE),
        ],
    )
    def test_each_tier_gets_its_canonical_label(
        self, level: RecoveryLevel, expected_label: RecoveryBudget,
    ) -> None:
        """Per issue #144 / diagrams 2 + 4, every tier has a fixed label."""
        assert AUDIO_PERMISSIVE_BUDGETS[level] is expected_label


# ---------------------------------------------------------------------------
# RecoveryPolicy — label-aware budget resolution
# ---------------------------------------------------------------------------

class TestRecoveryPolicyBudgetLabels:
    """``RecoveryPolicy`` must surface budget labels and resolve attempts."""

    def test_get_level_budget_uses_labels_when_no_numeric_override(self) -> None:
        policy = RecoveryPolicy(
            level_budget_labels={
                RecoveryLevel.FIX: RecoveryBudget.WIDE,
                RecoveryLevel.RETRY: RecoveryBudget.GENEROUS,
            },
        )
        assert policy.get_level_budget(RecoveryLevel.FIX) == RecoveryBudget.WIDE.value
        assert policy.get_level_budget(RecoveryLevel.RETRY) == RecoveryBudget.GENEROUS.value

    def test_numeric_level_budgets_override_labels(self) -> None:
        """Explicit numeric overrides win over labels (escape hatch)."""
        policy = RecoveryPolicy(
            level_budgets={RecoveryLevel.FIX: 99},
            level_budget_labels={RecoveryLevel.FIX: RecoveryBudget.WIDE},
        )
        assert policy.get_level_budget(RecoveryLevel.FIX) == 99

    def test_get_level_budget_label_returns_label(self) -> None:
        policy = RecoveryPolicy(
            level_budget_labels={RecoveryLevel.CREATIVE: RecoveryBudget.NARROW_MULTI},
        )
        assert policy.get_level_budget_label(RecoveryLevel.CREATIVE) is RecoveryBudget.NARROW_MULTI

    def test_get_level_budget_label_returns_none_when_unset(self) -> None:
        policy = RecoveryPolicy()
        assert policy.get_level_budget_label(RecoveryLevel.FIX) is None

    def test_defaults_preserved_when_no_labels_and_no_overrides(self) -> None:
        """Legacy behaviour must be untouched for policies that don't opt in."""
        policy = RecoveryPolicy()
        assert policy.get_level_budget(RecoveryLevel.FIX) == 5
        assert policy.get_level_budget(RecoveryLevel.RETRY) == 3
        assert policy.get_level_budget(RecoveryLevel.CREATIVE) == 2
        assert policy.get_level_budget(RecoveryLevel.COLLABORATIVE) == 1


# ---------------------------------------------------------------------------
# Audio agent policy — end-to-end wiring
# ---------------------------------------------------------------------------

class TestAudioAgentPolicyWiring:
    """``_make_audio_agent_policy`` must expose the permissive labels."""

    @pytest.fixture
    def audio_policy(self) -> RecoveryPolicy:
        return _make_audio_agent_policy()

    def test_audio_policy_carries_budget_labels(
        self, audio_policy: RecoveryPolicy,
    ) -> None:
        assert audio_policy.level_budget_labels is not None
        # Mutating the returned policy must not affect the module-level
        # canonical mapping.
        assert audio_policy.level_budget_labels is not AUDIO_PERMISSIVE_BUDGETS

    @pytest.mark.parametrize(
        "level,expected_label",
        [
            (RecoveryLevel.FIX, RecoveryBudget.WIDE),
            (RecoveryLevel.RETRY, RecoveryBudget.GENEROUS),
            (RecoveryLevel.CREATIVE, RecoveryBudget.NARROW_MULTI),
            (RecoveryLevel.COLLABORATIVE, RecoveryBudget.BOUNDED),
            (RecoveryLevel.HUMAN, RecoveryBudget.SINGLE),
        ],
    )
    def test_audio_policy_labels_match_canonical_mapping(
        self,
        audio_policy: RecoveryPolicy,
        level: RecoveryLevel,
        expected_label: RecoveryBudget,
    ) -> None:
        assert audio_policy.get_level_budget_label(level) is expected_label

    @pytest.mark.parametrize(
        "level,expected_attempts",
        [
            (RecoveryLevel.FIX, RecoveryBudget.WIDE.value),
            (RecoveryLevel.RETRY, RecoveryBudget.GENEROUS.value),
            (RecoveryLevel.CREATIVE, RecoveryBudget.NARROW_MULTI.value),
            (RecoveryLevel.COLLABORATIVE, RecoveryBudget.BOUNDED.value),
        ],
    )
    def test_audio_policy_attempt_counts_match_labels(
        self,
        audio_policy: RecoveryPolicy,
        level: RecoveryLevel,
        expected_attempts: int,
    ) -> None:
        assert audio_policy.get_level_budget(level) == expected_attempts

    def test_audio_l0_gets_more_attempts_than_l1(
        self, audio_policy: RecoveryPolicy,
    ) -> None:
        """Issue #144 acceptance: ``many attempts before L1 entry``."""
        l0 = audio_policy.get_level_budget(RecoveryLevel.FIX)
        l1 = audio_policy.get_level_budget(RecoveryLevel.RETRY)
        assert l0 > l1

    def test_audio_l0_is_wide_strictly_greater_than_old_default(
        self, audio_policy: RecoveryPolicy,
    ) -> None:
        """L0 WIDE must be more permissive than the generic default of 5.

        The whole point of diagram 2 is that audio L0 is unusually wide;
        if WIDE collapses to the generic default the asymmetry with the
        strict video ladder (diagram 3) disappears.
        """
        assert audio_policy.get_level_budget(RecoveryLevel.FIX) > 5

    def test_audio_attempt_counts_are_monotone_decreasing(
        self, audio_policy: RecoveryPolicy,
    ) -> None:
        """Each climb up the ladder must get stricter, never looser."""
        budgets = [
            audio_policy.get_level_budget(level)
            for level in (
                RecoveryLevel.FIX,
                RecoveryLevel.RETRY,
                RecoveryLevel.CREATIVE,
                RecoveryLevel.COLLABORATIVE,
            )
        ]
        assert budgets == sorted(budgets, reverse=True)
        assert len(set(budgets)) == len(budgets)  # all distinct

    def test_audio_policy_still_escalates_to_human(
        self, audio_policy: RecoveryPolicy,
    ) -> None:
        """L4 gate remains enabled — we never silently drop failing audio."""
        assert audio_policy.escalate_to_human is True


# ---------------------------------------------------------------------------
# Audio vs. video asymmetry — diagram 4
# ---------------------------------------------------------------------------

class TestAudioVideoBudgetAsymmetry:
    """Diagram 4: audio permissive vs. video strict; the two must not match."""

    def test_audio_l0_is_strictly_wider_than_video_l0(self) -> None:
        audio = _make_audio_agent_policy()
        video = _make_video_agent_policy()
        assert audio.get_level_budget(RecoveryLevel.FIX) > video.get_level_budget(
            RecoveryLevel.FIX
        )

    def test_audio_l1_is_strictly_wider_than_video_l1(self) -> None:
        audio = _make_audio_agent_policy()
        video = _make_video_agent_policy()
        assert audio.get_level_budget(RecoveryLevel.RETRY) > video.get_level_budget(
            RecoveryLevel.RETRY
        )

    def test_video_policy_has_no_permissive_budget_labels(self) -> None:
        """The permissive label set is audio-only; D2 handles video strictly."""
        video = _make_video_agent_policy()
        # Either no labels at all, or labels that don't claim WIDE/GENEROUS
        # at the low tiers.  The strict video ladder is D2's job, not D1's.
        if video.level_budget_labels:
            assert video.level_budget_labels.get(RecoveryLevel.FIX) is not RecoveryBudget.WIDE
            assert video.level_budget_labels.get(RecoveryLevel.RETRY) is not RecoveryBudget.GENEROUS
