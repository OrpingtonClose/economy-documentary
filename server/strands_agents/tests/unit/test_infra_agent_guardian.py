"""Unit tests for the guardian decision core."""

from __future__ import annotations

import pytest

from strands_agents.infra_agent.guardian import (
    DEFAULT_IDLE_SECONDS,
    DEFAULT_MAX_LIFETIME_SECONDS,
    GuardianConfig,
    GuardianState,
    remaining_s,
    should_destroy,
)


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig(idle_budget_s=60, max_lifetime_budget_s=3600)


@pytest.fixture
def state() -> GuardianState:
    return GuardianState(boot_ts=1_000.0, last_bump_ts=1_000.0)


def test_defaults_are_sane() -> None:
    cfg = GuardianConfig()
    assert cfg.idle_budget_s == DEFAULT_IDLE_SECONDS
    assert cfg.max_lifetime_budget_s == DEFAULT_MAX_LIFETIME_SECONDS


def test_config_rejects_non_positive_idle() -> None:
    with pytest.raises(ValueError, match="idle_budget_s must be positive"):
        GuardianConfig(idle_budget_s=0, max_lifetime_budget_s=100)


def test_config_rejects_non_positive_lifetime() -> None:
    with pytest.raises(ValueError, match="max_lifetime_budget_s must be positive"):
        GuardianConfig(idle_budget_s=10, max_lifetime_budget_s=0)


def test_config_rejects_lifetime_under_idle() -> None:
    with pytest.raises(ValueError, match="must be >= idle_budget_s"):
        GuardianConfig(idle_budget_s=120, max_lifetime_budget_s=60)


def test_alive_when_within_both_budgets(
    state: GuardianState, config: GuardianConfig
) -> None:
    decision = should_destroy(state=state, config=config, now=1_030.0)
    assert decision.reason is None
    assert decision.should_destroy is False
    assert decision.idle_elapsed_s == pytest.approx(30.0)
    assert decision.lifetime_elapsed_s == pytest.approx(30.0)


def test_idle_trigger_fires_at_exact_budget(
    state: GuardianState, config: GuardianConfig
) -> None:
    decision = should_destroy(state=state, config=config, now=1_060.0)
    assert decision.reason == "idle"
    assert decision.should_destroy is True


def test_idle_trigger_fires_past_budget(
    state: GuardianState, config: GuardianConfig
) -> None:
    decision = should_destroy(state=state, config=config, now=1_061.0)
    assert decision.reason == "idle"


def test_lifetime_trigger_fires_when_exceeded(
    state: GuardianState, config: GuardianConfig
) -> None:
    state.last_bump_ts = 4_590.0
    decision = should_destroy(state=state, config=config, now=4_600.1)
    assert decision.reason == "lifetime"


def test_manual_destroy_takes_precedence_over_budgets(
    state: GuardianState, config: GuardianConfig
) -> None:
    state.request_manual_destroy()
    decision = should_destroy(state=state, config=config, now=1_005.0)
    assert decision.reason == "manual"


def test_manual_destroy_wins_even_when_idle_also_expired(
    state: GuardianState, config: GuardianConfig
) -> None:
    state.request_manual_destroy()
    decision = should_destroy(state=state, config=config, now=5_000.0)
    assert decision.reason == "manual"


def test_lifetime_beats_idle_when_both_expired(
    state: GuardianState, config: GuardianConfig
) -> None:
    state.last_bump_ts = 4_500.0
    decision = should_destroy(state=state, config=config, now=4_700.0)
    assert decision.reason == "lifetime"


def test_bump_moves_last_bump_forward(state: GuardianState) -> None:
    state.bump(1_050.0)
    assert state.last_bump_ts == 1_050.0


def test_bump_never_moves_backwards(state: GuardianState) -> None:
    state.bump(1_100.0)
    state.bump(1_000.0)
    assert state.last_bump_ts == 1_100.0


def test_request_manual_destroy_is_idempotent(state: GuardianState) -> None:
    state.request_manual_destroy()
    state.request_manual_destroy()
    assert state.manual_destroy_requested is True


def test_remaining_s_positive_before_either_budget_hits(
    state: GuardianState, config: GuardianConfig
) -> None:
    idle_rem, lifetime_rem = remaining_s(state=state, config=config, now=1_030.0)
    assert idle_rem == pytest.approx(30.0)
    assert lifetime_rem == pytest.approx(3_570.0)


def test_remaining_s_negative_past_budget(
    state: GuardianState, config: GuardianConfig
) -> None:
    idle_rem, _ = remaining_s(state=state, config=config, now=1_100.0)
    assert idle_rem == pytest.approx(-40.0)


def test_elapsed_counters_clamp_at_zero(
    state: GuardianState, config: GuardianConfig
) -> None:
    decision = should_destroy(state=state, config=config, now=500.0)
    assert decision.idle_elapsed_s == 0.0
    assert decision.lifetime_elapsed_s == 0.0
