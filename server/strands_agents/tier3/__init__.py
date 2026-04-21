"""Tier-3 end-to-end robustness harness.

Tier 3 runs the full documentary pipeline end-to-end and compares its
output against the ADK baseline for parity.  This is the most expensive
eval tier — it burns GPU budget, requires the live judge fleet, and
runs on-demand only.

Shipped in this PR
------------------
* :mod:`strands_agents.tier3.parity_diff` — OTIO parity diff
  between two pipeline outputs.  Hermetic, unit-testable, no fleet
  dependency.

Deferred to follow-up PRs
-------------------------
* Final-render judge rubric + :class:`JudgeEnsemble` wrapper for
  grading the produced ``.mp4``.  Waits on judge-fleet uptime.
* End-to-end harness that drives both the strands and the ADK paths
  on one topic.  Waits on live-worker provisioning glue.
* On-demand CI workflow.  Pending the two above.
"""

from strands_agents.tier3.parity_diff import (
    ParityDiff,
    ParityFinding,
    ParitySeverity,
    compare_timelines,
)

__all__ = [
    "ParityDiff",
    "ParityFinding",
    "ParitySeverity",
    "compare_timelines",
]
