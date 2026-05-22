"""Production entry-point for the Qwen3-TTS worker.

Shape:

1. Resolve config from env vars (worker id, voice id, playground url,
   infra-agent bump url, advertised endpoint url).
2. Build the TTS engine. In production this imports the real Qwen3
   backend lazily (to avoid heavy CUDA imports in unit tests). For
   tests :func:`main` accepts an injected engine via the
   ``engine_factory`` hook.
3. Self-register with the playground registry; pin the voice.
4. Start a background heartbeat thread that POSTs every
   :data:`HEARTBEAT_INTERVAL_S` seconds.
5. Run uvicorn on :data:`WORKER_PORT`. The infra agent's guardian is
   running in parallel on :mod:`strands_agents.infra_agent`'s port.
6. On clean shutdown, unregister.

The runner does **not** own the guardian — that lives in its own
process. See ``scripts/qwen3_tts_worker_bootstrap.sh`` for how the two
are supervised.
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

from strands_agents.infra_agent.telemetry import (
    ResourceTelemetry,
    nvidia_smi_prober,
    shutil_disk_prober,
)

from .app import build_app
from .bump_client import InfraAgentBumpClient
from .engine import StubTTSEngine, TTSEngine
from .registry_client import (
    PlaygroundRegistryClient,
    RegistryClientError,
)

logger = logging.getLogger(__name__)


#: Port the worker FastAPI listens on. Hardcoded so the bootstrap
#: script can hard-code it into the registered ``endpoint_url``.
WORKER_PORT: int = 29231

#: How often the worker heartbeats the playground registry. Short
#: enough that ``is_stale`` (90 s) never trips under normal
#: operation, long enough to not hammer the registry.
HEARTBEAT_INTERVAL_S: float = 30.0

#: Default advertised VRAM when the VM has no GPU (local dev).
DEFAULT_LOCAL_VRAM_GB: int = 1


@dataclass
class WorkerConfig:
    """Resolved runner config."""

    worker_id: str
    voice_id: str
    endpoint_url: str
    playground_base_url: str | None
    vram_gb: int
    bump_url: str
    disk_path: str


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "name=<%s>, raw=<%s> | env var not int, falling back",
            name,
            raw,
        )
        return default


def _resolve_config(args: argparse.Namespace) -> WorkerConfig:
    worker_id = args.worker_id or os.environ.get("WORKER_ID") or ""
    if not worker_id:
        raise SystemExit("WORKER_ID must be set (env or --worker-id)")

    voice_id = args.voice_id or os.environ.get("WORKER_VOICE_ID") or ""
    if not voice_id:
        raise SystemExit("WORKER_VOICE_ID must be set (env or --voice-id)")

    endpoint_url = args.endpoint_url or os.environ.get("WORKER_ENDPOINT_URL")
    if not endpoint_url:
        public_ip = os.environ.get("PUBLIC_IPADDR") or "127.0.0.1"
        endpoint_url = f"http://{public_ip}:{WORKER_PORT}"

    playground_base_url = (
        args.playground_base_url or os.environ.get("PLAYGROUND_BACKEND_URL")
    )

    vram_gb = (
        args.vram_gb
        if args.vram_gb is not None
        else _env_int("WORKER_VRAM_GB", DEFAULT_LOCAL_VRAM_GB)
    )
    bump_url = args.bump_url or os.environ.get(
        "INFRA_AGENT_BUMP_URL", "http://127.0.0.1:29230/"
    )
    disk_path = args.disk_path or os.environ.get("WORKER_DISK_PATH", "/")

    return WorkerConfig(
        worker_id=worker_id,
        voice_id=voice_id,
        endpoint_url=endpoint_url,
        playground_base_url=playground_base_url,
        vram_gb=vram_gb,
        bump_url=bump_url,
        disk_path=disk_path,
    )


def _real_tts_engine_factory() -> TTSEngine:
    """Default factory for the production Qwen3-TTS engine.

    The real engine wraps ``transformers`` + ``torch`` and is heavy to
    import; we defer that until the VM is actually booting. Tests
    override this factory with :class:`StubTTSEngine`.
    """
    try:
        from ._qwen3_engine import Qwen3TTSEngine  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "qwen3 backend not installed, falling back to stub engine "
            "(this is expected in CI; production VMs install it via bootstrap)"
        )
        return StubTTSEngine()
    return Qwen3TTSEngine()


def heartbeat_loop(
    *,
    worker_id: str,
    registry_client: PlaygroundRegistryClient,
    telemetry: ResourceTelemetry,
    stop_event: threading.Event,
    interval_s: float = HEARTBEAT_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Background heartbeat loop.

    Swallows :class:`RegistryClientError` so a single network blip
    doesn't kill the worker. Stops when ``stop_event`` is set.
    """
    next_deadline = clock()
    while not stop_event.is_set():
        now = clock()
        if now >= next_deadline:
            free_gb: int | None = None
            try:
                snapshot = telemetry.sample()
            except Exception as exc:
                logger.warning(
                    "worker_id=<%s>, error=<%s> | telemetry sample failed (continuing)",
                    worker_id,
                    exc,
                )
            else:
                if (
                    snapshot.vram_total_gb is not None
                    and snapshot.vram_used_gb is not None
                ):
                    free_gb = max(
                        snapshot.vram_total_gb - snapshot.vram_used_gb, 0
                    )
            try:
                registry_client.heartbeat(
                    worker_id=worker_id, free_vram_gb=free_gb
                )
            except RegistryClientError as exc:
                logger.warning(
                    "worker_id=<%s>, error=<%s> | heartbeat failed (continuing)",
                    worker_id,
                    exc,
                )
            next_deadline = now + interval_s
        stop_event.wait(timeout=min(1.0, interval_s / 4.0))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qwen3-tts-worker",
        description="Qwen3-TTS worker FastAPI for Vast.ai VMs.",
    )
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--voice-id", default=None)
    parser.add_argument("--endpoint-url", default=None)
    parser.add_argument("--playground-base-url", default=None)
    parser.add_argument("--vram-gb", type=int, default=None)
    parser.add_argument("--bump-url", default=None)
    parser.add_argument("--disk-path", default=None)
    return parser.parse_args(argv)


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle(signum: int, _frame: object) -> None:
        logger.info("signum=<%d> | shutdown signal received", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (OSError, ValueError):
            # Non-main-thread or platform limitations.
            logger.debug("sig=<%d> | cannot install handler", sig)


def main(argv: list[str] | None = None) -> int:
    """Production entry-point.

    Returns:
        0 on clean shutdown, 1 on setup error.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    cfg = _resolve_config(args)

    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        logger.error("error=<%s> | uvicorn not installed, refusing to boot", exc)
        return 1

    telemetry = ResourceTelemetry(
        vram_prober=nvidia_smi_prober,
        disk_prober=shutil_disk_prober,
        disk_path=cfg.disk_path,
    )

    registry_client: PlaygroundRegistryClient | None = None
    if cfg.playground_base_url:
        registry_client = PlaygroundRegistryClient(
            base_url=cfg.playground_base_url
        )
        try:
            registry_client.register(
                worker_id=cfg.worker_id,
                role="tts",
                endpoint_url=cfg.endpoint_url,
                vram_gb=cfg.vram_gb,
                voice_id=cfg.voice_id,
            )
        except RegistryClientError as exc:
            logger.error(
                "worker_id=<%s>, error=<%s> | register failed, refusing to boot",
                cfg.worker_id,
                exc,
            )
            return 1
    else:
        logger.warning("no PLAYGROUND_BACKEND_URL set, worker runs unregistered (dev mode)")

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    heartbeat_thread: threading.Thread | None = None
    if registry_client is not None:
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            kwargs={
                "worker_id": cfg.worker_id,
                "registry_client": registry_client,
                "telemetry": telemetry,
                "stop_event": stop_event,
            },
            name="qwen3-tts-worker-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

    engine = _real_tts_engine_factory()
    bump_client = InfraAgentBumpClient(url=cfg.bump_url)

    app = build_app(
        worker_id=cfg.worker_id,
        pinned_voice_id=cfg.voice_id,
        engine=engine,
        telemetry=telemetry,
        bump_client=bump_client,
    )

    try:
        uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")
    finally:
        stop_event.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=10)
        if registry_client is not None:
            try:
                registry_client.unregister(worker_id=cfg.worker_id)
            except RegistryClientError as exc:
                logger.warning(
                    "worker_id=<%s>, error=<%s> | unregister failed on shutdown",
                    cfg.worker_id,
                    exc,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
