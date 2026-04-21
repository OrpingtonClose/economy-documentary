"""Coherence Evaluator agent (Strands).

LLM-as-judge gate inside the visual loop. Given the ``visual_concepts``
emitted by the visual concepter (Component 07), the movie-level
``style_lock``, and the ``content_analysis`` from the content analyst
(Component 06), rate the concept list along three axes (style
consistency, camera variety, narrative-visual alignment) and decide
whether the loop should converge or iterate.

Ports the ADK ``coherence_evaluator`` sub-agent inside
``server/agents/visual_director.py`` (lines 540-830) to a single
Strands :class:`Agent` with two ``@tool`` callables plus a contract
enforcer and an optional revision tagger.

Design decisions:

* ``score_visual_coherence`` layers a deterministic structural check
  over an LLM-backed soft judgement. The structural check surfaces
  hard invariants (style_lock forbidden style used in a concept,
  >3 consecutive identical shots, a phrase missing its concept); any
  hard violation forces the final rating to ``POOR`` no matter how
  generous the judge is. This mirrors the scenario agent (Component
  01) pattern where ``structural_checks`` hard-gate the evaluator.
* The soft judgement is delegated to an injected helper so tests can
  exercise it with a deterministic fake (mirrors Components 01/03/06/07).
  When no helper is registered :class:`CoherenceEvaluatorHelperNotConfigured`
  is raised; silent fallbacks are forbidden per AGENTS.md "fail loud".
* ``persist_coherence_report`` commits the rating + derived
  ``visual_coherence_passed`` bool onto ``agent.state`` so the DeepAgent
  orchestrator (Component 14) and the visual loop (Component 09)
  consume the outcome through the same blackboard the ADK
  ``visual_director`` used.
* :class:`ContractEnforcer` is wired with ``check_postconditions=False``
  because this stage only reads the VISUAL_DIRECTION preconditions
  (``scenes`` + ``whisperx_alignment``) — the concepter produces the
  actual ``visual_concepts`` postcondition. The rubric of "did we
  converge" lives in ``visual_coherence_passed``, not in the contract.

See ``docs/strands-migration/components/08-coherence-evaluator.md``.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable, Literal

from strands import Agent, ToolContext, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.hooks import ContractEnforcer, RevisionTagger

logger = logging.getLogger(__name__)


#: Allowed :data:`CritiqueRating` values for the evaluator. Aligned
#: with ``server/critique/record.py`` plus ``UNKNOWN`` for judge
#: failures (e.g. LLM error, empty concepts).
CoherenceRating = Literal["EXCELLENT", "GOOD", "FAIR", "POOR", "UNKNOWN"]

#: Ratings that count as "converged". The visual loop stops iterating
#: when :attr:`visual_coherence_passed` is True.
_PASSING_RATINGS: frozenset[str] = frozenset({"EXCELLENT", "GOOD"})

#: Maximum number of consecutive identical ``shot_type`` + ``camera_movement``
#: concepts before the structural check flags repetition. The ADK
#: coherence rubric allows short repetitive sequences for emphasis
#: (three identical shots is fine; four is monotony).
MAX_CONSECUTIVE_IDENTICAL_SHOTS: int = 3


SYSTEM_PROMPT = """\
You are the Coherence Evaluator for a documentary pipeline.

You receive a list of visual_concepts (ONE concept per phrase in
scene + phrase order), the movie-level style_lock, and the
content_analysis (scenes with phrases). You produce a holistic rating
of how well the concept list hangs together as a documentary's visual
plan.

Rate the concept list using EXACTLY ONE of the following labels:

* EXCELLENT — no issues: style is locked, camera varies deliberately,
  every phrase has a visually-aligned concept, transitions at scene
  boundaries are compatible.
* GOOD — at most 2 soft issues, no style_lock violations, no missing
  concepts, no repetitive-shot runs.
* FAIR — 3 to 5 soft issues OR exactly one style_lock violation OR
  one repetitive-shot run.
* POOR — any hard invariant is broken: a forbidden style token
  appears in a concept prompt, a phrase is missing its concept, or
  more than 3 consecutive concepts share the same shot_type and
  camera_movement.

Assess three axes:

