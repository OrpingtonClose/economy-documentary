"""Component 02 atom — timing evaluator.

One pure atom: :func:`compute_timing_report`. Given a scenes list plus a
WhisperX alignment, return whether narration fits the target duration
and a per-scene breakdown. No IO, fully deterministic.

This is the function the timing-loop connector (component 05) calls
after every audio render pass. The loop itself is not pure — it
launches audio renders, awaits them, and decides whether to refine.
Only this scoring step is.
"""

from __future__ import annotations

from strands_agents.timing_tool import compute_timing_report

__all__ = ["compute_timing_report"]
