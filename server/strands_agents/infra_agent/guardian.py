"""Guardian decision core — pure, deterministic, unit-testable.

The guardian decides when a VM should self-destruct. It is intentionally
a pure function over a small state object so every decision path can be
exercised without touching ``nvidia-smi``, the Vast.ai API, or the
playground registry.

Two destruction triggers:

* **Idle timeout** — no request hit the agent or the worker in the last
  ``idle_budget_s`` seconds. This is the cost-control primitive.
* **Lifetime ceiling** — the VM has been alive for more than
  ``max_lifetime_budget_s`` seconds regardless of traffic. This is the
  forget-a-VM-running safety net.

There is no override. A debugger keeps the VM alive by pinging the
agent periodically; an unattended VM dies. See
``docs/strands-migration/lessons/README.md`` for the guardian tuning
ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_IDLE_SECONDS: int = 900
DEFAULT_MAX_LIFETIME_SECONDS: int = 14400

#: Stable reason strings. The ledger in
#: ``docs/strands-migration/lessons/guardian-tuning.md`` expects these
#: exact values on every destruction row.
DestroyReason = Literal["idle", "lifetime", "manual", "crash"]


@dataclass(frozen=True)
class GuardianConfig:
    """Immutable tuning set at boot.

    Attributes:
        idle_budget_s: Seconds without a bump before the idle trigger
            fires. Must be > 0.
        max_lifetime_budget_s: Seconds since boot before the lifetime
            trigger fires. Must be >= ``idle_budget_s``.
    """

    idle_budget_s: int = DEFAULT_IDLE_SECONDS
    max_lifetime_budget_s: int = DEFAULT_MAX_LIFETIME_SECONDS

    def __post_init__(self) -> None:
        if self.idle_budget_s <= 0:
            raise ValueError(
                f"idle_budget_s must be positive, got {self.idle_budget_s}"
            )
        if self.max_lifetime_budget_s <= 0:
            raise ValueError(
                f"max_lifetime_budget_s must be positive, got {self.max_lifetime_budget_s}"
            )
        if self.max_lifetime_budget_s < self.idle_budget_s:
            raise ValueError(
                "max_lifetime_budget_s must be >= idle_budget_s "
                f"(got {self.max_lifetime_budget_s} < {self.idle_budget_s})"
            )


@dataclass
class GuardianState:
    """Mutable runtime state carried by the agent.

    Attributes:
        boot_ts: Unix timestamp of agent boot. Fixed for the VM's life.
        last_bump_ts: Unix timestamp of the most recent bump. Updated
            on every request hitting the agent or the wrapped worker.
        manual_destroy_requested: True once a ``/infra/destroy`` call
            has been accepted. Causes :func:`should_destroy` to return
            ``"manual"`` immediately.
    """

    boot_ts: float
    last_bump_ts: float
    manual_destroy_requested: bool = False

    def bump(self, now: float) -> None:
        """Mark the VM as active as of ``now``. Never moves backwards."""
        if now > self.last_bump_ts:
            self.last_bump_ts = now

    def request_manual_destroy(self) -> None:
        """Latch the manual-destroy flag. Idempotent."""
        self.manual_destroy_requested = True


@dataclass(frozen=True)
class GuardianDecision:
    """Result of :func:`should_destroy`.

    Attributes:
        reason: Why destruction should proceed, or ``None`` if the VM
            should stay alive.
        idle_elapsed_s: Seconds since the last bump at decision time.
        lifetime_elapsed_s: Seconds since boot at decision time.
    """

    reason: DestroyReason | None
    idle_elapsed_s: float
    lifetime_elapsed_s: float

    @property
    def should_destroy(self) -> bool:
        """Convenience flag mirroring ``reason is not None``."""
        return self.reason is not None


def should_destroy(
    *,
    state: GuardianState,
    config: GuardianConfig,
    now: float,
) -> GuardianDecision:
    """Decide whether the VM should self-destruct at ``now``.

    Order of precedence:

    1. ``manual_destroy_requested`` — returns ``"manual"`` even if the
       budgets still have slack.
    2. Lifetime ceiling — returns ``"lifetime"`` once since-boot
       exceeds ``max_lifetime_budget_s``.
    3. Idle ceiling — returns ``"idle"`` once since-last-bump exceeds
       ``idle_budget_s``.
    4. Otherwise no reason.

    Args:
        state: Current mutable runtime state.
        config: Immutable boot-time tuning.
        now: Unix seconds of the decision point.

    Returns:
        A :class:`GuardianDecision` with the chosen reason (or ``None``)
        and both elapsed counters for telemetry.
    """
    idle_elapsed = max(0.0, now - state.last_bump_ts)
    lifetime_elapsed = max(0.0, now - state.boot_ts)

    if state.manual_destroy_requested:
        return GuardianDecision(
            reason="manual",
            idle_elapsed_s=idle_elapsed,
            lifetime_elapsed_s=lifetime_elapsed,
        )
    if lifetime_elapsed >= config.max_lifetime_budget_s:
        return GuardianDecision(
            reason="lifetime",
            idle_elapsed_s=idle_elapsed,
            lifetime_elapsed_s=lifetime_elapsed,
        )
    if idle_elapsed >= config.idle_budget_s:
        return GuardianDecision(
            reason="idle",
            idle_elapsed_s=idle_elapsed,
            lifetime_elapsed_s=lifetime_elapsed,
        )
    return GuardianDecision(
        reason=None,
        idle_elapsed_s=idle_elapsed,
        lifetime_elapsed_s=lifetime_elapsed,
    )


def remaining_s(
    *,
    state: GuardianState,
    config: GuardianConfig,
    now: float,
) -> tuple[float, float]:
    """Return ``(idle_remaining_s, lifetime_remaining_s)`` at ``now``.

    Negative means the budget is exhausted — the corresponding trigger
    in :func:`should_destroy` would fire. Exposed for ``/infra/status``.
    """
    idle_remaining = config.idle_budget_s - (now - state.last_bump_ts)
    lifetime_remaining = config.max_lifetime_budget_s - (now - state.boot_ts)
    return idle_remaining, lifetime_remaining
