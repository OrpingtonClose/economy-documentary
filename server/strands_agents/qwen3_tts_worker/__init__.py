"""Qwen3-TTS worker module.

Runs on a Vast.ai GPU VM alongside the :mod:`strands_agents.infra_agent`
guardian. Exposes a minimal FastAPI surface for the playground / pipeline
to render speech against a single pinned voice, and self-registers with
the playground worker registry on boot.

One TTS voice per VM is a hard invariant (enforced by the registry on
``POST /playground/workers``). Voice assignment is passed at boot via the
``WORKER_VOICE_ID`` env var and never changes for the life of the VM.

Shape per Wave 2 slice 4b of the next-wave plan:

* :class:`engine.TTSEngine` — protocol every backend implementation
  satisfies (real Qwen3-TTS, stubs for tests).
* :class:`engine.StubTTSEngine` — deterministic in-memory synth for
  unit tests; generates a silent WAV of the requested duration.
* :func:`app.build_app` — FastAPI factory. Middleware bumps the local
  infra agent on every request so active traffic pins the VM alive.
* :class:`registry_client.PlaygroundRegistryClient` — full CRUD against
  ``/playground/workers`` (register + heartbeat + pin_voice + unregister).
* :func:`runner.main` — production entry-point. Resolves env, registers
  with the playground, spawns a heartbeat thread, runs uvicorn.
"""

from __future__ import annotations

from .app import build_app
from .bump_client import InfraAgentBumpClient, bump_infra_agent
from .engine import (
    StubTTSEngine,
    SynthesisRequest,
    SynthesisResult,
    TTSEngine,
    TTSEngineError,
)
from .registry_client import (
    PlaygroundRegistryClient,
    RegistryClientError,
    RegistryHeartbeatError,
    RegistryRegisterError,
    RegistryUnregisterError,
    RegistryVoicePinError,
)
from .runner import WorkerConfig, main

__all__ = [
    "InfraAgentBumpClient",
    "PlaygroundRegistryClient",
    "RegistryClientError",
    "RegistryHeartbeatError",
    "RegistryRegisterError",
    "RegistryUnregisterError",
    "RegistryVoicePinError",
    "StubTTSEngine",
    "SynthesisRequest",
    "SynthesisResult",
    "TTSEngine",
    "TTSEngineError",
    "WorkerConfig",
    "build_app",
    "bump_infra_agent",
    "main",
]
