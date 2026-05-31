"""Visual SubAgent declaration for Component 09 (visual-loop).

The visual loop is a cohesive domain — content analysis, visual concept
generation, and coherence evaluation iterate against each other before
anything is dispatched for GPU production. Keeping it in its own
``deepagents.SubAgent`` isolates the (large) per-scene context from the
main orchestrator and lets the loop use ``STRANDS_THINKER_MODEL`` if the
operator has configured one.

This module ships only the *declarative* SubAgent spec consumed by
Component 14's ``create_deep_agent(subagents=[...])`` call. The runtime
loop behaviour is evaluated via
``strands_agents.evals.experiments.visual_loop`` using trajectory
fixtures — no execution happens inside this module.

See ``docs/strands-migration/components/09-visual-loop.md``.
"""

from __future__ import annotations

from typing import Any

from strands_agents.coherence_evaluator import (
    persist_coherence_report,
    score_visual_coherence,
)
from strands_agents.content_analyst import (
    extract_phrases,
    persist_content_analysis,
    validate_phrases,
)
from strands_agents.visual_concepter import (
    check_style_lock,
    persist_visual_concepts,
    propose_concept,
)

#: Environment variable consulted by :func:`build_visual_subagent` to
#: override the model used by the SubAgent. Mirrors the convention used
#: elsewhere in the Strands package (see ``docs/strands-migration/
#: reference/DEEPAGENT_PATTERNS.md`` §3).
VISUAL_SUBAGENT_MODEL_ENV: str = "STRANDS_THINKER_MODEL"

#: Fallback model when ``VISUAL_SUBAGENT_MODEL_ENV`` is not set.
VISUAL_SUBAGENT_DEFAULT_MODEL: str = "openai/gpt-4o"

#: Hard ceiling on refine iterations. Mirrors the value documented in
#: ``docs/strands-migration/components/09-visual-loop.md`` — the SubAgent
#: must return the best-scoring set once this count is reached.
VISUAL_LOOP_MAX_ITERATIONS: int = 5

#: Rating vocabulary carried by :func:`score_visual_coherence`. Kept
#: in-sync with ``CoherenceRating`` in
#: ``strands_agents.coherence_evaluator``.
VISUAL_LOOP_PASS_RATINGS: frozenset[str] = frozenset({"EXCELLENT", "GOOD"})

_VISUAL_SUBAGENT_PROMPT_TEMPLATE: str = """\
You are the visual production planner for a documentary pipeline.

Given the accepted scenes (scenes.json) + style_lock + whisperx
alignment, produce per-scene visual concepts that cohere across the
whole piece. You are a SubAgent — the parent orchestrator owns
production dispatch. You MUST NOT call any launch_* tool.

Process:

1. For each scene in scenes.json, call ``extract_phrases`` with the
   scene dict and the matching whisperx segment. Collect the results
   into a ``content_analysis`` dict keyed by ``per_scene``.
2. Once all scenes have phrases, call ``validate_phrases`` over the
   full ``content_analysis`` to run the deterministic structural
   checks. If it reports violations, surface them to the parent
   orchestrator and stop — do NOT persist a failed analysis.
3. Call ``persist_content_analysis`` to commit the analysis on state.
4. For each phrase in ``content_analysis``, call ``propose_concept``
   with the phrase, the movie-level ``style_lock``, and
   ``visual_style``. Collect the concepts into an ordered list that
   matches phrase order.
5. Call ``check_style_lock`` once on the accumulated concept list.
6. Call ``persist_visual_concepts`` to commit the concepts on state.
7. Call ``score_visual_coherence`` with the concepts, style_lock, and
   content_analysis.
8. If the rating is GOOD or EXCELLENT, call
   ``persist_coherence_report`` and return a brief summary to the
   parent — including concepts_by_scene, the rating, and the
   per-scene issue list.
9. If the rating is FAIR or POOR, identify the scenes flagged in the
   report's ``issues`` list and re-run ``propose_concept`` for ONLY
   those phrases. Re-run ``check_style_lock``, then
   ``persist_visual_concepts``, then ``score_visual_coherence``.
   Repeat. Do NOT re-run ``extract_phrases``, ``validate_phrases``,
   or ``persist_content_analysis`` — the content analysis is fixed
   for the loop.
10. Stop when the verdict is GOOD/EXCELLENT, or after
    __MAX_ITERATIONS__ iterations. When the cap is reached, persist
    the best-scoring coherence report you have seen and delegate to
    the escalation SubAgent via the ``task`` tool with
    ``subagent_type="escalation"`` and a description summarising the
    unresolved drift.

Output contract:
- concepts_by_scene: dict mapping scene_num (int) to a list of
  concept dicts in phrase order.
- coherence_verdict: one of EXCELLENT / GOOD / FAIR / POOR.
- per_scene_report: list of {scene_num, issues, suggestions}.

You MUST NOT call any launch_* tool. Production dispatch is handled
by the parent orchestrator.
"""

VISUAL_SUBAGENT_PROMPT: str = _VISUAL_SUBAGENT_PROMPT_TEMPLATE.replace(
    "__MAX_ITERATIONS__", str(VISUAL_LOOP_MAX_ITERATIONS)
)


#: Tools the visual SubAgent is allowed to call. Ordered so the
#: extract→validate→persist bootstrap appears before the per-scene
#: concept proposal and coherence evaluation tools. Any ``launch_*``
#: tool is deliberately absent — see the prompt.
VISUAL_SUBAGENT_TOOLS: tuple[Any, ...] = (
    extract_phrases,
    validate_phrases,
    persist_content_analysis,
    propose_concept,
    check_style_lock,
    persist_visual_concepts,
    score_visual_coherence,
    persist_coherence_report,
)

#: Tool names a visual-loop trajectory should call. Exposed so tests
#: and the :class:`VisualLoopTrajectoryEvaluator` can assert against
#: the declared toolset without importing the tools directly.
VISUAL_SUBAGENT_TOOL_NAMES: tuple[str, ...] = (
    "extract_phrases",
    "validate_phrases",
    "persist_content_analysis",
    "propose_concept",
    "check_style_lock",
    "persist_visual_concepts",
    "score_visual_coherence",
    "persist_coherence_report",
)

#: Tools that belong to the one-shot content-analysis bootstrap and
#: must appear in iteration 1 only.
VISUAL_LOOP_BOOTSTRAP_TOOLS: frozenset[str] = frozenset(
    {"extract_phrases", "validate_phrases", "persist_content_analysis"}
)

#: Tools that appear once per iteration (bootstrap + every revision).
VISUAL_LOOP_ITERATION_TOOLS: frozenset[str] = frozenset(
    {
        "check_style_lock",
        "persist_visual_concepts",
        "score_visual_coherence",
        "persist_coherence_report",
    }
)


__all__ = []