1. Style consistency — every concept honours
   ``style_lock.positive_fragment`` and avoids every token in
   ``style_lock.forbidden_styles``.
2. Camera variety — consecutive concepts use different shot_types or
   camera_movements unless the scene narrates a deliberate emphasis
   beat (e.g. a montage).
3. Narrative-visual alignment — the concept for a phrase whose
   narrative_weight is ``hook`` is attention-grabbing; the concept
   for a phrase whose narrative_weight is ``payoff`` resolves the
   opening hook; concepts for ``data`` phrases use data-suitable
   shot types (insert, cutaway, detail, macro, aerial).

Workflow:

1. Call score_visual_coherence(visual_concepts, style_lock,
   content_analysis). It runs a deterministic structural check and
   combines that with your holistic judgement into a single
   {rating, issues, suggestions} dict.
2. Call persist_coherence_report(report). This is the last tool.
   Stop only after persist_coherence_report returns successfully.
"""


# ---------------------------------------------------------------------------
# Helpers registry (LLM-backed soft-scorer, test-injectable)
# ---------------------------------------------------------------------------


class CoherenceEvaluatorHelperNotConfigured(RuntimeError):
    """Raised when :func:`score_visual_coherence` runs without a helper.

    Tests and production wiring call :func:`set_coherence_evaluator_helpers`
    before invoking any tool. Raising instead of returning a placeholder
    keeps "helper silently missing" from masquerading as a clean POOR
    rating.
    """


_SoftScorer = Callable[
    [list[dict[str, Any]], dict[str, Any], dict[str, Any]],
    dict[str, Any],
]
_SCORER: _SoftScorer | None = None


def set_coherence_evaluator_helpers(
    *,
    soft_scorer: _SoftScorer | None = None,
) -> None:
    """Register a test-time or production soft scorer.

    The scorer receives ``(visual_concepts, style_lock, content_analysis)``
    and MUST return a dict with at least ``rating`` (one of
    :data:`CoherenceRating`) plus optional ``issues`` (``list[str]``)
    and ``suggestions`` (``list[str]``). The caller layers deterministic
    structural checks on top and takes the worst outcome.

    Args:
        soft_scorer: Callable that returns the judge's rating. Pass
            ``None`` to clear the registry.
    """
    global _SCORER
    _SCORER = soft_scorer
    logger.debug(
        "soft_scorer=<%s> | coherence evaluator helpers updated",
        "set" if soft_scorer is not None else "cleared",
    )


def clear_coherence_evaluator_helpers() -> None:
    """Reset the helper registry. Primarily used by tests."""
    set_coherence_evaluator_helpers(soft_scorer=None)


def _require_scorer() -> _SoftScorer:
    if _SCORER is None:
        raise CoherenceEvaluatorHelperNotConfigured(
            "no soft scorer registered; call "
            "set_coherence_evaluator_helpers(soft_scorer=...) before "
            "invoking coherence-evaluator tools",
        )
    return _SCORER


# ---------------------------------------------------------------------------
# Deterministic structural checks
# ---------------------------------------------------------------------------


def _forbidden_tokens(style_lock: dict[str, Any]) -> list[str]:
    """Normalised lowercase list of forbidden style tokens."""
    raw = style_lock.get("forbidden_styles") or []
    tokens: list[str] = []
    for tok in raw:
        if isinstance(tok, str):
            norm = tok.strip().lower()
            if norm:
                tokens.append(norm)
    return tokens


def _expected_phrase_ids(content_analysis: dict[str, Any]) -> list[str]:
    """Return the list of phrase_ids across all scenes in order."""
    per_scene = content_analysis.get("per_scene") or []
    ids: list[str] = []
    for scene in per_scene:
        phrases = scene.get("phrases") or []
        for phrase in phrases:
            pid = phrase.get("phrase_id")
            if isinstance(pid, str) and pid:
                ids.append(pid)
    return ids


def _structural_violations(
    visual_concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
    content_analysis: dict[str, Any],
) -> list[str]:
    """Return the list of HARD-invariant violations.

    A non-empty list forces the final rating to ``POOR`` regardless of
    the soft scorer's opinion. Hard invariants:

    * Every phrase_id in ``content_analysis`` appears exactly once in
      the concept list (no missing, no duplicates).
    * No concept's ``prompt`` mentions a token in
      ``style_lock.forbidden_styles``.
    * No more than :data:`MAX_CONSECUTIVE_IDENTICAL_SHOTS` consecutive
      concepts share the same (``shot_type``, ``camera_movement``)
      pair.
    """
    violations: list[str] = []

    expected = _expected_phrase_ids(content_analysis)
    covered: dict[str, int] = {}
    for concept in visual_concepts:
        pid = concept.get("phrase_id")
        if isinstance(pid, str):
            covered[pid] = covered.get(pid, 0) + 1

    for pid in expected:
        if pid not in covered:
            violations.append(f"phrase_id={pid!r} missing a visual concept")
    for pid, count in covered.items():
        if count > 1 and pid in expected:
            violations.append(
                f"phrase_id={pid!r} covered by {count} concepts (expected 1)"
            )
        if pid not in expected:
            violations.append(
                f"phrase_id={pid!r} not present in content_analysis"
            )

    forbidden = _forbidden_tokens(style_lock)
    for concept in visual_concepts:
        prompt = str(concept.get("prompt", "")).lower()
        for forb in forbidden:
            if forb and forb in prompt:
                violations.append(
                    f"concept for phrase_id="
                    f"{concept.get('phrase_id')!r} mentions forbidden "
                    f"style {forb!r}"
                )

    run_start = 0
    run_len = 1
    for idx in range(1, len(visual_concepts)):
        prev = visual_concepts[idx - 1]
        curr = visual_concepts[idx]
        prev_key = (prev.get("shot_type"), prev.get("camera_movement"))
        curr_key = (curr.get("shot_type"), curr.get("camera_movement"))
        if prev_key == curr_key and prev_key != (None, None):
            run_len += 1
            if run_len > MAX_CONSECUTIVE_IDENTICAL_SHOTS:
                violations.append(
                    f"consecutive concepts {run_start}..{idx} share "
                    f"shot_type={prev_key[0]!r} and "
                    f"camera_movement={prev_key[1]!r} (>"
                    f"{MAX_CONSECUTIVE_IDENTICAL_SHOTS} in a row)"
                )
                run_len = 1
                run_start = idx
        else:
            run_len = 1
            run_start = idx

    return violations


def _normalise_rating(value: Any) -> CoherenceRating:
    if not isinstance(value, str):
        return "UNKNOWN"
    candidate = value.strip().upper()
    if candidate in {"EXCELLENT", "GOOD", "FAIR", "POOR", "UNKNOWN"}:
        return candidate  # type: ignore[return-value]
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def score_visual_coherence(
    visual_concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
    content_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Score the visual concept list for holistic coherence.

    Runs a deterministic structural check first and merges it with
    the injected soft scorer's output. Any hard-invariant violation
    forces ``rating="POOR"``; otherwise the soft scorer's rating is
    returned with the union of both issue lists.

    Args:
        visual_concepts: The ordered concept list emitted by the
            visual concepter (Component 07).
        style_lock: Movie-level style lock (``positive_fragment``,
            ``forbidden_styles``, ``dominant_style``, ...).
        content_analysis: The content analyst's output
            (``{"per_scene": [...]}``) — used to confirm every phrase
            has exactly one concept.

    Returns:
        Dict with ``rating`` (one of :data:`CoherenceRating`),
        ``issues`` (``list[str]``), ``suggestions`` (``list[str]``),
        and ``visual_coherence_passed`` (``bool``).
    """
    if not visual_concepts:
        logger.info("concept_count=<0> | coherence: POOR (no concepts)")
        return {
            "rating": "POOR",
            "issues": ["visual_concepts is empty"],
            "suggestions": [
                "run visual concepter to populate visual_concepts",
            ],
            "visual_coherence_passed": False,
        }

    hard_violations = _structural_violations(
        visual_concepts, style_lock, content_analysis
    )

    scorer = _require_scorer()
    raw = scorer(visual_concepts, style_lock or {}, content_analysis or {})
    soft_rating = _normalise_rating(raw.get("rating"))
    soft_issues = [str(x) for x in (raw.get("issues") or []) if x]
    soft_suggestions = [
        str(x) for x in (raw.get("suggestions") or []) if x
    ]

    if hard_violations:
        rating: CoherenceRating = "POOR"
    else:
        rating = soft_rating if soft_rating != "UNKNOWN" else "FAIR"

    issues = hard_violations + soft_issues
    suggestions = soft_suggestions
    if hard_violations and not any(
        "style_lock" in s for s in suggestions
    ):
        suggestions = [
            "retry visual concepter for phrases flagged in issues",
            *suggestions,
        ]

    passed = rating in _PASSING_RATINGS
    logger.info(
        "rating=<%s>, issue_count=<%d>, hard=<%d> | coherence scored",
        rating,
        len(issues),
        len(hard_violations),
    )
    return {
        "rating": rating,
        "issues": issues,
        "suggestions": suggestions,
        "visual_coherence_passed": passed,
    }


