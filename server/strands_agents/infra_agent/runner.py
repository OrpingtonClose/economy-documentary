"""Infra agent runner — wires the pieces and drives the decision loop.

Responsibilities:

* Boot time snapshot (``time.time()`` at import).
* Periodic tick (every :data:`TICK_INTERVAL_S`) that:
    - samples telemetry so peaks update even without traffic,
    - checks :func:`should_destroy`,
    - on reason != None, runs the destruction sequence.
* FastAPI server via ``uvicorn`` on ``:29230``.
* Clean shutdown on ``SIGTERM`` / ``SIGINT``.

This module is the production entry-point for the agent; unit tests
exercise the pieces (guardian, clients, app) directly.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from strands_agents.infra_agent.app import build_app
from strands_agents.infra_agent.guardian import (
    DEFAULT_IDLE_SECONDS,
    DEFAULT_MAX_LIFETIME_SECONDS,
    DestroyReason,
    GuardianConfig,
    GuardianState,
    should_destroy,
)
from strands_agents.infra_agent.registry_client import (
    PlaygroundRegistryClient,
    RegistryDeregisterError,
)
from strands_agents.infra_agent.telemetry import (
    ResourceTelemetry,
    nvidia_smi_prober,
    shutil_disk_prober,
)
from strands_agents.infra_agent.vast_client import VastAiClient, VastAiDestroyError

logger = logging.getLogger(__name__)

#: Seconds between decision ticks. Short enough that manual-destroy
#: feels instant; long enough to not spam the VRAM probe.
TICK_INTERVAL_S: float = 5.0

#: Port the agent FastAPI listens on. Not configurable at runtime —
#: every bootstrap script hard-codes it so the registry knows where
#: to reach the agent.
AGENT_PORT: int = 29230


@dataclass
class RunnerConfig:
    """Resolved config from environment + CLI flags."""

    worker_id: str
    vm_instance_id: str | None
    playground_base_url: str | None
    idle_budget_s: int
    max_lifetime_budget_s: int
    disk_path: str


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("name=<%s>, raw=<%s> | env var not int, falling back", name, raw)
        return default


def _resolve_config(args: argparse.Namespace) -> RunnerConfig:
    """Build runner config from parsed args + env fallback."""
    worker_id = args.worker_id or os.environ.get("WORKER_ID") or ""
    if not worker_id:
        raise SystemExit("WORKER_ID must be set (env or --worker-id)")

    vm_instance_id = args.vm_instance_id or os.environ.get("VAST_INSTANCE_ID")
    playground_base_url = (
        args.playground_base_url or os.environ.get("PLAYGROUND_BACKEND_URL")
    )

    idle = args.idle_seconds or _env_int("GUARDIAN_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)
    lifetime = args.max_lifetime_seconds or _env_int(
        "GUARDIAN_MAX_LIFETIME_SECONDS", DEFAULT_MAX_LIFETIME_SECONDS
    )
    disk_path = args.disk_path or os.environ.get("GUARDIAN_DISK_PATH", "/")

    return RunnerConfig(
        worker_id=worker_id,
        vm_instance_id=vm_instance_id,
        playground_base_url=playground_base_url,
        idle_budget_s=idle,
        max_lifetime_budget_s=lifetime,
        disk_path=disk_path,
    )


def run_destroy_sequence(
    *,
    reason: DestroyReason,
    worker_id: str,
    vm_instance_id: str | None,
    registry_client: PlaygroundRegistryClient | None,
    vast_client: VastAiClient,
) -> None:
    """Execute the ordered shutdown: deregister → Vast destroy → exit.

    Order matters — deregister first so the playground registry stops
    handing this VM out for new work before its IP goes 404. Errors at
    each step are logged but non-fatal; the process always exits.
    """
    logger.warning(
        "worker_id=<%s>, reason=<%s> | destroy sequence starting",
        worker_id,
        reason,
    )

    if registry_client is not None:
        try:
            registry_client.deregister(worker_id)
        except RegistryDeregisterError as exc:
            logger.error(
                "worker_id=<%s>, error=<%s> | registry deregister failed (continuing)",
                worker_id,
                exc,
            )
    else:
        logger.info("worker_id=<%s> | no playground base url, skipping deregister", worker_id)

    if vm_instance_id is None:
        logger.info(
            "worker_id=<%s> | no vm_instance_id, skipping vast.ai destroy (local dev?)",
            worker_id,
        )
        return

    try:
        vast_client.destroy_instance(vm_instance_id)
    except VastAiDestroyError as exc:
        logger.error(
            "worker_id=<%s>, instance_id=<%s>, error=<%s> | vast destroy failed (process exiting)",
            worker_id,
            vm_instance_id,
            exc,
        )


def decision_tick_loop(
    *,
    state: GuardianState,
    config: GuardianConfig,
    telemetry: ResourceTelemetry,
    on_destroy: Callable[[DestroyReason], None],
    stop_event: threading.Event,
    tick_interval_s: float = TICK_INTERVAL_S,
    clock: Callable[[], float] = time.time,
) -> None:
    """Background loop: sample telemetry + check decision each tick.

    Args:
        state: Mutable guardian state.
        config: Guardian config.
        telemetry: Peak tracker; sampled every tick.
        on_destroy: Callable invoked with the destroy reason when the
            guardian decides the VM should shut down. The runner wires
            this to the registry-deregister + Vast-destroy sequence.
        stop_event: When set, loop exits before the next sleep.
        tick_interval_s: Override for tests.
        clock: Override for tests.
    """
    while not stop_event.is_set():
        try:
            telemetry.sample()
        except Exception as exc:
            logger.warning("error=<%s> | telemetry sample failed (continuing)", exc)

        now = clock()
        decision = should_destroy(state=state, config=config, now=now)
        if decision.should_destroy and decision.reason is not None:
            logger.warning(
                "reason=<%s>, idle_elapsed_s=<%f>, lifetime_elapsed_s=<%f> | destroy triggered",
                decision.reason,
                decision.idle_elapsed_s,
                decision.lifetime_elapsed_s,
            )
            on_destroy(decision.reason)
            return

        if stop_event.wait(tick_interval_s):
            return


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--vm-instance-id", default=None)
    parser.add_argument("--playground-base-url", default=None)
    parser.add_argument("--idle-seconds", type=int, default=None)
    parser.add_argument("--max-lifetime-seconds", type=int, default=None)
    parser.add_argument("--disk-path", default=None)
    parser.add_argument(
        "--no-gpu",
        action="store_true",
        help="Skip nvidia-smi probe (for non-GPU VMs).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Production entry-point. Returns a shell-friendly exit code."""
    logging.basicConfig(
        level=os.environ.get("INFRA_AGENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    args = _parse_args(argv)
    try:
        config_resolved = _resolve_config(args)
    except SystemExit as exc:
        logger.error("error=<%s> | config resolution failed", exc)
        raise

    boot_ts = time.time()
    state = GuardianState(boot_ts=boot_ts, last_bump_ts=boot_ts)
    guardian_cfg = GuardianConfig(
        idle_budget_s=config_resolved.idle_budget_s,
        max_lifetime_budget_s=config_resolved.max_lifetime_budget_s,
    )
    telemetry = ResourceTelemetry(
        vram_prober=(lambda: None) if args.no_gpu else nvidia_smi_prober,
        disk_prober=shutil_disk_prober,
        disk_path=config_resolved.disk_path,
    )

    registry_client: PlaygroundRegistryClient | None = None
    if config_resolved.playground_base_url:
        registry_client = PlaygroundRegistryClient(
            base_url=config_resolved.playground_base_url
        )
    vast_client = VastAiClient()

    stop_event = threading.Event()
    destroy_triggered = threading.Event()

    def _on_destroy(reason: DestroyReason) -> None:
        if destroy_triggered.is_set():
            return
        destroy_triggered.set()
        run_destroy_sequence(
            reason=reason,
            worker_id=config_resolved.worker_id,
            vm_instance_id=config_resolved.vm_instance_id,
            registry_client=registry_client,
            vast_client=vast_client,
        )
        stop_event.set()

    tick_thread = threading.Thread(
        target=decision_tick_loop,
        name="infra-agent-tick",
        kwargs={
            "state": state,
            "config": guardian_cfg,
            "telemetry": telemetry,
            "on_destroy": _on_destroy,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    tick_thread.start()

    def _handle_signal(signum: int, _frame: "object") -> None:
        logger.warning("signum=<%d> | signal received, stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        logger.error("error=<%s> | uvicorn not available", exc)
        return 2

    app = build_app(
        worker_id=config_resolved.worker_id,
        vm_instance_id=config_resolved.vm_instance_id,
        state=state,
        config=guardian_cfg,
        telemetry=telemetry,
    )
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")

    stop_event.set()
    tick_thread.join(timeout=10)
    return 0 if destroy_triggered.is_set() else 1


if __name__ == "__main__":
    sys.exit(main())
