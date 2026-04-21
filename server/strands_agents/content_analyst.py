"""Content Analyst agent (Strands).

Parses each scene's narration into visually-meaningful *phrases*, tagging
them with phrase_type, narrative_weight, visual_intent, and the time
span within the whisperx alignment the phrase covers. Ports the ADK
``content_analyst`` sub-agent inside
``server/agents/visual_director.py`` (lines 42-94) to a single Strands
:class:`Agent` with three ``@tool`` callables plus contract / revision
hooks.

Design decisions:

* ``extract_phrases`` is LLM-backed — segmenting narration into
  visually-salient phrases requires natural-language reasoning. The
  tool delegates to an injected helper so tests can exercise it with a
  deterministic fake (mirrors the Component 01 / 03 pattern).
* ``validate_phrases`` is deterministic. It enforces the
  ``content_analysis`` schema documented in
  ``docs/strands-migration/contracts/STATE_SCHEMA.md`` section 8:
  every scene has >= 1 phrase, phrase_type and narrative_weight drawn
  from the enum vocabulary, monotonic non-overlapping time_spans
  clamped to the whisperx segment, scene 1 phrases contain a ``hook``
  weight, and the final scene contains a ``payoff``.
* ``persist_content_analysis`` commits the final structure onto
  ``agent.state`` so downstream components (visual concepter, coherence
  evaluator) consume it through the same blackboard the ADK
  ``visual_director`` used.
* :class:`ContractEnforcer` is wired with
  ``check_postconditions=False`` because the content analyst only
  reads ``scenes`` + ``whisperx_alignment`` (the VISUAL_DIRECTION
  preconditions) and does NOT produce ``visual_concepts`` itself —
  that is Component 07's responsibility.

See ``docs/strands-migration/components/06-content-analyst.md``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import Any, Callable

from strands import Agent, ToolContext, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.hooks import ContractEnforcer, RevisionTagger

logger = logging.getLogger(__name__)


#: Allowed ``phrase_type`` values. Mirrors
#: ``docs/strands-migration/contracts/STATE_SCHEMA.md`` section 8.
PHRASE_TYPES: frozenset[str] = frozenset(
    {"concept", "entity", "process", "transition", "data"}
)

#: Allowed ``narrative_weight`` values.
NARRATIVE_WEIGHTS: frozenset[str] = frozenset(
    {"hook", "build", "payoff", "connective"}
)

#: Minimum phrases every scene must emit; below this the downstream
#: visual concepter has nothing to shot-plan against.
MIN_PHRASES_PER_SCENE: int = 1


SYSTEM_PROMPT = """\
You are the Content Analyst for a documentary pipeline.

Given a scenes list and a whisperx alignment you produce a
content_analysis structure: for each scene, a list of PHRASES that the
visual concepter can shot-plan against.

A PHRASE is a short, visually-meaningful span of narration. Break each
scene's voices into phrases such that every phrase describes something
a cinematographer could frame: one subject, one action, one beat.

For each phrase emit:

* phrase_id: stable identifier derived from (scene_num, phrase_idx,
  phrase text). Call extract_phrases per scene and let it compute this
  for you — never invent phrase_ids yourself.
