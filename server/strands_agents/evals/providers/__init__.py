"""Trace providers for post-hoc evaluation of production runs.

Wraps :mod:`strands_evals.providers` with graceful-degradation
behaviour: missing optional dependencies (``langfuse``) or missing
credentials produce a clear error at *call time*, not import time, so a
CI job can import the package freely and skip provider tests when the
environment is not wired up.
"""
