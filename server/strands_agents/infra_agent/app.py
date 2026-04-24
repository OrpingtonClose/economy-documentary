"""FastAPI surface for the infrastructure agent.

Mounted on port 29230 on every Vast.ai VM. Four endpoints:

* ``GET /infra/status`` — returns full agent state (boot ts, remaining
  budgets, peak telemetry, destroy reason if latched). Bumps the
  idle timer on hit.
* ``POST /infra/bump`` — explicit noop that resets the idle timer.
  For debug sessions where a status pull is too noisy.
* ``POST /infra/destroy`` — latches the manual-destroy flag. The
  runner's next tick triggers the destruction sequence.
* ``GET /health`` — lightweight liveness probe. Does NOT bump the
  idle timer (otherwise k8s-style pollers keep VMs alive forever).

The app is a factory (:func:`build_app`) so tests and production both
build against injected guardian state / config / telemetry / clients.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from strands_agents.infra_agent.guardian import (
    GuardianConfig,
    GuardianState,
    remaining_s,
)
from strands_agents.infra_agent.telemetry import ResourceTelemetry

logger = logging.getLogger(__name__)


class _DestroyRequest(BaseModel):
    """Optional body on ``POST /infra/destroy``."""

    reason: str | None = Field(default=None, max_length=256)


def _serialise_status(
    *,
    worker_id: str,
    vm_instance_id: str | None,
    state: GuardianState,
    config: GuardianConfig,
    telemetry: ResourceTelemetry,
    now: float,
) -> dict[str, Any]:
    """Render a ``/infra/status`` payload."""
    idle_remaining, lifetime_remaining = remaining_s(
        state=state, config=config, now=now
    )
    snapshot = telemetry.sample()
    return {
        "worker_id": worker_id,
        "vm_instance_id": vm_instance_id,
        "boot_ts": state.boot_ts,
        "last_bump_ts": state.last_bump_ts,
        "uptime_s": now - state.boot_ts,
        "idle_budget_s": config.idle_budget_s,
        "idle_remaining_s": idle_remaining,
        "lifetime_budget_s": config.max_lifetime_budget_s,
        "lifetime_remaining_s": lifetime_remaining,
        "manual_destroy_requested": state.manual_destroy_requested,
        "telemetry": {
            "vram_total_gb": snapshot.vram_total_gb,
            "vram_used_gb": snapshot.vram_used_gb,
            "vram_peak_gb": snapshot.vram_peak_gb,
            "disk_total_gb": snapshot.disk_total_gb,
            "disk_used_gb": snapshot.disk_used_gb,
            "disk_peak_gb": snapshot.disk_peak_gb,
        },
    }


def build_app(
    *,
    worker_id: str,
    vm_instance_id: str | None,
    state: GuardianState,
    config: GuardianConfig,
    telemetry: ResourceTelemetry,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Construct an agent FastAPI app wired to the given dependencies.

    Args:
        worker_id: The playground worker id this agent represents.
        vm_instance_id: Vast.ai instance id, or ``None`` for non-Vast
            hosts (local dev).
        state: Mutable :class:`GuardianState`. Bumps happen here.
        config: Immutable :class:`GuardianConfig`.
        telemetry: Peak-tracking telemetry instance.
        clock: Overridable for tests.

    Returns:
        A ready-to-serve FastAPI app.
    """
    app = FastAPI(
        title=f"infra-agent[{worker_id}]",
        description="Per-VM cost guardian + control plane",
    )
    router = APIRouter(prefix="/infra", tags=["infra"])

    @app.get("/health")
    def _health() -> dict[str, Any]:
        """Liveness. Does not bump — polling loops shouldn't keep VMs alive."""
        return {"ok": True, "worker_id": worker_id}

    @router.get("/status")
    def _status() -> JSONResponse:
        now = clock()
        state.bump(now)
        logger.debug(
            "worker_id=<%s>, last_bump_ts=<%f> | status hit (bumped)",
            worker_id,
            state.last_bump_ts,
        )
        return JSONResponse(
            content=_serialise_status(
                worker_id=worker_id,
                vm_instance_id=vm_instance_id,
                state=state,
                config=config,
                telemetry=telemetry,
                now=now,
            )
        )

    @router.post("/bump")
    def _bump() -> JSONResponse:
        now = clock()
        state.bump(now)
        logger.debug(
            "worker_id=<%s>, last_bump_ts=<%f> | explicit bump",
            worker_id,
            state.last_bump_ts,
        )
        _idle_remaining, _lifetime_remaining = remaining_s(
            state=state, config=config, now=now
        )
        return JSONResponse(
            content={
                "ok": True,
                "last_bump_ts": state.last_bump_ts,
                "idle_remaining_s": _idle_remaining,
            }
        )

    @router.post("/destroy")
    def _destroy(body: _DestroyRequest | None = None) -> JSONResponse:
        reason = (body.reason if body is not None else None) or "manual"
        state.request_manual_destroy()
        logger.warning(
            "worker_id=<%s>, reason=<%s> | manual destroy latched",
            worker_id,
            reason,
        )
        return JSONResponse(
            content={
                "ok": True,
                "manual_destroy_requested": True,
                "reason": reason,
            }
        )

    app.include_router(router)
    return app
