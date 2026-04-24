"""Component Playground — read-only catalog + run surface.

This package exposes the 15 atomic components of the documentary
pipeline as individually addressable units for the standalone
``frontend-playground`` workbench. See
``docs/strands-migration/plans/component-playground.md`` for the full
plan and ``server/playground.py`` for the FastAPI router that mounts
this catalog onto the main app.

The playground intentionally does not own any component logic. It
imports the same ``*_cases()`` factories the CI experiments use and the
same evaluator stacks the runtime uses, so any change upstream flows
into the catalog without duplication.
"""

from strands_agents.playground.reachability import (
    MODEL_UNREACHABLE,
    CredentialsProber,
    ModelProber,
    ReachabilityCache,
    ReachabilityStatus,
    get_default_cache,
    probe_models,
    set_default_cache,
)
from strands_agents.playground.registry import (
    COMPONENT_IDS,
    INFRA_COMPONENT_IDS,
    Component,
    DeclaredModel,
    EvaluatorDeclaration,
    get_component,
    iter_components,
)
from strands_agents.playground.user_cases import (
    DEFAULT_USER_CASES_DIR,
    DuplicateCaseNameError,
    UserCase,
    VALID_ROLES,
    append_user_case,
    load_user_cases,
    preview_diff,
    user_cases_path,
)
from strands_agents.playground.worker_registry import (
    HEARTBEAT_STALE_SECONDS,
    NO_WORKERS_REGISTERED,
    VRAM_INSUFFICIENT,
    WORKER_ROLES,
    DuplicateWorkerError,
    NoWorkersRegisteredError,
    VoiceAlreadyPinnedError,
    VoiceOnNonTtsWorkerError,
    VramInsufficientError,
    VramProbeResult,
    VramProber,
    VramShortfall,
    Worker,
    WorkerAlreadyHasVoiceError,
    WorkerNotFoundError,
    WorkerRegistry,
    WorkerRegistryError,
    WorkerRole,
    get_default_registry,
    preflight_vram,
    set_default_registry,
    vram_insufficient_envelope,
)

__all__ = [
    "COMPONENT_IDS",
    "INFRA_COMPONENT_IDS",
    "Component",
    "CredentialsProber",
    "DEFAULT_USER_CASES_DIR",
    "DeclaredModel",
    "DuplicateCaseNameError",
    "DuplicateWorkerError",
    "EvaluatorDeclaration",
    "HEARTBEAT_STALE_SECONDS",
    "MODEL_UNREACHABLE",
    "ModelProber",
    "NO_WORKERS_REGISTERED",
    "NoWorkersRegisteredError",
    "ReachabilityCache",
    "ReachabilityStatus",
    "UserCase",
    "VALID_ROLES",
    "VRAM_INSUFFICIENT",
    "VoiceAlreadyPinnedError",
    "VoiceOnNonTtsWorkerError",
    "VramInsufficientError",
    "VramProbeResult",
    "VramProber",
    "VramShortfall",
    "WORKER_ROLES",
    "Worker",
    "WorkerAlreadyHasVoiceError",
    "WorkerNotFoundError",
    "WorkerRegistry",
    "WorkerRegistryError",
    "WorkerRole",
    "append_user_case",
    "get_component",
    "get_default_cache",
    "get_default_registry",
    "iter_components",
    "load_user_cases",
    "preflight_vram",
    "preview_diff",
    "probe_models",
    "set_default_cache",
    "set_default_registry",
    "user_cases_path",
    "vram_insufficient_envelope",
]
