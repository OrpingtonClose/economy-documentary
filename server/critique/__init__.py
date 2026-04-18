"""Stylistic QA critique agents for the documentary pipeline.

This package implements ARCH-E3 (issue #149, absorbing #91 and #98):
stylistic QA invariants that run on every emitted narration block
regardless of which audio-ladder tier produced it. A block that passes
timing but fails a stylistic invariant re-enters the audio ladder with
the invariant violation as the failure signal.

Spec: ``docs/ARCHITECTURE_DIAGRAMS.md`` diagram 2.

Public surface:

- :mod:`server.critique.audio_invariants` — plain measurement callables
  (``tools=[...]`` on the ADK agent). Each callable is pure and operates
  on WAV paths plus optional prior-block context; it returns a
  :class:`InvariantResult` with a pass/fail verdict and measurement.
- :mod:`server.critique.stylistic_qa_agent` — the composing ADK
  ``Agent`` (subclass via blackboard-driven callbacks) and the
  stage-boundary ``after_agent_callback`` that raises the
  ladder-re-entry signal.
- :mod:`server.critique.ledger_override` — a scoped-override stub that
  consults the Preference Ledger (when ARCH-A4 lands) to suppress the
  uniform-LUFS invariant for a deliberate exception like "Cassandra
  louder in scene 3".
"""

from critique.audio_invariants import (
    InvariantResult,
    InvariantVerdict,
    InvariantViolation,
    NarrationBlock,
    check_character_voice_consistency,
    check_clicks,
    check_hiss_floor_continuity,
    check_peak_limiter,
    check_plosive_truncation,
    check_uniform_lufs,
    check_voice_continuity,
    run_all_invariants,
)
from critique.ledger_override import is_lufs_override_active
from critique.stylistic_qa_agent import (
    STYLISTIC_QA_OPERATION,
    StylisticInvariantFailure,
    build_stylistic_qa_agent,
    stylistic_qa_after_agent_callback,
)

__all__ = [
    "InvariantResult",
    "InvariantVerdict",
    "InvariantViolation",
    "NarrationBlock",
    "STYLISTIC_QA_OPERATION",
    "StylisticInvariantFailure",
    "build_stylistic_qa_agent",
    "check_character_voice_consistency",
    "check_clicks",
    "check_hiss_floor_continuity",
    "check_peak_limiter",
    "check_plosive_truncation",
    "check_uniform_lufs",
    "check_voice_continuity",
    "is_lufs_override_active",
    "run_all_invariants",
    "stylistic_qa_after_agent_callback",
]
