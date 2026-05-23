"""FastAPI surface for the infrastructure agent.

Plain-text protocol — two endpoints only:

* ``GET /`` — lightweight liveness probe. Returns plain text.
  Does NOT bump the idle timer (otherwise pollers keep VMs alive forever).
* ``POST /`` — instruction endpoint. Receives raw text:
  - ``bump`` → resets the idle timer
  - ``destroy [reason]`` → latches the manual-destroy flag
  - ``status`` → returns full agent state as plain text

The app is a factory (:func:`build_app`) so tests and production both
build against injected guardian state / config / telemetry / clients.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

from strands_agents.infra_agent.guardian import (
    GuardianConfig,
    GuardianState,
    remaining_s,
)
from strands_agents.infra_agent.telemetry import ResourceTelemetry

logger = logging.getLogger(__name__)


def _serialise_status_plain(
    *,
    worker_id: str,
    vm_instance_id: str | None,
    state: GuardianState,
    config: GuardianConfig,
    telemetry: ResourceTelemetry,
    now: float,
) -> str:
    """Render a plain-text status line."""
    idle_remaining, lifetime_remaining = remaining_s(
        state=state, config=config, now=now
    )
    snapshot = telemetry.sample()
    parts = [
        f"worker_id={worker_id}",
        f"vm_id={vm_instance_id or 'none'}",
        f"uptime_s={round(now - state.boot_ts, 1)}",
        f"idle_remaining_s={round(idle_remaining, 1)}",
        f"lifetime_remaining_s={round(lifetime_remaining, 1)}",
        f"manual_destroy={state.manual_destroy_requested}",
        f"vram={snapshot.vram_used_gb:.1f}/{snapshot.vram_total_gb:.1f}GB",
        f"disk={snapshot.disk_used_gb:.1f}/{snapshot.disk_total_gb:.1f}GB",
    ]
    return " ".join(parts)


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

    @app.get("/")
    def _health() -> Response:
        """Liveness. Does not bump — polling loops shouldn't keep VMs alive."""
        return Response(
            content=f"ok worker_id={worker_id}",
            media_type="text/plain",
        )

    @app.post("/")
    async def _instruction(request: Request) -> Response:
        """Receive a plain-text instruction."""
        body = await request.body()
        text = body.decode("utf-8").strip()
        now = clock()

        if text == "bump":
            state.bump(now)
            logger.debug(
                "worker_id=<%s>, last_bump_ts=<%f> | bump",
                worker_id,
                state.last_bump_ts,
            )
            return Response(content="ok", media_type="text/plain")

        if text == "status":
            state.bump(now)
            status_text = _serialise_status_plain(
                worker_id=worker_id,
                vm_instance_id=vm_instance_id,
                state=state,
                config=config,
                telemetry=telemetry,
                now=now,
            )
            return Response(content=status_text, media_type="text/plain")

        if text.startswith("destroy"):
            reason = text[len("destroy"):].strip() or "manual"
            state.request_manual_destroy()
            logger.warning(
                "worker_id=<%s>, reason=<%s> | manual destroy latched",
                worker_id,
                reason,
            )
            return Response(content="ok", media_type="text/plain")

        return Response(
            content=f"unknown instruction: {text}",
            media_type="text/plain",
            status_code=400,
        )

    return app
