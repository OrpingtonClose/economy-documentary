"""Tests for ARCH-D2: video ladder strict one-shot-per-tier enforcement.

Pins the rule from diagram 3 of ``docs/ARCHITECTURE_DIAGRAMS.md``:

    STRICT RULE: one failure per level only.  Each tier (VL0 fix,
    VL1 retry, VL2 creative, VL3 collaborative, VL4 human) gets
    exactly one attempt.  A second failure at the same tier is not
    permitted -- escalate immediately to the next tier.

These tests exercise the agent-powered recovery ladder directly with
a stubbed ``RecoveryAgent`` so that we can count how many times the
underlying operation is called per tier and how many times each
agent's ``decide()`` method is invoked.  Infra-ladder behaviour is
orthogonal (ARCH-C) and is not exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from escalation_policy import (  # noqa: E402
    AUDIO_LADDER_CONFIG,
    VIDEO_LADDER_CONFIG,
    LadderDiscipline,
)
from recovery import (  # noqa: E402
    RecoveryBudget,
    RecoveryExhausted,
    RecoveryLevel,
    RecoveryPolicy,
    _execute_with_agents,
    _make_audio_agent_policy,
    _make_video_agent_policy,
)
from recovery_agents import RecoveryAgent, RecoveryContext, RecoveryDecision  # noqa: E402


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _StubAgent(RecoveryAgent):
    """A ``RecoveryAgent`` that skips the LLM and returns a canned
    ``RecoveryDecision``.  Records every ``decide()`` invocation so
    tests can assert exactly how many times a tier fired.
    """

    def __init__(
        self,
        name: str,
        action: str,
        state_patches: dict[str, Any] | None = None,
    ) -> None:
        # Do not call super().__init__ -- we want no LLM wiring.
        self.name = name
        self.instruction = ""
        self.tools = []
        self.model = "stub"
        self.max_tool_rounds = 0
        self._action = action
        self._state_patches = state_patches or {}
        self.decisions: list[RecoveryContext] = []

    def decide(self, context: RecoveryContext) -> RecoveryDecision:
        self.decisions.append(context)
        return RecoveryDecision(
            action=self._action,
            state_patches=dict(self._state_patches),
            explanation=f"stub agent '{self.name}' returning {self._action}",
            confidence=0.9,
        )


class _CountingOperation:
    """A callable that records how many times it was invoked and what
    kwargs it was called with, and always raises."""

    def __init__(self, error_cls: type[Exception] = RuntimeError) -> None:
        self.calls: list[dict] = []
        self._error_cls = error_cls

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        raise self._error_cls(f"operation failed (call #{len(self.calls)})")


def _video_policy_with_stubs(
    agents_by_level: dict[int, _StubAgent],
) -> RecoveryPolicy:
    """Build a video-strict policy carrying stub agents."""
    policy = _make_video_agent_policy()
    policy.agents = dict(agents_by_level)
    policy.escalate_to_human = False  # don't block on AG-UI in tests
    return policy


# ---------------------------------------------------------------------------
# Budget numbers are clamped to 1 at every tier for the video ladder
# ---------------------------------------------------------------------------

class TestVideoPolicyBudgetsAreOneShot:
    """``get_level_budget`` returns 1 at every tier for the video ladder."""

    @pytest.mark.parametrize("level", list(RecoveryLevel))
    def test_every_tier_budget_is_one(self, level: RecoveryLevel) -> None:
        policy = _make_video_agent_policy()
        assert policy.get_level_budget(level) == 1

    @pytest.mark.parametrize("level", list(RecoveryLevel))
    def test_every_tier_label_is_single(self, level: RecoveryLevel) -> None:
        policy = _make_video_agent_policy()
        assert policy.get_level_budget_label(level) is RecoveryBudget.SINGLE

    def test_strict_discipline_is_detected(self) -> None:
        policy = _make_video_agent_policy()
        assert policy._is_strict_one_shot() is True

    def test_ladder_config_is_strict(self) -> None:
        policy = _make_video_agent_policy()
        assert policy.ladder_config is VIDEO_LADDER_CONFIG
        assert policy.ladder_config.discipline == LadderDiscipline.STRICT_ONE_SHOT


# ---------------------------------------------------------------------------
# Runtime one-shot enforcement via _execute_with_agents
# ---------------------------------------------------------------------------

class TestStrictOneShotRuntime:
    """Exercise the full agent ladder and count per-tier invocations."""

    def _run(self, policy: RecoveryPolicy) -> tuple[_CountingOperation, Exception | None]:
        op = _CountingOperation()
        err: Exception | None = None
        try:
            _execute_with_agents(
                operation=op,
                operation_name="test_video_op",
                kwargs={"seed": 1},
                policy=policy,
                context=None,
                pipeline_state={},
                diagnostic_data={},
            )
        except Exception as e:  # noqa: BLE001 -- we want to see every failure type
            err = e
        return op, err

    def test_fix_fails_once_per_tier_then_escalates(self) -> None:
        """When every tier's agent says ``fix`` and every re-run
        fails, each tier must still only re-run the operation ONCE,
        and the ladder must exhaust (RecoveryExhausted)."""
        stub_l0 = _StubAgent("L0-fix", action="fix", state_patches={"seed": 2})
        stub_l1 = _StubAgent("L1-fix", action="fix", state_patches={"seed": 3})
        stub_l2 = _StubAgent("L2-fix", action="fix", state_patches={"seed": 4})
        stub_l3 = _StubAgent("L3-fix", action="fix", state_patches={"seed": 5})

        policy = _video_policy_with_stubs({
            0: stub_l0,
            1: stub_l1,
            2: stub_l2,
            3: stub_l3,
        })

        op, err = self._run(policy)

        # Each agent must have been consulted exactly once per tier.
        assert len(stub_l0.decisions) == 1, "L0 must fire exactly once"
        assert len(stub_l1.decisions) == 1, "L1 must fire exactly once"
        assert len(stub_l2.decisions) == 1, "L2 must fire exactly once"
        assert len(stub_l3.decisions) == 1, "L3 must fire exactly once"

        # Operation calls: 1 initial + 1 per fix tier (4) = 5 max
        # -- i.e. every tier gets EXACTLY ONE re-run attempt, not more.
        # (initial call + one retry per tier)
        assert len(op.calls) == 5, (
            f"Strict one-shot: expected exactly 5 operation calls "
            f"(initial + one per tier L0..L3), got {len(op.calls)}"
        )

        # Ladder must have exhausted -- no silent success, no swallow.
        assert err is not None
        assert isinstance(err, RecoveryExhausted)

    def test_retry_action_also_runs_only_once_per_tier(self) -> None:
        """``retry`` is the other action that re-invokes the operation;
        it must also be capped at one attempt per tier under strict."""
        stub_l0 = _StubAgent("L0-retry", action="retry")
        stub_l1 = _StubAgent("L1-retry", action="retry")
        stub_l2 = _StubAgent("L2-retry", action="retry")
        stub_l3 = _StubAgent("L3-retry", action="retry")

        policy = _video_policy_with_stubs({
            0: stub_l0,
            1: stub_l1,
            2: stub_l2,
            3: stub_l3,
        })
        # Shorten backoff so the test doesn't wait on exponential sleeps.
        policy.retry_backoff_base = 0.0
        policy.retry_backoff_max = 0.0

        op, err = self._run(policy)

        # Each tier: agent fires once, operation is re-run once, then ladder
        # moves on regardless of outcome.
        assert len(stub_l0.decisions) == 1
        assert len(stub_l1.decisions) == 1
        assert len(stub_l2.decisions) == 1
        assert len(stub_l3.decisions) == 1
        assert len(op.calls) == 5  # 1 initial + 1 per retry tier
        assert isinstance(err, RecoveryExhausted)

    def test_escalate_short_circuits_within_tier(self) -> None:
        """When an agent escalates, the tier burns zero extra operation
        calls -- and (because strict) the ladder still only gets one
        shot at the next tier's agent."""
        stub_l0 = _StubAgent("L0-esc", action="escalate")
        stub_l1 = _StubAgent("L1-fix", action="fix", state_patches={"seed": 9})
        stub_l2 = _StubAgent("L2-esc", action="escalate")
        stub_l3 = _StubAgent("L3-esc", action="escalate")

        policy = _video_policy_with_stubs({
            0: stub_l0,
            1: stub_l1,
            2: stub_l2,
            3: stub_l3,
        })

        op, err = self._run(policy)

        # Initial call + L1's one fix re-run = 2; L0/L2/L3 escalate
        # without running the op.
        assert len(op.calls) == 2
        # Each tier's agent still fires at most once.
        assert len(stub_l0.decisions) == 1
        assert len(stub_l1.decisions) == 1
        assert len(stub_l2.decisions) == 1
        assert len(stub_l3.decisions) == 1
        assert isinstance(err, RecoveryExhausted)

    def test_no_tier_ever_gets_a_second_attempt(self) -> None:
        """Meta-assertion: across all agents, no single tier triggers
        the operation more than once."""
        stub_l0 = _StubAgent("L0-fix", action="fix")
        stub_l1 = _StubAgent("L1-fix", action="fix")
        stub_l2 = _StubAgent("L2-fix", action="fix")
        stub_l3 = _StubAgent("L3-fix", action="fix")

        policy = _video_policy_with_stubs({
            0: stub_l0,
            1: stub_l1,
            2: stub_l2,
            3: stub_l3,
        })

        op, err = self._run(policy)

        # Every tier's agent fired once (inner loop is range(1, 2)).
        for stub in (stub_l0, stub_l1, stub_l2, stub_l3):
            assert len(stub.decisions) == 1, (
                f"{stub.name} was invoked {len(stub.decisions)} times; "
                "strict one-shot allows only one."
            )
        assert err is not None


