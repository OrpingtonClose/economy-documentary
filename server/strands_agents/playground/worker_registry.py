"""Worker registry + VRAM pre-flight probe for the Component Playground.

Per the plan in ``docs/strands-migration/plans/next-wave.md`` (slice 3
of Wave 2/3):

* Every GPU- or TTS-bound component declares a minimum VRAM requirement
  (or, for TTS, that it needs exactly one voice per VM).
* Each worker self-registers with its role, VRAM total, and -- for TTS
  -- the single voice it is pinned to. Pinning is enforced here so two
  parallel ``launch_audio_render`` calls with different voices on the
  same pool is a registration-time error, not a runtime race.
* Before the pipeline enters a stage that needs a worker, the
  playground calls :func:`preflight_vram` to verify every worker in the
  target pool has enough VRAM to run the component's declared model.
* Failure fails loud with exact numbers so the frontend can show e.g.
  ``worker ltx-a: 40 GB available, 48 GB required for LTX-Video 2.3``
  on the playground card.

Design:

* **In-memory, thread-safe.** A Redis-backed adapter lands with the
  real worker bootstrap in slice 4/5; the protocol surface defined
  here is deliberately independent of the storage backend so the
  unit tests can ship without any external service.
* **Probe is injectable via :class:`VramProber`** so tests can use a
  deterministic stub while the live HTTP prober against the worker's
  ``/health/vram`` endpoint lands alongside the worker bootstrap.
* **One-voice-per-VM is a hard invariant** (AGENTS.md §1). The
  registry raises :class:`VoiceAlreadyPinnedError` /
  :class:`WorkerAlreadyHasVoiceError` on violation rather than letting
  the caller "retry with a different voice" -- there is no valid
  retry, the TTS worker is stateful.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


#: Worker roles the playground pipeline currently recognises. Ordering
#: mirrors the pipeline stage ordering in
#: ``docs/strands-migration/plans/next-wave.md`` §2.1.
WorkerRole = Literal["tts", "ltx_render", "assembly"]

#: Stable vocabulary of worker roles. Exposed for iteration and for
#: validation on register calls.
WORKER_ROLES: tuple[WorkerRole, ...] = ("tts", "ltx_render", "assembly")

#: Sentinel reason string on ``STEP_FAILED`` envelopes when a worker's
#: VRAM is below the component-declared minimum. Stable so the
#: frontend and CI matcher agree on what to look for.
VRAM_INSUFFICIENT: str = "VRAM_INSUFFICIENT"

#: Sentinel reason string when a stage requests a worker role with no
#: live registrations -- usually because the worker VM hasn't booted
#: yet or its bootstrap crashed. Distinct from :data:`VRAM_INSUFFICIENT`
#: because the remediation is different (spin up a VM, not replace it).
NO_WORKERS_REGISTERED: str = "NO_WORKERS_REGISTERED"


def _now() -> float:
    """Override point for tests. Unix seconds, monotonic enough for
    heartbeat staleness comparisons."""

    return time.time()


@dataclass(frozen=True)
class VramProbeResult:
    """One snapshot of a worker's GPU state.

    Attributes:
        worker_id: The registered worker's id.
        total_gb: ``torch.cuda.get_device_properties(0).total_memory``
            rounded to whole GB. This is what the pre-flight check
            compares against.
        free_gb: Best-effort free VRAM at probe time. Not gated on;
            purely informational for the playground card.
        compute_capability: e.g. ``(8, 0)`` for A100. Informational.
        probed_at: Unix seconds.
    """

    worker_id: str
    total_gb: int
    free_gb: int | None
    compute_capability: tuple[int, int] | None
    probed_at: float


@dataclass
class Worker:
    """One registered worker VM.

    Attributes:
        worker_id: Unique across the whole fleet. Conventionally
            ``<role>-<shortid>`` e.g. ``tts-a3f`` / ``ltx-9bb``.
        role: See :data:`WORKER_ROLES`.
        endpoint_url: HTTP base URL for the worker's router -- the
            pipeline calls ``{endpoint_url}/render`` / ``/tts`` /
            ``/health/vram`` against this.
        vram_gb: Declared VRAM total. The registry trusts the worker's
            self-report on register; the real value lands via a probe
            on first heartbeat.
        voice_id: Only meaningful when ``role == "tts"``. ``None``
            until the worker pins a voice via
            :meth:`WorkerRegistry.pin_voice`. Once set, immutable for
            the lifetime of the worker (one TTS voice per VM --
            AGENTS.md §1).
        registered_at: Unix seconds.
        last_heartbeat_at: Unix seconds of the most recent heartbeat.
            Workers older than :data:`HEARTBEAT_STALE_SECONDS` are
            considered stale and skipped by :func:`preflight_vram`.
        last_probe: Most recent VRAM probe result, or ``None`` if the
            worker has not been probed since registration.
    """

    worker_id: str
    role: WorkerRole
    endpoint_url: str
    vram_gb: int
    voice_id: str | None = None
    registered_at: float = field(default_factory=_now)
    last_heartbeat_at: float = field(default_factory=_now)
    last_probe: VramProbeResult | None = None


#: Workers whose most recent heartbeat was more than this many seconds
#: ago are treated as unreachable by :func:`preflight_vram`. Loose
#: enough to tolerate a slow network, tight enough that a crashed VM
#: falls out of the pool before the pipeline tries to dispatch to it.
HEARTBEAT_STALE_SECONDS: float = 90.0


class VramProber(Protocol):
    """Anything that can turn a :class:`Worker` into a probe result."""

    def probe(self, worker: Worker) -> VramProbeResult: ...


class WorkerRegistryError(Exception):
    """Base class for all registry-raised errors."""


class WorkerNotFoundError(WorkerRegistryError):
    """Caller referenced a ``worker_id`` that is not registered."""

    def __init__(self, worker_id: str) -> None:
        super().__init__(f"worker not registered: {worker_id}")
        self.worker_id = worker_id


class DuplicateWorkerError(WorkerRegistryError):
    """Caller tried to register a ``worker_id`` that is already live."""

    def __init__(self, worker_id: str) -> None:
        super().__init__(f"worker already registered: {worker_id}")
        self.worker_id = worker_id


class VoiceAlreadyPinnedError(WorkerRegistryError):
    """Another worker has already pinned this voice.

    One TTS voice per VM is an AGENTS.md §1 hard invariant. Raised
    instead of silently re-using the voice on a second VM, which would
    race the stateful TTS worker.
    """

    def __init__(self, voice_id: str, other_worker_id: str) -> None:
        super().__init__(
            f"voice {voice_id!r} is already pinned to worker {other_worker_id}"
        )
        self.voice_id = voice_id
        self.other_worker_id = other_worker_id


class WorkerAlreadyHasVoiceError(WorkerRegistryError):
    """This worker is already pinned to a different voice.

    A pinned voice is immutable for the lifetime of the worker
    registration -- see :class:`Worker.voice_id`. Callers that want to
    switch voices must unregister and re-register (i.e. replace the
    VM).
    """

    def __init__(self, worker_id: str, existing_voice_id: str, new_voice_id: str) -> None:
        super().__init__(
            f"worker {worker_id} already pinned to voice {existing_voice_id!r}; "
            f"refusing to re-pin to {new_voice_id!r}"
        )
        self.worker_id = worker_id
        self.existing_voice_id = existing_voice_id
        self.new_voice_id = new_voice_id


class VoiceOnNonTtsWorkerError(WorkerRegistryError):
    """Caller tried to pin a voice on a worker whose role isn't ``tts``."""

    def __init__(self, worker_id: str, role: WorkerRole) -> None:
        super().__init__(
            f"cannot pin voice on worker {worker_id}: role is {role!r}, not 'tts'"
        )
        self.worker_id = worker_id
        self.role = role


