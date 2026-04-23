"""Model reachability probe for the Component Playground.

Per the plan in
``docs/strands-migration/plans/component-playground.md``:

* every component declares its model(s) as part of its spec;
* before a run starts, the playground probes the declared model(s);
* a failed probe surfaces as ``MODEL_UNREACHABLE`` — an automatic
  hard-gate failure, not a skip, not a fallback, same rule in CI.

This module ships the probe machinery only. PR 3 wires the probe
into the run endpoint so a ``MODEL_UNREACHABLE`` status short-
circuits the run before any compute is spent.

Design:

* The probe is injectable via :class:`ModelProber` so tests can
  swap in deterministic stubs without touching networking.
* The default prober checks whether the provider's credential
  environment variables are present. That's enough to gate real
  runs — a missing key means the component cannot reach the model,
  which is exactly what the plan says must fail hard.
* Results are cached for ``_DEFAULT_TTL_SECONDS`` (60) to keep the
  frontend's polling cost bounded. The cache is thread-safe so the
  FastAPI workers can share it.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from strands_agents.playground.registry import DeclaredModel

_DEFAULT_TTL_SECONDS: float = 60.0

#: Sentinel string surfaced on the run stream when a probe fails.
#: PR 3 converts this into a hard-gate evaluator failure; exposing it
#: here keeps the string canonical so the frontend and the run
#: endpoint agree on what to match against.
MODEL_UNREACHABLE: str = "MODEL_UNREACHABLE"

#: Environment variables each provider looks at, in priority order.
#: Presence of *any* is treated as reachable-by-credentials.
_PROVIDER_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    #: Local providers (Ollama / vLLM) don't need an API key. The
    #: default prober treats them as reachable when an explicit
    #: endpoint is configured, and unreachable otherwise — a local
    #: model with no endpoint is not actually running.
    "local": ("OLLAMA_HOST", "OLLAMA_BASE_URL", "LOCAL_LLM_BASE_URL"),
}


@dataclass(frozen=True)
class ReachabilityStatus:
    """One model's reachability at a point in time.

    Attributes:
        model_id: The ``provider/model`` string from
            :class:`DeclaredModel.id`.
        provider: Short provider label (duplicated for UI
            convenience so the caller doesn't have to split the id).
        reachable: ``True`` iff the probe succeeded.
        reason: Free-form machine-readable reason. Stable vocabulary:
            ``"ok"`` — credentials / endpoint present;
            ``"no_credentials"`` — no provider env var set;
            ``"unknown_provider"`` — provider not recognised;
            ``"probe_error:<detail>"`` — a live prober raised.
        checked_at: Unix seconds when the probe ran.
        latency_ms: Probe latency if the prober actually made a call,
            else ``None``.
    """

    model_id: str
    provider: str
    reachable: bool
    reason: str
    checked_at: float
    latency_ms: float | None = None


class ModelProber(Protocol):
    """Anything that can turn a :class:`DeclaredModel` into a status."""

    def probe(self, model: DeclaredModel) -> ReachabilityStatus: ...


def _now() -> float:
    return time.time()


#: Env flag opting into live-ping reachability (the real thing). When
#: set (staging / production), the default prober becomes
#: :class:`LiteLLMPingProber`, which actually calls the model with a
#: one-token completion and fails red if the upstream rejects the
#: request — including the "declared id is wrong" case that slipped past
#: the credentials-only probe in early staging runs.
PLAYGROUND_LIVE_PING_ENV: str = "PLAYGROUND_REACHABILITY_PING"


class CredentialsProber:
    """Default prober: credentials-presence only, no network.

    A real run still goes through the provider's SDK, so if the
    credentials are valid but the upstream is down, the run itself
    fails hard with ``MODEL_UNREACHABLE``. This prober exists so the
    catalog UI can show a green/red dot without spending money.
    """

    def __init__(
        self,
        *,
        environ: Callable[[], dict[str, str]] = lambda: dict(os.environ),
    ) -> None:
        self._environ = environ

    def probe(self, model: DeclaredModel) -> ReachabilityStatus:
        env = self._environ()
        checked_at = _now()
        env_vars = _PROVIDER_CREDENTIAL_ENV.get(model.provider)
        if env_vars is None:
            return ReachabilityStatus(
                model_id=model.id,
                provider=model.provider,
                reachable=False,
                reason="unknown_provider",
                checked_at=checked_at,
            )
        if not env_vars:
            # Provider declared but no env vars associated — treat as
            # unreachable rather than silently green.
            return ReachabilityStatus(
                model_id=model.id,
                provider=model.provider,
                reachable=False,
                reason="no_credentials",
                checked_at=checked_at,
            )
        for var in env_vars:
            if env.get(var):
                return ReachabilityStatus(
                    model_id=model.id,
                    provider=model.provider,
                    reachable=True,
                    reason="ok",
                    checked_at=checked_at,
                )
        return ReachabilityStatus(
            model_id=model.id,
            provider=model.provider,
            reachable=False,
            reason="no_credentials",
            checked_at=checked_at,
        )


#: Env override for :class:`LiteLLMPingProber` per-probe timeout in
#: seconds. Thinking-style models (Gemini 3 Pro preview) can easily
#: spend 20-40 s on a cold call — the default is generous enough to
#: not flag them red on the first hit of the day, but not so long
#: that a truly dead endpoint stalls the catalog UI for a minute.
PLAYGROUND_PING_TIMEOUT_ENV: str = "PLAYGROUND_REACHABILITY_PING_TIMEOUT"

_DEFAULT_PING_TIMEOUT_SECONDS: float = 45.0


def _resolve_ping_timeout() -> float:
    raw = os.environ.get(PLAYGROUND_PING_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_PING_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PING_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_PING_TIMEOUT_SECONDS
    return value


class LiteLLMPingProber:
    """Live prober: fires a one-token completion at the declared model.

    Catches three failure modes that :class:`CredentialsProber` misses:

    * wrong declared id (``gemini/gemini-3.1-pro`` → 404 NOT_FOUND);
    * revoked / expired credentials (present in env but rejected);
    * upstream outage.

    The probe is capped at ``max_tokens=1`` so the cost per probe is
    effectively zero (single-token completion) but the request round-
    trips through the provider's actual routing, which is the point.

    Args:
        timeout_seconds: Per-probe upper bound. Defaults to 45 s so
            thinking-mode models (Gemini 3 Pro preview) don't get
            flagged red on a slow cold call. Overridable via
            ``PLAYGROUND_REACHABILITY_PING_TIMEOUT``.
        complete: Injectable hook for tests. Defaults to
            :func:`litellm.completion`.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        complete: Callable[..., object] | None = None,
    ) -> None:
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else _resolve_ping_timeout()
        )
        self._complete = complete

    def _resolve_complete(self) -> Callable[..., object]:
        if self._complete is not None:
            return self._complete
        import litellm  # local import keeps module import side-effect-free

        return litellm.completion

    def probe(self, model: DeclaredModel) -> ReachabilityStatus:
        checked_at = _now()
        start = time.perf_counter()
        try:
            complete = self._resolve_complete()
            complete(
                model=model.id,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=self._timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — any error → unreachable
            latency_ms = (time.perf_counter() - start) * 1000.0
            detail = _short_error(exc)
            return ReachabilityStatus(
                model_id=model.id,
                provider=model.provider,
                reachable=False,
                reason=f"probe_error:{detail}",
                checked_at=checked_at,
                latency_ms=latency_ms,
            )
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ReachabilityStatus(
            model_id=model.id,
            provider=model.provider,
            reachable=True,
            reason="ok",
            checked_at=checked_at,
            latency_ms=latency_ms,
        )


def _short_error(exc: BaseException) -> str:
    """Compact single-line error suitable for ``reason`` fields.

    Keeps 404 / authentication / timeout classes visible while
    trimming multi-line provider noise.
    """
    name = type(exc).__name__
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:157] + "..."
    return f"{name}: {msg}" if msg else name


