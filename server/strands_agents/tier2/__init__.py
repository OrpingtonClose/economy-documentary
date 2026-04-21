"""Tier-2 atomic-robustness eval harness.

The tier-2 suites grade each documentary component (01-15) against
real corpus artifacts, using the JudgeEnsemble when credentials are
available and structural sanity checks otherwise.  Each component
owns one pytest module under ``tests/unit/tier2/`` that parametrises
over the component's corpus entries.

Two modes:

- **Hermetic** (default, every PR): artifacts are loaded from the
  committed seed corpus; tests assert provenance, expected_verdict
  coverage, and that a golden+adversarial pair exists.  No network,
  no GPU, no judges.  Fast CI gate that catches corpus regressions.
- **Live** (``--tier2-live``, nightly): artifacts are routed through
  :class:`strands_agents.judges.ensemble.JudgeEnsemble` with a
  component-specific rubric.  The verdict is compared against the
  artifact's ``expected_verdict``.  Requires Gemma-4 / Qwen3.5-Omni /
  video-SALMONN-2 reachable (or proprietary fallback creds).

This package exposes :func:`load_tier2_cases` and
:class:`Tier2Case` so test modules share one authoring pattern.
"""

from __future__ import annotations

from strands_agents.tier2.harness import (
    Tier2Case,
    Tier2Mode,
    assert_hermetic,
    build_judge_request,
    load_tier2_cases,
    normalise_verdict,
    verdict_matches,
)
from strands_agents.tier2.rubrics import RUBRICS, Rubric, get_rubric

__all__ = [
    "RUBRICS",
    "Rubric",
    "Tier2Case",
    "Tier2Mode",
    "assert_hermetic",
    "build_judge_request",
    "get_rubric",
    "load_tier2_cases",
    "normalise_verdict",
    "verdict_matches",
]