@dataclass(frozen=True)
class VramShortfall:
    """Per-worker shortfall detail surfaced on a failed pre-flight."""

    worker_id: str
    actual_gb: int
    required_gb: int


class VramInsufficientError(WorkerRegistryError):
    """At least one worker in the target pool has insufficient VRAM.

    The pipeline orchestrator catches this and emits
    ``STEP_FAILED { reason: "VRAM_INSUFFICIENT", detail: {...} }``
    without entering the stage. Never retry -- the VM has the VRAM it
    has. Remediation is to provision a larger VM.
    """

    def __init__(
        self,
        role: WorkerRole,
        required_gb: int,
        model: str,
        shortfalls: tuple[VramShortfall, ...],
    ) -> None:
        detail = ", ".join(
            f"{s.worker_id}={s.actual_gb}GB" for s in shortfalls
        )
        super().__init__(
            f"insufficient VRAM for role={role!r} model={model!r}: "
            f"required {required_gb}GB, shortfalls: {detail}"
        )
        self.role = role
        self.required_gb = required_gb
        self.model = model
        self.shortfalls = shortfalls


class NoWorkersRegisteredError(WorkerRegistryError):
    """Pre-flight ran against a role with zero live workers."""

    def __init__(self, role: WorkerRole) -> None:
        super().__init__(f"no workers registered for role={role!r}")
        self.role = role