@tool(context=True)
def persist_coherence_report(
    report: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Commit the final ``visual_coherence_report`` onto agent state.

    Writes three keys to the blackboard: ``visual_coherence_report``
    (the full mapping), ``visual_coherence_report_json`` (pre-serialised
    string form for readers that expect JSON), and
    ``visual_coherence_passed`` (the derived bool the visual loop
    checks). Persistence does not re-validate; callers should run
    :func:`score_visual_coherence` first.

    Args:
        report: The final ``{rating, issues, suggestions,
            visual_coherence_passed}`` mapping.
        tool_context: Framework-injected context providing
            ``tool_context.agent.state``.

    Returns:
        ``{"persisted": True, "rating": str, "passed": bool,
        "issue_count": int}``.
    """
    state = tool_context.agent.state
    snapshot = copy.deepcopy(report)
    rating = _normalise_rating(snapshot.get("rating"))
    snapshot["rating"] = rating
    passed = bool(snapshot.get("visual_coherence_passed"))
    # Re-derive passed from rating so a malformed caller-supplied
    # bool cannot disagree with the rating.
    passed = rating in _PASSING_RATINGS
    snapshot["visual_coherence_passed"] = passed

    state.set("visual_coherence_report", snapshot)
    state.set(
        "visual_coherence_report_json",
        json.dumps(snapshot, ensure_ascii=False),
    )
    state.set("visual_coherence_passed", passed)

    issue_count = len(snapshot.get("issues") or [])
    logger.info(
        "rating=<%s>, passed=<%s>, issue_count=<%d> | coherence persisted",
        rating,
        passed,
        issue_count,
    )
    return {
        "persisted": True,
        "rating": rating,
        "passed": passed,
        "issue_count": issue_count,
    }


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------


def build_coherence_evaluator_agent(
    *,
    model: Any = None,
    window_size: int = 10,
    enforce_contract: bool = True,
    tag_revisions: bool = False,
) -> Agent:
    """Return a configured coherence-evaluator :class:`Agent`.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``. When
            ``None`` the SDK falls through to its default.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Ten covers a
            single score → persist cycle; the evaluator rarely iterates.
        enforce_contract: When True, wire :class:`ContractEnforcer`
            for :data:`VISUAL_DIRECTION_CONTRACT` (preconditions only;
            the visual concepter produces the postcondition
            ``visual_concepts``, not this stage).
        tag_revisions: When True, wire :class:`RevisionTagger` with
            ``output_key="visual_coherence_report"``. Off by default
            because the preference ledger must be seeded before the
            agent runs; downstream integrations flip this on once the
            pipeline ledger is wired.

    Returns:
        Configured :class:`Agent` ready for ``.__call__`` invocations.
    """
    hooks: list[Any] = []
    if enforce_contract:
        hooks.append(
            ContractEnforcer(
                VISUAL_DIRECTION_CONTRACT,
                check_postconditions=False,
            )
        )
    if tag_revisions:
        hooks.append(
            RevisionTagger(
                "visual_coherence_report",
                stage="coherence_evaluator",
                retag_on_reproduce=True,
            )
        )

    return Agent(
        name="coherence_evaluator",
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[score_visual_coherence, persist_coherence_report],
        conversation_manager=SlidingWindowConversationManager(
            window_size=window_size
        ),
        hooks=hooks,
    )
