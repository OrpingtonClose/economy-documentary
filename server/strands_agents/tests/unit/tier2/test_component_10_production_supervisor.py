"""Tier-2 atomic-robustness eval for component 10 (production-supervisor).

Production supervisor decisions — accept/reject/fix/escalate decisions match cited QA signals.

Three test functions (auto-wired by the builder):

- ``test_corpus_seeded`` — asserts the corpus has a golden + adversarial
  pair for this component.  xfail-skipped when corpus coverage is
  incomplete so the gap is visible in CI without blocking the gate.
- ``test_hermetic_artifact_loads[key]`` — parametrised structural gate
  over every corpus artifact for this component.  Runs every PR.
- ``test_live_judge_matches_expected_verdict[key]`` — parametrised live
  judge roundtrip.  Gated behind ``--tier2-live``; runs nightly.
"""

from __future__ import annotations

from strands_agents.tests.unit.tier2._builder import build_component_tests

COMPONENT = "10-production-supervisor"

globals().update(build_component_tests(COMPONENT))
