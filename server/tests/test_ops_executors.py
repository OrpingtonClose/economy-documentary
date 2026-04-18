"""Unit tests for :mod:`orchestrator.ops_executors`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_server = Path(__file__).resolve().parent.parent
if str(_server) not in sys.path:
    sys.path.insert(0, str(_server))

from orchestrator.escalation_menu import EscalationAction  # noqa: E402
from orchestrator.ops_executors import (  # noqa: E402
    execute_freeze_batch_and_replan,
    execute_ops_action,
    execute_provision_extra_worker,
    execute_recycle_worker,
    execute_wait_for_worker_recovery,
    set_infra_agent_factory,
    set_orchestrator_factory,
    set_provisioner_factory,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeInfraAgent:
    def __init__(self, healthy: list[str] | None = None) -> None:
        self.healthy = list(healthy or [])
        self.removed: list[str] = []
        self.paused_with: list[str] = []

    def get_healthy_workers(self, role: Any = None) -> list[str]:
        return list(self.healthy)

    def remove_worker(self, url: str) -> None:
        self.removed.append(url)

    def pause(self, reason: str) -> None:
        self.paused_with.append(reason)


class FakeProvisioner:
    def __init__(self, raise_on: str = "") -> None:
        self.recycled: list[tuple[str, str]] = []
        self.provisioned: list[tuple[str, int]] = []
        self.raise_on = raise_on

    def recycle_worker(self, worker_url: str, reason: str = "") -> dict[str, Any]:
        if self.raise_on == "recycle":
            raise RuntimeError("boom")
        self.recycled.append((worker_url, reason))
        return {"new_vm_id": "vm-42"}

    def provision_extra_workers(self, role: str, count: int) -> dict[str, Any]:
        if self.raise_on == "provision":
            raise RuntimeError("boom")
        self.provisioned.append((role, count))
        return {"queued": count, "role": role}


class FakeOrchestrator:
    def __init__(self) -> None:
        self.replans: list[str] = []

    def request_replan(self, reason: str) -> None:
        self.replans.append(reason)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_infra() -> FakeInfraAgent:
    infra = FakeInfraAgent()
    set_infra_agent_factory(lambda: infra)
    yield infra
    set_infra_agent_factory(None)


@pytest.fixture
def fake_provisioner() -> FakeProvisioner:
    prov = FakeProvisioner()
    set_provisioner_factory(lambda: prov)
    yield prov
    set_provisioner_factory(None)


@pytest.fixture
def fake_orch() -> FakeOrchestrator:
    orch = FakeOrchestrator()
    set_orchestrator_factory(lambda: orch)
    yield orch
    set_orchestrator_factory(None)


@pytest.fixture
def no_provisioner() -> None:
    set_provisioner_factory(lambda: None)
    yield
    set_provisioner_factory(None)


@pytest.fixture
def no_orchestrator() -> None:
    set_orchestrator_factory(lambda: None)
    yield
    set_orchestrator_factory(None)


# ---------------------------------------------------------------------------
# wait_for_worker_recovery
# ---------------------------------------------------------------------------

def test_wait_returns_ok_when_worker_reappears(fake_infra: FakeInfraAgent) -> None:
    # First poll: empty.  Second poll: healthy.
    calls = {"n": 0}

    def fake_get_healthy(role: Any = None) -> list[str]:
        calls["n"] += 1
        return ["http://1.2.3.4:8000"] if calls["n"] >= 2 else []

    fake_infra.get_healthy_workers = fake_get_healthy  # type: ignore[assignment]

    slept: list[float] = []
    result = execute_wait_for_worker_recovery(
        "http://1.2.3.4:8000",
        timeout_sec=5.0,
        sleeper=lambda s: slept.append(s),
    )

    assert result["ok"] is True
    assert result["action"] == "wait_for_worker_recovery"
    assert result["attempts"] >= 2
    assert slept, "executor should have slept between polls"


def test_wait_times_out_without_recovery(fake_infra: FakeInfraAgent) -> None:
    fake_infra.healthy = []

    # Sleeper advances a fake clock so the deadline elapses quickly.
    import orchestrator.ops_executors as mod

    times = [1000.0]

    def fake_time() -> float:
        return times[0]

    def advance(_: float) -> None:
        times[0] += 1.0

    monkey_orig = mod.time.time
    mod.time.time = fake_time  # type: ignore[assignment]
    try:
        result = execute_wait_for_worker_recovery(
            "http://nope",
            timeout_sec=2.0,
            sleeper=advance,
        )
    finally:
        mod.time.time = monkey_orig  # type: ignore[assignment]

    assert result["ok"] is False
    assert "did not recover" in result["detail"]


# ---------------------------------------------------------------------------
# recycle_worker
# ---------------------------------------------------------------------------

def test_recycle_worker_calls_native_api(
    fake_provisioner: FakeProvisioner, fake_infra: FakeInfraAgent
) -> None:
    result = execute_recycle_worker("http://1.2.3.4:8000", "vram_pressure")

    assert result["ok"] is True
    assert fake_provisioner.recycled == [("http://1.2.3.4:8000", "vram_pressure")]
    assert fake_infra.removed == ["http://1.2.3.4:8000"]


def test_recycle_worker_handles_missing_api(fake_infra: FakeInfraAgent) -> None:
    class NoRecycle:
        pass

    set_provisioner_factory(lambda: NoRecycle())
    try:
        result = execute_recycle_worker("http://1.2.3.4:8000", "reason")
    finally:
        set_provisioner_factory(None)

    assert result["ok"] is False
    assert "has no recycle_worker" in result["detail"]
    assert result["infra_removed"] is True


def test_recycle_worker_handles_no_provisioner(no_provisioner: None) -> None:
    result = execute_recycle_worker("http://1.2.3.4:8000", "reason")
    assert result["ok"] is False
    assert result["detail"] == "provisioner unavailable"


def test_recycle_worker_handles_provisioner_exception(
    fake_infra: FakeInfraAgent,
) -> None:
    prov = FakeProvisioner(raise_on="recycle")
    set_provisioner_factory(lambda: prov)
    try:
        result = execute_recycle_worker("http://x", "r")
    finally:
        set_provisioner_factory(None)

    assert result["ok"] is False
    assert "raised" in result["detail"]


# ---------------------------------------------------------------------------
# provision_extra_worker
# ---------------------------------------------------------------------------

def test_provision_extra_worker_happy_path(
    fake_provisioner: FakeProvisioner,
) -> None:
    result = execute_provision_extra_worker("video", 2)

    assert result["ok"] is True
    assert fake_provisioner.provisioned == [("video", 2)]


def test_provision_extra_worker_rejects_bad_role() -> None:
    result = execute_provision_extra_worker("unicorn", 1)
    assert result["ok"] is False
    assert "invalid role" in result["detail"]


def test_provision_extra_worker_no_provisioner(no_provisioner: None) -> None:
    result = execute_provision_extra_worker("video", 1)
    assert result["ok"] is False
    assert result["detail"] == "provisioner unavailable"


def test_provision_extra_worker_missing_method() -> None:
    class Empty:
        pass

    set_provisioner_factory(lambda: Empty())
    try:
        result = execute_provision_extra_worker("video", 1)
    finally:
        set_provisioner_factory(None)

    assert result["ok"] is False
    assert "has no provision_extra_worker" in result["detail"]


# ---------------------------------------------------------------------------
# freeze_batch_and_replan
# ---------------------------------------------------------------------------

def test_freeze_pauses_infra_and_requests_replan(
    fake_infra: FakeInfraAgent, fake_orch: FakeOrchestrator
) -> None:
    result = execute_freeze_batch_and_replan("cost overrun")

    assert result["ok"] is True
    assert result["paused"] is True
    assert result["replan_requested"] is True
    assert fake_infra.paused_with == ["cost overrun"]
    assert fake_orch.replans == ["cost overrun"]


def test_freeze_degrades_gracefully_without_orchestrator(
    fake_infra: FakeInfraAgent, no_orchestrator: None
) -> None:
    result = execute_freeze_batch_and_replan("reason")

    # Pausing alone still succeeds; replan flag is False.
    assert result["paused"] is True
    assert result["replan_requested"] is False
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# execute_ops_action dispatch
# ---------------------------------------------------------------------------

def test_execute_ops_action_dispatches_each_variant(
    fake_infra: FakeInfraAgent,
    fake_provisioner: FakeProvisioner,
    fake_orch: FakeOrchestrator,
) -> None:
    fake_infra.healthy = ["http://ready"]
    wait = execute_ops_action(
        EscalationAction(
            action="wait_for_worker_recovery",
            worker_url="http://ready",
            timeout_sec=0.5,
        )
    )
    recycle = execute_ops_action(
        EscalationAction(
            action="recycle_worker",
            worker_url="http://ready",
            reason="test",
        )
    )
    provision = execute_ops_action(
        EscalationAction(
            action="provision_extra_worker",
            role="video",
            count=1,
        )
    )
    freeze = execute_ops_action(
        EscalationAction(
            action="freeze_batch_and_replan",
            reason="test",
        )
    )

    assert {wait["action"], recycle["action"], provision["action"], freeze["action"]} == {
        "wait_for_worker_recovery",
        "recycle_worker",
        "provision_extra_worker",
        "freeze_batch_and_replan",
    }


def test_execute_ops_action_rejects_creative_action() -> None:
    from orchestrator.escalation_menu import EscalationActionError

    with pytest.raises(EscalationActionError):
        execute_ops_action(
            EscalationAction(
                action="regenerate_clip",
                clip_id="c1",
                prompt_delta="d",
                seed_delta=7,
            )
        )