* text: the narration words the phrase covers (substring of the
  scene's voice text, whitespace-normalised).
* phrase_type: ONE of {concept, entity, process, transition, data}.
    - concept: an idea, belief, or abstract principle.
    - entity: a named actor, institution, place, or object.
    - process: a mechanism unfolding over time (chains of cause).
    - transition: a bridge between two ideas, times, or places.
    - data: a quantified statement (percentages, trends, comparisons).
* narrative_weight: ONE of {hook, build, payoff, connective}.
    - hook: captures attention at the start of a scene or movie.
    - build: sets up a concept the later payoff relies on.
    - payoff: the conclusion the build was aiming at.
    - connective: bridges phrases without carrying its own weight.
* visual_intent: one sentence describing what a shot covering this
  phrase should depict (NOT a camera direction — that is the visual
  concepter's job).
* word_span: [start_word_index, end_word_index) into the scene's
  joined voices[].text. Indices are zero-based and inclusive-exclusive.
* time_span: [start_sec, end_sec] within the whisperx alignment for
  this scene. Must lie entirely inside the scene's whisperx segment
  duration — extract_phrases will clamp for you if you drift.

Structural rules the validate_phrases tool enforces:

* Every scene emits AT LEAST one phrase.
* Scene 1 phrases include at least one with narrative_weight=hook.
* The final scene's phrases include at least one with
  narrative_weight=payoff.
* Phrase time_spans within a scene are monotonic and non-overlapping.
* phrase_type and narrative_weight values must come from the enum
  vocabularies above.

Workflow:

1. extract_phrases(scene, whisperx_segment) per scene. Collect the
   returned phrases into content_analysis.per_scene.
2. validate_phrases(content_analysis). If it reports issues, go back
   to extract_phrases for the affected scenes.
3. persist_content_analysis(content_analysis). This is the last tool.

Stop only after persist_content_analysis returns successfully.
"""


# ---------------------------------------------------------------------------
# Helpers registry (LLM-backed extractor, test-injectable)
# ---------------------------------------------------------------------------


_PhraseExtractor = Callable[
    [dict[str, Any], dict[str, Any], int], list[dict[str, Any]]
]
_EXTRACTOR: _PhraseExtractor | None = None


def set_content_analyst_helpers(
    *,
    phrase_extractor: _PhraseExtractor | None = None,
) -> None:
    """Register a test-time or production phrase extractor.

    When no helper is registered :func:`extract_phrases` raises
    :class:`ContentAnalystHelperNotConfigured` so missing wiring is
    surfaced loudly rather than silently returning an empty list.

    Args:
        phrase_extractor: Callable implementing
            ``(scene, whisperx_segment, max_phrases) -> list[phrase]``.
            Each returned phrase should contain ``text``,
            ``phrase_type``, ``narrative_weight``, ``visual_intent``,
            ``word_span``, ``time_span``. ``phrase_id`` is computed
            deterministically by :func:`extract_phrases` itself so the
            helper does not need to produce one.
    """
    global _EXTRACTOR
    if phrase_extractor is not None:
        _EXTRACTOR = phrase_extractor


def clear_content_analyst_helpers() -> None:
    """Reset injected helpers. Primarily for test isolation."""
    global _EXTRACTOR
    _EXTRACTOR = None


class ContentAnalystHelperNotConfigured(RuntimeError):
    """Raised when an LLM-backed tool is invoked with no helper wired in."""


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _phrase_id(scene_num: int, phrase_idx: int, text: str) -> str:
    """Derive a stable phrase_id from the scene + phrase identity.

    The digest is truncated to 10 hex chars (~40 bits) which is plenty
    of collision headroom for a single documentary run while keeping
    the identifier human-scannable in traces.
    """
    payload = f"{int(scene_num)}|{int(phrase_idx)}|{text.strip()[:80]}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"ph-{int(scene_num):02d}-{int(phrase_idx):02d}-{digest[:10]}"


def _scene_num(scene: dict[str, Any]) -> int:
    raw: Any = None
    for key in ("scene_num", "id", "scene_id"):
        if key in scene and scene[key] is not None:
            raw = scene[key]
            break
    if raw is None:
        raise ValueError("scene missing scene_num / id / scene_id")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"scene id must be int-convertible, got {raw!r}"
        ) from exc


def _segment_bounds(segment: dict[str, Any]) -> tuple[float, float]:
    """Extract ``(start_sec, end_sec)`` from a whisperx segment dict."""
    start = segment.get("start")
    end = segment.get("end")
    if start is None or end is None:
        raise ValueError(
            "whisperx segment missing start/end; got keys=<%s>"
            % sorted(segment.keys())
        )
    start_f = float(start)
    end_f = float(end)
    if end_f < start_f:
        raise ValueError(
            f"whisperx segment malformed: end=<{end_f}> < start=<{start_f}>"
        )
    return start_f, end_f


def _clamp_time_span(
    raw: Any, seg_start: float, seg_end: float
) -> list[float]:
    """Clamp ``[start, end]`` to ``[seg_start, seg_end]`` preserving order."""
    if (
        not isinstance(raw, (list, tuple))
        or len(raw) != 2
        or any(not isinstance(v, (int, float)) for v in raw)
    ):
        return [seg_start, seg_end]
    start = max(seg_start, min(float(raw[0]), seg_end))
    end = max(seg_start, min(float(raw[1]), seg_end))
    if end < start:
        end = start
    return [start, end]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def extract_phrases(
    scene: dict[str, Any],
    whisperx_segment: dict[str, Any],
    max_phrases: int = 6,
) -> dict[str, Any]:
    """Segment one scene's narration into visually-meaningful phrases.

    Delegates the actual segmentation to the injected helper (an LLM
    in production, a deterministic fake in tests) and then:

    * Stamps each returned phrase with a deterministic ``phrase_id``
      derived from ``(scene_num, phrase_idx, text)`` so re-runs produce
      the same identifiers for identical inputs.
    * Clamps every phrase's ``time_span`` to the whisperx segment
      bounds so downstream tooling never sees a phrase outside its
      scene's audio window.
    * Truncates the returned list to ``max_phrases`` to keep the
      downstream visual concepter within its output-token budget.

    Args:
        scene: The scene dict; must expose ``scene_num`` (or ``id`` /
            ``scene_id``) and a ``voices`` list.
        whisperx_segment: The whisperx alignment entry for this scene;
            must carry ``start`` and ``end`` seconds.
        max_phrases: Upper bound on the number of phrases returned.
            Defaults to six; below one raises :class:`ValueError`.

    Returns:
        ``{"scene_num": int, "phrases": [...]}`` where each phrase
        contains ``phrase_id``, ``text``, ``phrase_type``,
        ``narrative_weight``, ``visual_intent``, ``word_span``,
        ``time_span``.

    Raises:
        ContentAnalystHelperNotConfigured: When no extractor helper is
            wired.
        ValueError: When ``max_phrases`` is below one or the segment is
            missing bounds.
    """
    if _EXTRACTOR is None:
        raise ContentAnalystHelperNotConfigured(
            "phrase_extractor helper not configured; call "
            "set_content_analyst_helpers"
        )
    if max_phrases < 1:
        raise ValueError(f"max_phrases=<{max_phrases}> | must be >= 1")

    scene_num = _scene_num(scene)
    seg_start, seg_end = _segment_bounds(whisperx_segment)
    logger.debug(
        "scene_num=<%d>, seg_start=<%.3f>, seg_end=<%.3f>, max_phrases=<%d> | "
        "extracting phrases",
        scene_num,
        seg_start,
        seg_end,
        max_phrases,
    )

    raw = _EXTRACTOR(scene, whisperx_segment, max_phrases)
    phrases: list[dict[str, Any]] = []
    for idx, phrase in enumerate(raw[:max_phrases]):
        text = str(phrase.get("text", "")).strip()
        entry: dict[str, Any] = {
            "phrase_id": _phrase_id(scene_num, idx, text),
            "text": text,
            "phrase_type": phrase.get("phrase_type", "concept"),
            "narrative_weight": phrase.get("narrative_weight", "build"),
            "visual_intent": str(phrase.get("visual_intent", "")),
            "word_span": list(phrase.get("word_span", [0, 0])),
            "time_span": _clamp_time_span(
                phrase.get("time_span"), seg_start, seg_end
            ),
        }
        phrases.append(entry)

    return {"scene_num": scene_num, "phrases": phrases}


@tool
def validate_phrases(content_analysis: dict[str, Any]) -> dict[str, Any]:
    """Run the deterministic structural checks on ``content_analysis``.

    Args:
        content_analysis: The accumulated ``{"per_scene": [...]}``
            structure produced by iterating :func:`extract_phrases`.

    Returns:
        ``{"valid": bool, "issues": [...]}`` where each issue is a
        ``{"scene_num": int | None, "code": str, "detail": str}`` dict.
        Callers interpret ``issues`` length > 0 as "re-extract the
        affected scenes"; the trajectory evaluator asserts the
        validator was called at least once before persistence.
    """
    issues: list[dict[str, Any]] = []
    per_scene = content_analysis.get("per_scene")
    if not isinstance(per_scene, list) or not per_scene:
        issues.append(
            {
                "scene_num": None,
                "code": "empty_per_scene",
                "detail": "content_analysis.per_scene is missing or empty",
            }
        )
        return {"valid": False, "issues": issues}

    for scene_entry in per_scene:
        scene_num = scene_entry.get("scene_num")
        phrases = scene_entry.get("phrases") or []
        if len(phrases) < MIN_PHRASES_PER_SCENE:
            issues.append(
                {
                    "scene_num": scene_num,
                    "code": "no_phrases",
                    "detail": (
                        f"scene {scene_num} emitted {len(phrases)} phrases; "
                        f"need >= {MIN_PHRASES_PER_SCENE}"
                    ),
                }
            )
            continue

        prev_end: float | None = None
        for idx, phrase in enumerate(phrases):
            ptype = phrase.get("phrase_type")
            if ptype not in PHRASE_TYPES:
                issues.append(
                    {
                        "scene_num": scene_num,
                        "code": "bad_phrase_type",
                        "detail": (
                            f"scene {scene_num} phrase {idx} has "
                            f"phrase_type={ptype!r}; allowed={sorted(PHRASE_TYPES)}"
                        ),
                    }
                )
            weight = phrase.get("narrative_weight")
            if weight not in NARRATIVE_WEIGHTS:
                issues.append(
                    {
                        "scene_num": scene_num,
                        "code": "bad_narrative_weight",
                        "detail": (
                            f"scene {scene_num} phrase {idx} has "
                            f"narrative_weight={weight!r}; "
                            f"allowed={sorted(NARRATIVE_WEIGHTS)}"
                        ),
                    }
                )
            span = phrase.get("time_span")
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or not all(isinstance(v, (int, float)) for v in span)
            ):
                issues.append(
                    {
                        "scene_num": scene_num,
                        "code": "bad_time_span",
                        "detail": (
                            f"scene {scene_num} phrase {idx} has malformed "
                            f"time_span={span!r}"
                        ),
                    }
                )
                continue
            start, end = float(span[0]), float(span[1])
            if end < start:
                issues.append(
                    {
                        "scene_num": scene_num,
                        "code": "inverted_time_span",
                        "detail": (
                            f"scene {scene_num} phrase {idx} time_span "
                            f"end={end} < start={start}"
                        ),
                    }
                )
            if prev_end is not None and start < prev_end:
                issues.append(
                    {
                        "scene_num": scene_num,
                        "code": "overlapping_time_span",
                        "detail": (
                            f"scene {scene_num} phrase {idx} starts at "
                            f"{start} but previous phrase ended at {prev_end}"
                        ),
                    }
                )
            prev_end = end

    first_scene = per_scene[0]
    if not any(
        p.get("narrative_weight") == "hook"
        for p in (first_scene.get("phrases") or [])
    ):
        issues.append(
            {
                "scene_num": first_scene.get("scene_num"),
                "code": "missing_hook",
                "detail": "first scene must include a phrase with narrative_weight=hook",
            }
        )

    last_scene = per_scene[-1]
    if not any(
        p.get("narrative_weight") == "payoff"
        for p in (last_scene.get("phrases") or [])
    ):
        issues.append(
            {
                "scene_num": last_scene.get("scene_num"),
                "code": "missing_payoff",
                "detail": "last scene must include a phrase with narrative_weight=payoff",
            }
        )

    logger.debug(
        "scene_count=<%d>, issue_count=<%d> | validated content_analysis",
        len(per_scene),
        len(issues),
    )
    return {"valid": not issues, "issues": issues}


@tool(context=True)
def persist_content_analysis(
    content_analysis: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Commit the final ``content_analysis`` onto the agent's state.

    Writes both the structured mapping and a JSON string form so
    readers that expect either shape (the ADK pipeline persists JSON)
    can pick it up. The hook stack handles revision tagging — this
    tool is pure blackboard I/O.

    Args:
        content_analysis: The final ``{"per_scene": [...]}``
            structure. Callers should run :func:`validate_phrases`
            first; persistence does not re-validate.
        tool_context: Framework-injected context providing
            ``tool_context.agent.state``.

    Returns:
        ``{"persisted": True, "scene_count": int, "phrase_count": int}``.
    """
    state = tool_context.agent.state
    snapshot = copy.deepcopy(content_analysis)
    state.set("content_analysis", snapshot)
    state.set(
        "content_analysis_json",
        json.dumps(snapshot, ensure_ascii=False),
    )

    per_scene = snapshot.get("per_scene") or []
    phrase_count = sum(
        len(entry.get("phrases") or []) for entry in per_scene
    )
    logger.info(
        "scene_count=<%d>, phrase_count=<%d> | content-analyst: persisted",
        len(per_scene),
        phrase_count,
    )
    return {
        "persisted": True,
        "scene_count": len(per_scene),
        "phrase_count": phrase_count,
    }


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------


def build_content_analyst_agent(
    *,
    model: Any = None,
    window_size: int = 30,
    enforce_contract: bool = True,
    tag_revisions: bool = False,
) -> Agent:
    """Return a configured content-analyst :class:`Agent`.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``. When
            ``None`` the SDK falls through to its default.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Thirty covers a
            ten-scene movie's extract/validate cycle without evicting
            the original scene list from context.
        enforce_contract: When True, wire :class:`ContractEnforcer`
            for :data:`VISUAL_DIRECTION_CONTRACT` (preconditions only;
            content-analyst does not produce ``visual_concepts`` —
            that is Component 07's job).
        tag_revisions: When True, wire :class:`RevisionTagger` with
            ``output_key="content_analysis"`` and
            ``retag_on_reproduce=True``. Off by default because the
            preference ledger must be seeded before the agent runs;
            downstream integrations flip this on once the pipeline
            ledger is wired.

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
                "content_analysis",
                stage="content_analyst",
                retag_on_reproduce=True,
            )
        )

    return Agent(
        name="content_analyst",
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[extract_phrases, validate_phrases, persist_content_analysis],
        conversation_manager=SlidingWindowConversationManager(
            window_size=window_size
        ),
        hooks=hooks,
    )
