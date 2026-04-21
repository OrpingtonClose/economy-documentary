"""Experiment factories for strands_agents evals.

Each component PR adds a module here exporting a ``build_*`` factory
and a ``*_task`` callable, keeping experiment construction colocated
with the agent it tests.
"""

from __future__ import annotations

from .recovery import (
    RECOVERY_EXPERIMENT_NAME,
    build_recovery_classifier_contract_experiment,
    build_recovery_experiment,
    build_recovery_remanifester_contract_experiment,
    recovery_task,
)
from .smoke import build_smoke_experiment, smoke_task

__all__ = [
    "RECOVERY_EXPERIMENT_NAME",
    "build_recovery_classifier_contract_experiment",
    "build_recovery_experiment",
    "build_recovery_remanifester_contract_experiment",
    "build_smoke_experiment",
    "recovery_task",
    "smoke_task",
]