class WorkerRegistry:
    """Thread-safe in-memory fleet registry.

    Each registered :class:`Worker` is keyed by its ``worker_id``. The
    registry enforces the one-voice-per-VM invariant on voice pinning,
    but it does **not** enforce the VRAM pre-flight -- callers invoke
    :func:`preflight_vram` explicitly before dispatching a stage.

    Example::

        registry = WorkerRegistry()
        registry.register_worker(
            worker_id="tts-a3f",
            role="tts",
            endpoint_url="http://10.0.0.5:8080",
            vram_gb=24,
        )
        registry.pin_voice("tts-a3f", "narrator_male_1")
        # ... workers send periodic heartbeats ...
        registry.heartbeat("tts-a3f", free_vram_gb=22)
    """

    def __init__(self, now: Callable[[], float] = _now) -> None:
        self._now = now
        self._workers: dict[str, Worker] = {}
        self._voice_owner: dict[str, str] = {}
        self._lock = threading.RLock()

    # -- registration --------------------------------------------------

    def register_worker(
        self,
        *,
        worker_id: str,
        role: WorkerRole,
        endpoint_url: str,
        vram_gb: int,
        voice_id: str | None = None,
    ) -> Worker:
        """Register a worker. Idempotent on ``worker_id`` is a bug,
        not a feature -- a repeated register call with the same id
        raises :class:`DuplicateWorkerError` so the worker's bootstrap
        script has to explicitly unregister first.

        Raises:
            DuplicateWorkerError: ``worker_id`` is already registered.
            VoiceAlreadyPinnedError: ``voice_id`` is already pinned on
                another worker.
            VoiceOnNonTtsWorkerError: ``voice_id`` given with a
                non-``tts`` role.
            ValueError: ``role`` is not in :data:`WORKER_ROLES` or
                ``vram_gb`` is non-positive.
        """

        if role not in WORKER_ROLES:
            raise ValueError(f"unknown worker role: {role!r}")
        if vram_gb <= 0:
            raise ValueError(f"vram_gb must be positive, got {vram_gb}")

        with self._lock:
            if worker_id in self._workers:
                raise DuplicateWorkerError(worker_id)
            if voice_id is not None:
                if role != "tts":
                    raise VoiceOnNonTtsWorkerError(worker_id, role)
                owner = self._voice_owner.get(voice_id)
                if owner is not None:
                    raise VoiceAlreadyPinnedError(voice_id, owner)
            now = self._now()
            worker = Worker(
                worker_id=worker_id,
                role=role,
                endpoint_url=endpoint_url,
                vram_gb=vram_gb,
                voice_id=voice_id,
                registered_at=now,
                last_heartbeat_at=now,
            )
            self._workers[worker_id] = worker
            if voice_id is not None:
                self._voice_owner[voice_id] = worker_id
            logger.info(
                "worker_id=<%s>, role=<%s>, vram_gb=<%d>, voice_id=<%s> | "
                "worker registered",
                worker_id,
                role,
                vram_gb,
                voice_id,
            )
            return worker

    def unregister_worker(self, worker_id: str) -> None:
        """Remove a worker and release any voice pin it held.

        Raises:
            WorkerNotFoundError: ``worker_id`` is not registered.
        """

        with self._lock:
            worker = self._workers.pop(worker_id, None)
            if worker is None:
                raise WorkerNotFoundError(worker_id)
            if worker.voice_id is not None:
                self._voice_owner.pop(worker.voice_id, None)
            logger.info(
                "worker_id=<%s> | worker unregistered", worker_id
            )

    # -- voice pinning -------------------------------------------------

    def pin_voice(self, worker_id: str, voice_id: str) -> None:
        """Pin ``voice_id`` to ``worker_id``. Idempotent on the same
        pair (pinning the same voice to the same worker twice is a
        no-op so cloud-init scripts can retry safely).

        Raises:
            WorkerNotFoundError: ``worker_id`` is not registered.
            VoiceOnNonTtsWorkerError: the worker's role isn't ``tts``.
            WorkerAlreadyHasVoiceError: the worker is pinned to a
                different voice.
            VoiceAlreadyPinnedError: the voice is pinned on a
                different worker.
        """

        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotFoundError(worker_id)
            if worker.role != "tts":
                raise VoiceOnNonTtsWorkerError(worker_id, worker.role)
            if worker.voice_id is not None:
                if worker.voice_id == voice_id:
                    return
                raise WorkerAlreadyHasVoiceError(
                    worker_id=worker_id,
                    existing_voice_id=worker.voice_id,
                    new_voice_id=voice_id,
                )
            owner = self._voice_owner.get(voice_id)
            if owner is not None and owner != worker_id:
                raise VoiceAlreadyPinnedError(voice_id, owner)
            worker.voice_id = voice_id
            self._voice_owner[voice_id] = worker_id
            logger.info(
                "worker_id=<%s>, voice_id=<%s> | voice pinned",
                worker_id,
                voice_id,
            )

    # -- heartbeat / probe ---------------------------------------------

    def heartbeat(
        self,
        worker_id: str,
        *,
        free_vram_gb: int | None = None,
    ) -> None:
        """Record a liveness heartbeat. ``free_vram_gb`` is optional;
        when present it updates the worker's most recent probe result
        in place.

        Raises:
            WorkerNotFoundError: ``worker_id`` is not registered.
        """

        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotFoundError(worker_id)
            now = self._now()
            worker.last_heartbeat_at = now
            if free_vram_gb is not None:
                previous = worker.last_probe
                worker.last_probe = VramProbeResult(
                    worker_id=worker_id,
                    total_gb=worker.vram_gb,
                    free_gb=free_vram_gb,
                    compute_capability=(
                        previous.compute_capability if previous else None
                    ),
                    probed_at=now,
                )

    def record_probe(self, result: VramProbeResult) -> None:
        """Persist a full :class:`VramProbeResult`. Also refreshes the
        heartbeat timestamp -- a successful probe is strictly stronger
        evidence of liveness than a bare heartbeat.

        Raises:
            WorkerNotFoundError: ``result.worker_id`` is not registered.
        """

        with self._lock:
            worker = self._workers.get(result.worker_id)
            if worker is None:
                raise WorkerNotFoundError(result.worker_id)
            worker.last_probe = result
            worker.last_heartbeat_at = result.probed_at
            # Trust the probe over the worker's self-report on
            # register: the probe reads `torch.cuda` directly.
            worker.vram_gb = result.total_gb

    # -- reads ---------------------------------------------------------

    def get_worker(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotFoundError(worker_id)
            return worker

    def has_worker(self, worker_id: str) -> bool:
        with self._lock:
            return worker_id in self._workers

    def iter_workers(
        self, *, role: WorkerRole | None = None
    ) -> Iterator[Worker]:
        """Snapshot iterator -- safe to consume outside the lock."""

        with self._lock:
            snapshot = tuple(self._workers.values())
        for worker in snapshot:
            if role is None or worker.role == role:
                yield worker

    def workers_for_role(self, role: WorkerRole) -> tuple[Worker, ...]:
        return tuple(self.iter_workers(role=role))

    def voice_owner(self, voice_id: str) -> str | None:
        with self._lock:
            return self._voice_owner.get(voice_id)

    def is_stale(self, worker: Worker, now: float | None = None) -> bool:
        at = now if now is not None else self._now()
        return (at - worker.last_heartbeat_at) > HEARTBEAT_STALE_SECONDS


def preflight_vram(
    registry: WorkerRegistry,
    *,
    role: WorkerRole,
    required_gb: int,
    model: str,
    include_stale: bool = False,
) -> tuple[Worker, ...]:
    """Gate a stage on VRAM sufficiency.

    Returns the tuple of workers eligible to serve the stage (live,
    correct role, sufficient VRAM).

    Raises:
        NoWorkersRegisteredError: zero workers are registered under
            ``role`` (ignoring staleness filter).
        VramInsufficientError: at least one live worker under ``role``
            has ``vram_gb < required_gb``. Detail includes every
            shortfall, not just the first, so the frontend can list
            all offending workers at once.
    """

    workers = registry.workers_for_role(role)
    if not workers:
        raise NoWorkersRegisteredError(role)

    if not include_stale:
        workers = tuple(w for w in workers if not registry.is_stale(w))
        if not workers:
            raise NoWorkersRegisteredError(role)

    shortfalls = tuple(
        VramShortfall(
            worker_id=w.worker_id,
            actual_gb=w.vram_gb,
            required_gb=required_gb,
        )
        for w in workers
        if w.vram_gb < required_gb
    )
    if shortfalls:
        raise VramInsufficientError(
            role=role,
            required_gb=required_gb,
            model=model,
            shortfalls=shortfalls,
        )
    return workers


def vram_insufficient_envelope(
    error: VramInsufficientError,
    *,
    stage: str = "production",
) -> dict[str, object]:
    """Serialise a :class:`VramInsufficientError` as the payload of a
    ``STEP_FAILED`` AG-UI envelope.

    The orchestrator wraps this in the usual event envelope (adding
    ``seq`` / ``ts`` / ``kind`` / ``type``), but the ``detail`` shape
    is anchored here so CI matchers and the frontend key off the same
    field names.
    """

    return {
        "stage": stage,
        "reason": VRAM_INSUFFICIENT,
        "detail": {
            "role": error.role,
            "model": error.model,
            "required_gb": error.required_gb,
            "shortfalls": [
                {
                    "worker_id": s.worker_id,
                    "actual_gb": s.actual_gb,
                    "required_gb": s.required_gb,
                }
                for s in error.shortfalls
            ],
        },
    }


# -- process-wide default registry ---------------------------------------

_DEFAULT_LOCK = threading.Lock()
_default_registry: WorkerRegistry | None = None


def get_default_registry() -> WorkerRegistry:
    """Return the process-wide registry, constructing it lazily.

    The FastAPI router, pipeline orchestrator, and tests all share one
    registry so a worker registered at app startup is visible to
    requests handled on different worker threads.
    """

    global _default_registry
    with _DEFAULT_LOCK:
        if _default_registry is None:
            _default_registry = WorkerRegistry()
        return _default_registry


def set_default_registry(registry: WorkerRegistry | None) -> None:
    """Override the process-wide registry. Primarily for tests -- a
    unit test can install a fresh instance in its fixture and restore
    the previous one in teardown. Passing ``None`` clears the
    registry so the next :func:`get_default_registry` builds a clean
    one.
    """

    global _default_registry
    with _DEFAULT_LOCK:
        _default_registry = registry
