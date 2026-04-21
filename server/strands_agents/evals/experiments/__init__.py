"""Experiment factories for strands_agents evals.

Each component PR adds a module here exporting a ``build_*`` factory
and a ``*_task`` callable, keeping experiment construction colocated
with the agent it tests.
"""

from .assembly import (
    ASSEMBLY_EXPERIMENT_NAME,
    assembly_task,
    build_assembly_experiment,
    cleanup_assembly_artifact_root,
)
from .smoke import build_smoke_experiment, smoke_task

__all__ = [
    "ASSEMBLY_EXPERIMENT_NAME",
    "assembly_task",
    "build_assembly_experiment",
    "build_smoke_experiment",
    "cleanup_assembly_artifact_root",
    "smoke_task",
]
