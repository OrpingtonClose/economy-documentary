"""Experiment factories for strands_agents evals.

Each component PR adds a module here exporting a ``build_*`` factory
and a ``*_task`` callable, keeping experiment construction colocated
with the agent it tests.
"""

from __future__ import annotations

from .approval import approval_task, build_approval_experiment
from .pipeline import build_pipeline_experiment, pipeline_task

__all__ = [
    "approval_task",
    "build_approval_experiment",
    "build_pipeline_experiment",
    "pipeline_task",
]
