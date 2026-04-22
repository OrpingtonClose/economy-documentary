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
    Component,
    DeclaredModel,
    EvaluatorDeclaration,
    get_component,
    iter_components,
)

__all__ = [
    "COMPONENT_IDS",
    "Component",
    "CredentialsProber",
    "DeclaredModel",
    "EvaluatorDeclaration",
    "MODEL_UNREACHABLE",
    "ModelProber",
    "ReachabilityCache",
    "ReachabilityStatus",
    "get_component",
    "get_default_cache",
    "iter_components",
    "probe_models",
    "set_default_cache",
]