# ---------------------------------------------------------------------------
# Numeric overrides cannot loosen the strict ladder
# ---------------------------------------------------------------------------

class TestStrictCannotBeLoosened:
    """Even if a caller overrides ``level_budgets`` or
    ``level_budget_labels``, the strict discipline still clamps to 1.
    """

    def test_numeric_override_does_not_grant_more_attempts(self) -> None:
        stub_l0 = _StubAgent("L0-fix", action="fix")
        policy = RecoveryPolicy(
            agents={0: stub_l0},
            ladder_config=VIDEO_LADDER_CONFIG,
            level_budgets={0: 5, 1: 5, 2: 5, 3: 5, 4: 5},
            escalate_to_human=False,
        )
        op = _CountingOperation()
        with pytest.raises(RecoveryExhausted):
            _execute_with_agents(
                operation=op,
                operation_name="op",
                kwargs={},
                policy=policy,
            )
        # Exactly 1 initial + 1 L0 fix = 2 operation calls.
        assert len(op.calls) == 2
        assert len(stub_l0.decisions) == 1

    def test_label_override_does_not_grant_more_attempts(self) -> None:
        stub_l0 = _StubAgent("L0-fix", action="fix")
        policy = RecoveryPolicy(
            agents={0: stub_l0},
            ladder_config=VIDEO_LADDER_CONFIG,
            level_budget_labels={
                RecoveryLevel.FIX: RecoveryBudget.WIDE,
                RecoveryLevel.RETRY: RecoveryBudget.GENEROUS,
            },
            escalate_to_human=False,
        )
        op = _CountingOperation()
        with pytest.raises(RecoveryExhausted):
            _execute_with_agents(
                operation=op,
                operation_name="op",
                kwargs={},
                policy=policy,
            )
        assert len(op.calls) == 2
        assert len(stub_l0.decisions) == 1


