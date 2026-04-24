"""LTX-Video worker module.

Runs on a Vast.ai GPU VM alongside the :mod:`strands_agents.infra_agent`
guardian. Exposes a minimal FastAPI surface for the playground / pipeline
to render per-scene video clips, and self-registers with the playground
worker registry on boot.

Shape per Wave 2 slice 5 of the next-wave plan:

* :class:`engine.VideoEngine` — protocol every backend implementation
  satisfies (real LTX-Video 2.3, stubs for tests).
* :class:`engine.StubVideoEngine` — deterministic in-memory renderer
  used by unit tests; emits a tiny valid MP4 header so the FastAPI,
  registry, and bump-middleware layers can be covered without any GPU
  or model weights in CI.
* :func:`app.build_app` — FastAPI factory. Middleware bumps the local
  infra agent on every request so active traffic pins the VM alive.
* :class:`registry_client.PlaygroundRegistryClient` — CRUD against
  ``/playground/workers`` (register + heartbeat + unregister). Unlike
  the TTS worker the video role does not pin a voice, so there is no
  ``pin_voice`` call.
* :func:`runner.main` — production entry-point. Resolves env, registers
  with the playground, spawns a heartbeat thread, runs uvicorn.

Sizing policy (per ``docs/strands-migration/lessons/gpu-sizing.md``):
first VMs overprovision on H200 / ~500 GB disk. Later slices optimise
downward after observing real peak VRAM / disk usage.
"""

from __future__ import annotations

from .app import build_app
from .bump_client import InfraAgentBumpClient, bump_infra_agent
from .engine import (
    RenderRequest,
    RenderResult,
    StubVideoEngine,
    VideoEngine,
    VideoEngineError,
)
from .registry_client import (
    PlaygroundRegistryClient,
    RegistryClientError,
    RegistryHeartbeatError,
    RegistryRegisterError,
    RegistryUnregisterError,
)
from .runner import WorkerConfig, main

__all__ = [
    "InfraAgentBumpClient",
    "PlaygroundRegistryClient",
    "RegistryClientError",
    "RegistryHeartbeatError",
    "RegistryRegisterError",
    "RegistryUnregisterError",
    "RenderRequest",
    "RenderResult",
    "StubVideoEngine",
    "VideoEngine",
    "VideoEngineError",
    "WorkerConfig",
    "build_app",
    "bump_infra_agent",
    "main",
]