@dataclass
class _CacheEntry:
    status: ReachabilityStatus
    expires_at: float


class ReachabilityCache:
    """TTL cache for probe results, thread-safe."""

    def __init__(
        self,
        prober: ModelProber,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = _now,
    ) -> None:
        self._prober = prober
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, model: DeclaredModel) -> ReachabilityStatus:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(model.id)
            if entry is not None and entry.expires_at > now:
                return entry.status
        status = self._prober.probe(model)
        with self._lock:
            self._entries[model.id] = _CacheEntry(
                status=status, expires_at=now + self._ttl_seconds
            )
        return status

    def get_many(
        self, models: Iterable[DeclaredModel]
    ) -> list[ReachabilityStatus]:
        return [self.get(m) for m in models]

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()


def _build_default_prober() -> ModelProber:
    """Pick the production prober based on ``PLAYGROUND_REACHABILITY_PING``.

    Live ping is opt-in because it costs a round-trip per declared
    model and we don't want CI (where provider credentials may be
    deliberately absent) to hit real endpoints. Staging / production
    set ``PLAYGROUND_REACHABILITY_PING=1`` to get the real signal.
    """
    flag = os.environ.get(PLAYGROUND_LIVE_PING_ENV, "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return LiteLLMPingProber()
    return CredentialsProber()


#: Module-level singleton. Tests override via :func:`set_default_cache`.
_default_cache: ReachabilityCache = ReachabilityCache(_build_default_prober())


def get_default_cache() -> ReachabilityCache:
    """Return the process-wide reachability cache."""
    return _default_cache


def set_default_cache(cache: ReachabilityCache) -> ReachabilityCache:
    """Install ``cache`` as the default and return the previous cache.

    Returning the prior cache lets callers (mostly tests) restore the
    original in a ``try/finally`` so the module-level singleton is
    never left mutated after a test run.
    """
    global _default_cache
    previous = _default_cache
    _default_cache = cache
    return previous


def probe_models(
    models: Iterable[DeclaredModel],
    *,
    cache: ReachabilityCache | None = None,
) -> list[ReachabilityStatus]:
    """Probe every model, using the default cache unless overridden."""
    return (cache or get_default_cache()).get_many(models)


__all__ = [
    "MODEL_UNREACHABLE",
    "PLAYGROUND_LIVE_PING_ENV",
    "CredentialsProber",
    "LiteLLMPingProber",
    "ModelProber",
    "ReachabilityCache",
    "ReachabilityStatus",
    "get_default_cache",
    "probe_models",
    "set_default_cache",
]
