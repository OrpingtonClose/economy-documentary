"""Infrastructure agent: per-VM control plane + cost guardian.

Every Vast.ai VM we provision (TTS workers in slice 4b, LTX-Video
workers in slice 5, future Langfuse replicas) installs this agent as a
long-running sidecar. It exposes a small HTTP surface on port
``29230`` that:

* **Keeps the VM alive** on any traffic (``POST /`` or any other
  request) — idle inactivity past ``GUARDIAN_IDLE_SECONDS`` triggers
  self-destruct.
* **Caps lifetime** regardless of traffic — ``GUARDIAN_MAX_LIFETIME_SECONDS``
  is an immutable ceiling set at boot.
* **Reports telemetry** — peak VRAM, peak disk, uptime — so the
  orchestrator can observe real footprint in the lessons ledger.
* **Self-destructs cleanly** — deregisters from the playground worker
  registry first, then calls ``vastai destroy instance <id>``, then
  exits.

The decision core in :mod:`~.guardian` is pure and deterministic; the
Vast.ai and registry clients are mockable for unit tests. The runner in
:mod:`~.runner` wires the parts together for production.

See ``docs/strands-migration/plans/next-wave.md`` (Wave 2 slice 4a) for
the design rationale.
"""

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
    DiskProber,
    ResourceTelemetry,
    TelemetrySnapshot,
    VramProber,
    shutil_disk_prober,
)
from strands_agents.infra_agent.vast_client import (
    VastAiClient,
    VastAiDestroyError,
)

__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_LIFETIME_SECONDS",
    "DestroyReason",
    "DiskProber",
    "GuardianConfig",
    "GuardianState",
    "PlaygroundRegistryClient",
    "RegistryDeregisterError",
    "ResourceTelemetry",
    "TelemetrySnapshot",
    "VastAiClient",
    "VastAiDestroyError",
    "VramProber",
    "build_app",
    "shutil_disk_prober",
    "should_destroy",
]
