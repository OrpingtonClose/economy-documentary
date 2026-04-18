"""Tests for ARCH-D3: shared escalation-policy config (typed).

Pins the shape and invariants of ``server/escalation_policy.py``:

* Every content ladder is represented as a ``LadderBudgetConfig``.
* Discipline is ``PERMISSIVE`` or ``STRICT_ONE_SHOT`` (no other values).
* Configs are immutable (frozen dataclass + MappingProxy budgets).
* Coverage: every ``RecoveryLevel`` (L0-L4) has an explicit budget.
* Shape validation: STRICT_ONE_SHOT configs refuse non-SINGLE budgets
  -- this is what stops someone silently reintroducing a multi-shot
  video ladder (diagram 3).
* Monotone: per-tier budgets shrink as the ladder climbs.
* Both ``_make_audio_agent_policy`` and ``_make_video_agent_policy``
  read budgets from the shared config, not from hardcoded numbers.

These tests assert the **config shape is honoured at runtime** per the
ARCH-D3 acceptance criteria (issue #146).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from escalation_policy import (  # noqa: E402
    AUDIO_LADDER_CONFIG,
    LADDER_CONFIGS,
    VIDEO_LADDER_CONFIG,
    LadderBudgetConfig,
    LadderConfigError,
    LadderDiscipline,
    STRICT_DISCIPLINE_VALUE,
    get_ladder_config,
)
from recovery import (  # noqa: E402
    AUDIO_PERMISSIVE_BUDGETS,
    RecoveryBudget,
    RecoveryLevel,
    RecoveryPolicy,
    _make_audio_agent_policy,
    _make_video_agent_policy,
)


# ---------------------------------------------------------------------------
# LadderDiscipline enum shape
# ---------------------------------------------------------------------------

class TestLadderDisciplineEnum:
    """The canonical discipline vocabulary for D3."""

    def test_only_two_disciplines_exist(self) -> None:
        """PERMISSIVE and STRICT_ONE_SHOT -- and nothing else.

        A third value would mean a third discipline the ladders in
        ``recovery.py`` don't know how to honour; keep the set small
        and explicit.
        """
        assert {m.name for m in LadderDiscipline} == {
            "PERMISSIVE",
            "STRICT_ONE_SHOT",
        }

    def test_string_enum(self) -> None:
        """Each discipline carries a stable string value used for
        duck-typing in ``recovery.py`` (avoids circular imports)."""
        assert LadderDiscipline.PERMISSIVE.value == "permissive"
        assert LadderDiscipline.STRICT_ONE_SHOT.value == "strict_one_shot"

    def test_strict_discipline_string_constant_matches(self) -> None:
        """``STRICT_DISCIPLINE_VALUE`` is the wire format recovery.py
        compares against; it MUST equal the enum value or the runtime
        strict-one-shot check silently fails."""
        assert STRICT_DISCIPLINE_VALUE == LadderDiscipline.STRICT_ONE_SHOT.value


# ---------------------------------------------------------------------------
# LadderBudgetConfig shape + invariants
# ---------------------------------------------------------------------------

class TestLadderBudgetConfigShape:
    """Invariants enforced by ``LadderBudgetConfig.__post_init__``."""

    def _full_budgets(
        self,
        audio: bool = True,
    ) -> dict[RecoveryLevel, RecoveryBudget]:
        if audio:
            return {
                RecoveryLevel.FIX: RecoveryBudget.WIDE,
                RecoveryLevel.RETRY: RecoveryBudget.GENEROUS,
                RecoveryLevel.CREATIVE: RecoveryBudget.NARROW_MULTI,
                RecoveryLevel.COLLABORATIVE: RecoveryBudget.BOUNDED,
                RecoveryLevel.HUMAN: RecoveryBudget.SINGLE,
            }
        return {level: RecoveryBudget.SINGLE for level in RecoveryLevel}

    def test_requires_every_recovery_level(self) -> None:
        """Missing L0-L4 would silently fall back during recovery."""
        partial = {
            RecoveryLevel.FIX: RecoveryBudget.WIDE,
            RecoveryLevel.RETRY: RecoveryBudget.GENEROUS,
        }
        with pytest.raises(LadderConfigError, match="missing budgets"):
            LadderBudgetConfig(
                ladder_id="broken",
                medium="test",
                discipline=LadderDiscipline.PERMISSIVE,
                budgets=partial,
            )

    def test_rejects_non_recovery_level_keys(self) -> None:
        bad = dict(self._full_budgets())
        bad[999] = RecoveryBudget.SINGLE  # type: ignore[index]
        with pytest.raises(LadderConfigError, match="budget key"):
            LadderBudgetConfig(
                ladder_id="broken",
                medium="test",
                discipline=LadderDiscipline.PERMISSIVE,
                budgets=bad,  # type: ignore[arg-type]
            )

    def test_rejects_non_recovery_budget_values(self) -> None:
        bad = dict(self._full_budgets())
        bad[RecoveryLevel.FIX] = 5  # type: ignore[assignment]
        with pytest.raises(LadderConfigError, match="budget value"):
            LadderBudgetConfig(
                ladder_id="broken",
                medium="test",
                discipline=LadderDiscipline.PERMISSIVE,
                budgets=bad,  # type: ignore[arg-type]
            )

    def test_requires_monotone_non_increasing_budgets(self) -> None:
        """Budgets must shrink (or equal) as the ladder climbs."""
        bad = {
            RecoveryLevel.FIX: RecoveryBudget.SINGLE,
            RecoveryLevel.RETRY: RecoveryBudget.WIDE,  # inverted
            RecoveryLevel.CREATIVE: RecoveryBudget.NARROW_MULTI,
            RecoveryLevel.COLLABORATIVE: RecoveryBudget.BOUNDED,
            RecoveryLevel.HUMAN: RecoveryBudget.SINGLE,
        }
        with pytest.raises(LadderConfigError, match="non-monotone"):
            LadderBudgetConfig(
                ladder_id="broken",
                medium="test",
                discipline=LadderDiscipline.PERMISSIVE,
                budgets=bad,
            )

    def test_strict_discipline_refuses_non_single_budget(self) -> None:
        """Diagram 3: STRICT_ONE_SHOT means every tier is SINGLE."""
        bad = dict(self._full_budgets(audio=True))  # WIDE/GENEROUS/...
        with pytest.raises(LadderConfigError, match="STRICT_ONE_SHOT"):
            LadderBudgetConfig(
                ladder_id="broken",
                medium="video",
                discipline=LadderDiscipline.STRICT_ONE_SHOT,
                budgets=bad,
            )

    def test_strict_discipline_refuses_even_one_non_single(self) -> None:
        """Any single tier with a non-SINGLE budget breaks the rule."""
        bad = {level: RecoveryBudget.SINGLE for level in RecoveryLevel}
        bad[RecoveryLevel.FIX] = RecoveryBudget.BOUNDED  # cheat
        with pytest.raises(LadderConfigError, match="STRICT_ONE_SHOT"):
            LadderBudgetConfig(
                ladder_id="broken",
                medium="video",
                discipline=LadderDiscipline.STRICT_ONE_SHOT,
                budgets=bad,
            )

    def test_permissive_discipline_accepts_single_everywhere(self) -> None:
        """Permissive can still use all-SINGLE if it wants (degenerate
        but valid); the discipline test is asymmetric."""
        cfg = LadderBudgetConfig(
            ladder_id="degenerate",
            medium="test",
            discipline=LadderDiscipline.PERMISSIVE,
            budgets={level: RecoveryBudget.SINGLE for level in RecoveryLevel},
        )
        assert cfg.discipline == LadderDiscipline.PERMISSIVE

    def test_config_is_frozen_dataclass(self) -> None:
        """Canonical configs must not be mutable at runtime."""
        with pytest.raises((AttributeError, TypeError)):
            AUDIO_LADDER_CONFIG.medium = "other"  # type: ignore[misc]

    def test_budgets_mapping_is_read_only(self) -> None:
        """The budgets mapping is wrapped in ``MappingProxyType`` so
        callers cannot mutate canonical config in place."""
        with pytest.raises(TypeError):
            AUDIO_LADDER_CONFIG.budgets[RecoveryLevel.FIX] = (  # type: ignore[index]
                RecoveryBudget.SINGLE
            )

    def test_rejects_empty_ladder_id(self) -> None:
        with pytest.raises(LadderConfigError, match="ladder_id"):
            LadderBudgetConfig(
                ladder_id="",
                medium="audio",
                discipline=LadderDiscipline.PERMISSIVE,
                budgets=self._full_budgets(),
            )

    def test_rejects_empty_medium(self) -> None:
        with pytest.raises(LadderConfigError, match="medium"):
            LadderBudgetConfig(
                ladder_id="x",
                medium="",
                discipline=LadderDiscipline.PERMISSIVE,
                budgets=self._full_budgets(),
            )


# ---------------------------------------------------------------------------
# Canonical AUDIO_LADDER_CONFIG
# ---------------------------------------------------------------------------

class TestAudioLadderConfig:
    """The shared audio config must match ARCH-D1 (diagrams 2 + 4)."""

    def test_medium_is_audio(self) -> None:
        assert AUDIO_LADDER_CONFIG.medium == "audio"

    def test_discipline_is_permissive(self) -> None:
        assert AUDIO_LADDER_CONFIG.discipline == LadderDiscipline.PERMISSIVE
        assert not AUDIO_LADDER_CONFIG.is_strict_one_shot()

    def test_covers_every_level(self) -> None:
        assert set(AUDIO_LADDER_CONFIG.budgets.keys()) == set(RecoveryLevel)

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
    def test_tier_labels_match_diagram_2(
        self, level: RecoveryLevel, expected_label: RecoveryBudget,
    ) -> None:
        assert AUDIO_LADDER_CONFIG.label_for(level) is expected_label

    def test_matches_d1_canonical_mapping(self) -> None:
        """The audio config must agree with the D1 ``AUDIO_PERMISSIVE_BUDGETS``
        mapping -- D3 extends D1, it does not replace it."""
        assert dict(AUDIO_LADDER_CONFIG.budgets) == dict(AUDIO_PERMISSIVE_BUDGETS)

    def test_attempts_for_returns_int_count(self) -> None:
        assert AUDIO_LADDER_CONFIG.attempts_for(RecoveryLevel.FIX) == int(
            RecoveryBudget.WIDE
        )


# ---------------------------------------------------------------------------
# Canonical VIDEO_LADDER_CONFIG
# ---------------------------------------------------------------------------

class TestVideoLadderConfig:
    """The shared video config must match ARCH-D2 (diagram 3)."""

    def test_medium_is_video(self) -> None:
        assert VIDEO_LADDER_CONFIG.medium == "video"

    def test_discipline_is_strict_one_shot(self) -> None:
        assert VIDEO_LADDER_CONFIG.discipline == LadderDiscipline.STRICT_ONE_SHOT
        assert VIDEO_LADDER_CONFIG.is_strict_one_shot()

    def test_covers_every_level(self) -> None:
        assert set(VIDEO_LADDER_CONFIG.budgets.keys()) == set(RecoveryLevel)

    @pytest.mark.parametrize("level", list(RecoveryLevel))
    def test_every_tier_is_single(self, level: RecoveryLevel) -> None:
        """Diagram 3: every tier gets exactly one attempt."""
        assert VIDEO_LADDER_CONFIG.label_for(level) is RecoveryBudget.SINGLE
        assert VIDEO_LADDER_CONFIG.attempts_for(level) == 1

    def test_no_tier_claims_wide_or_generous(self) -> None:
        """The permissive label vocabulary is audio-only."""
        for level, label in VIDEO_LADDER_CONFIG.budgets.items():
            assert label is not RecoveryBudget.WIDE, (
                f"Video tier {level.name} must not be WIDE"
            )
            assert label is not RecoveryBudget.GENEROUS, (
                f"Video tier {level.name} must not be GENEROUS"
            )


# ---------------------------------------------------------------------------
# Audio-vs-video asymmetry (diagram 4)
# ---------------------------------------------------------------------------

class TestLadderAsymmetry:
    """The whole point of ARCH-D: the two ladders must differ by design."""

    def test_audio_l0_strictly_wider_than_video_l0(self) -> None:
        assert AUDIO_LADDER_CONFIG.attempts_for(
            RecoveryLevel.FIX
        ) > VIDEO_LADDER_CONFIG.attempts_for(RecoveryLevel.FIX)

    def test_audio_l1_strictly_wider_than_video_l1(self) -> None:
        assert AUDIO_LADDER_CONFIG.attempts_for(
            RecoveryLevel.RETRY
        ) > VIDEO_LADDER_CONFIG.attempts_for(RecoveryLevel.RETRY)

    def test_audio_is_more_attempts_than_video_at_every_low_tier(self) -> None:
        """Through L3; L4 is SINGLE on both by convention."""
        for level in (
            RecoveryLevel.FIX,
            RecoveryLevel.RETRY,
            RecoveryLevel.CREATIVE,
            RecoveryLevel.COLLABORATIVE,
        ):
            audio = AUDIO_LADDER_CONFIG.attempts_for(level)
            video = VIDEO_LADDER_CONFIG.attempts_for(level)
            assert audio > video, (
                f"Audio L{int(level)} ({audio}) must be > "
                f"video L{int(level)} ({video}) per diagram 4"
            )

    def test_disciplines_differ(self) -> None:
        assert AUDIO_LADDER_CONFIG.discipline != VIDEO_LADDER_CONFIG.discipline


# ---------------------------------------------------------------------------
# LADDER_CONFIGS registry + lookup helper
# ---------------------------------------------------------------------------

class TestLadderConfigsRegistry:
    """The registry must expose both content ladders by medium."""

    def test_has_audio_and_video(self) -> None:
        assert "audio" in LADDER_CONFIGS
        assert "video" in LADDER_CONFIGS

    def test_audio_registry_entry_is_the_canonical_config(self) -> None:
        assert LADDER_CONFIGS["audio"] is AUDIO_LADDER_CONFIG

    def test_video_registry_entry_is_the_canonical_config(self) -> None:
        assert LADDER_CONFIGS["video"] is VIDEO_LADDER_CONFIG

    def test_registry_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            LADDER_CONFIGS["audio"] = VIDEO_LADDER_CONFIG  # type: ignore[index]

    def test_get_ladder_config_returns_canonical(self) -> None:
        assert get_ladder_config("audio") is AUDIO_LADDER_CONFIG
        assert get_ladder_config("video") is VIDEO_LADDER_CONFIG

    def test_get_ladder_config_fails_loud_on_unknown_medium(self) -> None:
        with pytest.raises(LadderConfigError, match="No ladder config"):
            get_ladder_config("text")


# ---------------------------------------------------------------------------
# RecoveryPolicy ↔ LadderBudgetConfig wiring
# ---------------------------------------------------------------------------

class TestRecoveryPolicyReadsConfig:
    """Policies must derive per-tier budgets from the config at runtime."""

    def test_audio_policy_has_ladder_config_attached(self) -> None:
        policy = _make_audio_agent_policy()
        assert policy.ladder_config is AUDIO_LADDER_CONFIG

    def test_video_policy_has_ladder_config_attached(self) -> None:
        policy = _make_video_agent_policy()
        assert policy.ladder_config is VIDEO_LADDER_CONFIG

    def test_audio_policy_budgets_match_config(self) -> None:
        policy = _make_audio_agent_policy()
        for level, label in AUDIO_LADDER_CONFIG.budgets.items():
            assert policy.get_level_budget(level) == int(label)
            assert policy.get_level_budget_label(level) is label

    def test_video_policy_budgets_match_config(self) -> None:
        """Every tier of the video policy must have exactly 1 attempt."""
        policy = _make_video_agent_policy()
        for level in RecoveryLevel:
            assert policy.get_level_budget(level) == 1
            assert policy.get_level_budget_label(level) is RecoveryBudget.SINGLE

    def test_video_policy_has_no_hardcoded_multi_attempts(self) -> None:
        """Before ARCH-D2/D3 the video policy was hardcoded to
        ``{0: 3, 1: 3, 2: 2, 3: 1}``.  Those numbers must be gone."""
        policy = _make_video_agent_policy()
        assert policy.get_level_budget(RecoveryLevel.FIX) == 1
        assert policy.get_level_budget(RecoveryLevel.RETRY) == 1
        assert policy.get_level_budget(RecoveryLevel.CREATIVE) == 1
        assert policy.get_level_budget(RecoveryLevel.COLLABORATIVE) == 1

    def test_strict_one_shot_clamps_numeric_override(self) -> None:
        """Even if a caller tries to override budgets on a strict policy,
        ``get_level_budget`` must still return 1 -- the discipline is
        authoritative."""
        policy = RecoveryPolicy(
            ladder_config=VIDEO_LADDER_CONFIG,
            level_budgets={
                int(RecoveryLevel.FIX): 99,
                int(RecoveryLevel.RETRY): 99,
            },
        )
        assert policy.get_level_budget(RecoveryLevel.FIX) == 1
        assert policy.get_level_budget(RecoveryLevel.RETRY) == 1

    def test_strict_one_shot_clamps_label_override(self) -> None:
        """Label overrides are also ignored under STRICT_ONE_SHOT."""
        policy = RecoveryPolicy(
            ladder_config=VIDEO_LADDER_CONFIG,
            level_budget_labels={
                RecoveryLevel.FIX: RecoveryBudget.WIDE,
            },
        )
        assert policy.get_level_budget(RecoveryLevel.FIX) == 1

    def test_permissive_policy_respects_numeric_override(self) -> None:
        """Non-strict policies keep the D1 escape hatch."""
        policy = RecoveryPolicy(
            ladder_config=AUDIO_LADDER_CONFIG,
            level_budgets={int(RecoveryLevel.FIX): 99},
        )
        assert policy.get_level_budget(RecoveryLevel.FIX) == 99

    def test_ladder_config_alone_yields_consistent_budgets(self) -> None:
        """Regression guard (Devin Review #176): a policy that only has
        ``ladder_config`` set (no ``level_budget_labels``, no numeric
        overrides) must return identical budgets from
        ``get_level_budget`` and ``get_level_budget_label``.  Previously
        the two methods disagreed -- labels read from the config but
        numbers fell through to hard-coded defaults."""
        policy = RecoveryPolicy(ladder_config=AUDIO_LADDER_CONFIG)
        for level, label in AUDIO_LADDER_CONFIG.budgets.items():
            assert policy.get_level_budget_label(level) is label
            assert policy.get_level_budget(level) == int(label), (
                f"ladder_config alone must drive get_level_budget at "
                f"L{int(level)}: expected {int(label)}, got "
                f"{policy.get_level_budget(level)}"
            )

    def test_numeric_override_still_beats_ladder_config(self) -> None:
        """The escape-hatch contract from D1 is preserved: an explicit
        numeric entry in ``level_budgets`` wins over ``ladder_config``."""
        policy = RecoveryPolicy(
            ladder_config=AUDIO_LADDER_CONFIG,
            level_budgets={int(RecoveryLevel.FIX): 42},
        )
        assert policy.get_level_budget(RecoveryLevel.FIX) == 42
        # Other tiers still come from the config.
        assert policy.get_level_budget(RecoveryLevel.RETRY) == int(
            AUDIO_LADDER_CONFIG.label_for(RecoveryLevel.RETRY)
        )

    def test_policy_without_config_preserves_legacy_defaults(self) -> None:
        """Regression guard: policies that don't opt in behave as before."""
        policy = RecoveryPolicy()
        assert policy.ladder_config is None
        assert policy._is_strict_one_shot() is False
        assert policy.get_level_budget(RecoveryLevel.FIX) == 5
        assert policy.get_level_budget(RecoveryLevel.RETRY) == 3