# ---------------------------------------------------------------------------
# Audio ladder is NOT clamped (asymmetric with video)
# ---------------------------------------------------------------------------

class TestAudioLadderStillPermissive:
    """Regression guard: the strict clamp MUST NOT leak onto the audio
    ladder.  Audio L0 should still get RecoveryBudget.WIDE attempts.
    """

    def test_audio_l0_keeps_wide_budget(self) -> None:
        policy = _make_audio_agent_policy()
        assert policy._is_strict_one_shot() is False
        assert policy.get_level_budget(RecoveryLevel.FIX) == int(
            RecoveryBudget.WIDE
        )

    def test_audio_policy_allows_multiple_fix_attempts_at_l0(self) -> None:
        """With a WIDE budget, an L0 stub that says ``fix`` every time
        should be consulted repeatedly until either the op succeeds or
        the budget is exhausted.  This pins the asymmetry at runtime."""
        stub_l0 = _StubAgent("audio-L0", action="fix")
        # Only wire L0 so we don't run the whole ladder.
        policy = _make_audio_agent_policy()
        policy.agents = {0: stub_l0}
        policy.escalate_to_human = False

        op = _CountingOperation()
        with pytest.raises(RecoveryExhausted):
            _execute_with_agents(
                operation=op,
                operation_name="audio_op",
                kwargs={},
                policy=policy,
            )

        # WIDE = 8 attempts allowed at L0 -> agent should have fired 8
        # times at that tier.  Operation is called 1 (initial) + 8
        # (per-attempt) = 9 times.
        assert len(stub_l0.decisions) == int(RecoveryBudget.WIDE), (
            f"Audio L0 should consult its agent WIDE ({int(RecoveryBudget.WIDE)}) "
            f"times; got {len(stub_l0.decisions)}"
        )
        assert len(op.calls) == 1 + int(RecoveryBudget.WIDE)


# ---------------------------------------------------------------------------
# Defensive assertion: tampering with budget by bypassing get_level_budget
# ---------------------------------------------------------------------------

class TestStrictDefensiveAssertion:
    """If a future refactor ever bypasses ``get_level_budget`` and
    passes a non-1 budget to the strict loop, the loop must raise --
    never silently permit second attempts."""

    def test_strict_policy_with_coerced_budget_method_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_l0 = _StubAgent("L0", action="fix")
        policy = _video_policy_with_stubs({0: stub_l0})

        # Replace get_level_budget with a pathological version that
        # returns 3 despite the strict discipline still being on.  The
        # defensive assertion in _execute_with_agents must reject it.
        monkeypatch.setattr(
            policy, "get_level_budget", lambda level: 3,
        )
        op = _CountingOperation()
        with pytest.raises(RuntimeError, match="ARCH-D2 violation"):
            _execute_with_agents(
                operation=op,
                operation_name="op",
                kwargs={},
                policy=policy,
            )
