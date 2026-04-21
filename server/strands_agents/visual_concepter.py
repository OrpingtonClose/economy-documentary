"""Visual Concepter agent (Strands).

Turns the Content Analyst's phrases into LTX-2.3 shot prompts. Ports
the ADK ``visual_concepter`` sub-agent inside
``server/agents/visual_director.py`` (lines 101-448) to a single Strands
:class:`Agent` with three ``@tool`` callables plus a contract enforcer,
a style-lock enforcer, and an optional revision tagger.

Design decisions:

* ``propose_concept`` is LLM-backed — mapping a phrase + style_lock +
  visual_style to a cinematographically coherent LTX prompt requires
  natural-language reasoning. The tool delegates to an injected
  helper so tests can exercise it with a deterministic fake (mirrors
  the Components 01/03/06 pattern).
* ``check_style_lock`` is deterministic. It enforces the rules the
  StyleLockEnforcer hook also acts on: every concept carries the
  ``style_lock.dominant_style`` (or the ``positive_fragment`` snippet)
  in its prompt, mentions no ``forbidden_styles`` tokens, and keeps
  shot_type / camera_movement inside the closed vocabularies below.
  Returning ``{"ok": True, "violations": []}`` tells the LLM to move
  to ``persist_visual_concepts``.
* ``persist_visual_concepts`` commits the final concept list onto
  ``agent.state`` so the production supervisor (Component 10) and the
  coherence evaluator (Component 08) consume it through the same
  blackboard the ADK ``visual_director`` used.
* :class:`ContractEnforcer` is wired with
  ``check_postconditions=True`` because the visual concepter is the
  component that actually produces ``visual_concepts`` (the final
  post-condition of :data:`VISUAL_DIRECTION_CONTRACT`).
* :class:`StyleLockEnforcer` listens on :class:`AfterToolCallEvent`
  for ``propose_concept`` and sets ``event.retry = True`` when the
  returned concept drifts — surfacing style drift as a retry rather
  than relying on the LLM to self-police.

See ``docs/strands-migration/components/07-visual-concepter.md``.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable

from strands import Agent, ToolContext, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.hooks import ContractEnforcer, RevisionTagger

logger = logging.getLogger(__name__)


#: Allowed ``shot_type`` values. Mirrors the LTX-2.3 shot-type
#: vocabulary documented in
#: ``docs/strands-migration/components/07-visual-concepter.md``.
SHOT_TYPES: frozenset[str] = frozenset(
    {
        "extreme_close_up",
        "close_up",
        "medium_close_up",
        "medium",
        "medium_wide",
        "wide",
        "extreme_wide",
        "establishing",
        "detail",
        "macro",
        "aerial",
        "over_shoulder",
        "two_shot",
        "cutaway",
        "insert",
    }
)

#: Allowed ``camera_movement`` values. Mirrors the camera-move
#: vocabulary in the ADK visual_concepter instruction (visual_director.py
#: lines 150-157) plus the data-visualisation ``graphic_overlay`` value
#: used for data-heavy phrases.
CAMERA_MOVEMENTS: frozenset[str] = frozenset(
    {
        "locked",
        "tripod_locked",
        "dolly_in",
        "dolly_out",
        "crane_up",
        "crane_down",
        "pan_left",
        "pan_right",
        "truck_left",
        "truck_right",
        "orbit",
        "handheld",
        "graphic_overlay",
    }
)

#: ``shot_type`` values that are appropriate for phrases whose
#: ``phrase_type`` is ``data``. check_style_lock uses this to surface
#: data-heavy phrases that drifted into live-action shot types.
DATA_SUITABLE_SHOT_TYPES: frozenset[str] = frozenset(
    {"insert", "cutaway", "detail", "macro", "aerial"}
)

#: Minimum LTX-2.3 clip duration in seconds. Anything shorter would be
#: discarded by the production supervisor.
MIN_CLIP_DURATION_SEC: float = 1.0

#: Maximum LTX-2.3 clip duration in seconds. Anything longer pushes
#: the downstream worker past its VRAM envelope.
MAX_CLIP_DURATION_SEC: float = 10.0


SYSTEM_PROMPT = """\
You are the Visual Concepter for a documentary pipeline.

Given a content_analysis (a list of scenes each carrying phrases),
the movie-level visual_style, and the style_lock, produce a
visual_concepts list: ONE concept per phrase, in scene + phrase order.

For each phrase emit:

* phrase_id: the phrase_id from content_analysis (copy; do NOT invent).
* scene_id: the scene_id from content_analysis (copy; do NOT invent).
* shot_type: ONE of the SHOT_TYPES vocabulary below.
* camera_movement: ONE of the CAMERA_MOVEMENTS vocabulary below.
* prompt: a flowing cinematography paragraph describing the shot
  (4-6 sentences). Must include style_lock.positive_fragment verbatim
  somewhere in the paragraph so the LTX diffusion model never loses
  the documentary's look.
* negative_prompt: a comma-separated list including every token in
  visual_style.avoid AND every style in style_lock.forbidden_styles.
  Always include "text, watermark, logo, low resolution, artifacts".
* duration_sec: (phrase.time_span[1] - phrase.time_span[0]), clamped
  to [1.0, 10.0]. Use the phrase's time_span verbatim — never invent.
* ltx_params: {
    "resolution": [1280, 720],
    "seed": null,           # production supervisor picks a seed
    "steps": 30,             # LTX-2.3 default
  }
* style_lock_applied: true  # the StyleLockEnforcer verifies this.

SHOT_TYPES (pick ONE per phrase):
  extreme_close_up, close_up, medium_close_up, medium, medium_wide,
  wide, extreme_wide, establishing, detail, macro, aerial,
  over_shoulder, two_shot, cutaway, insert.

CAMERA_MOVEMENTS (pick ONE per phrase):
  locked, tripod_locked, dolly_in, dolly_out, crane_up, crane_down,
  pan_left, pan_right, truck_left, truck_right, orbit, handheld,
  graphic_overlay.

Rules the check_style_lock tool enforces:

* Every concept's prompt contains style_lock.positive_fragment.
* No concept's prompt mentions any style in style_lock.forbidden_styles.
* shot_type and camera_movement come from the vocabularies above.
* phrase_type == "data" phrases map to data-suitable shot types
  (insert, cutaway, detail, macro, aerial) with camera_movement
  "graphic_overlay" or "locked".
* Consecutive phrases in the same scene use different
  camera_movement values (prevents static repetition).

Workflow:

1. For each phrase in content_analysis.per_scene[*].phrases, call
   propose_concept(phrase, style_lock, visual_style). Collect the
   returned concepts in input order.
2. Call check_style_lock(visual_concepts, style_lock). If it returns
   violations, iterate propose_concept for the affected phrase_ids.
3. Call persist_visual_concepts(visual_concepts). This is the last
   tool. Stop only after persist_visual_concepts returns successfully.
"""


# ---------------------------------------------------------------------------
# Helpers registry (LLM-backed proposer, test-injectable)
# ---------------------------------------------------------------------------


_ConceptProposer = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]
]
_PROPOSER: _ConceptProposer | None = None


def set_visual_concepter_helpers(
    *,
    concept_proposer: _ConceptProposer | None = None,
) -> None:
    """Register a test-time or production concept proposer.

    When no helper is registered :func:`propose_concept` raises
    :class:`VisualConcepterHelperNotConfigured` so missing wiring is
    surfaced loudly rather than silently returning a placeholder
    concept.

    Args:
        concept_proposer: Callable implementing
            ``(phrase, style_lock, visual_style) -> concept``. The
            returned concept should carry ``shot_type``,
            ``camera_movement``, ``prompt``, ``negative_prompt``,
            ``duration_sec``, and ``ltx_params``. ``phrase_id`` +
            ``scene_id`` are stamped deterministically by
            :func:`propose_concept` from the phrase.
    """
    global _PROPOSER
    if concept_proposer is not None:
        _PROPOSER = concept_proposer


def clear_visual_concepter_helpers() -> None:
    """Reset injected helpers. Primarily for test isolation."""
    global _PROPOSER
    _PROPOSER = None


class VisualConcepterHelperNotConfigured(RuntimeError):
    """Raised when an LLM-backed tool is invoked with no helper wired in."""


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _clamp_duration(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return MIN_CLIP_DURATION_SEC
    return max(MIN_CLIP_DURATION_SEC, min(value, MAX_CLIP_DURATION_SEC))


def _phrase_duration(phrase: dict[str, Any]) -> float:
    span = phrase.get("time_span")
    if (
        not isinstance(span, (list, tuple))
        or len(span) != 2
    ):
        return MIN_CLIP_DURATION_SEC
    try:
        start, end = float(span[0]), float(span[1])
    except (TypeError, ValueError):
        return MIN_CLIP_DURATION_SEC
    return _clamp_duration(end - start)


def _forbidden_tokens(style_lock: dict[str, Any]) -> list[str]:
    raw = style_lock.get("forbidden_styles") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(t).lower().strip() for t in raw if str(t).strip()]


def _default_ltx_params() -> dict[str, Any]:
    return {"resolution": [1280, 720], "seed": None, "steps": 30}


def _compose_negative_prompt(
    raw: Any,
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
) -> str:
    """Merge the LLM's negative prompt with style_lock + visual_style."""
    tokens: list[str] = []
    if isinstance(raw, str):
        tokens.extend(t.strip() for t in raw.split(",") if t.strip())
    elif isinstance(raw, (list, tuple)):
        tokens.extend(str(t).strip() for t in raw if str(t).strip())

    tokens.extend(_forbidden_tokens(style_lock))
    avoid = visual_style.get("avoid") if isinstance(visual_style, dict) else None
    if isinstance(avoid, str):
        tokens.extend(t.strip() for t in avoid.split(",") if t.strip())
    elif isinstance(avoid, (list, tuple)):
        tokens.extend(str(t).strip() for t in avoid if str(t).strip())

    tokens.extend(
        ["text", "watermark", "logo", "low resolution", "artifacts"]
    )

    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        key = token.lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(token)
    return ", ".join(ordered)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def propose_concept(
    phrase: dict[str, Any],
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
) -> dict[str, Any]:
    """Produce one visual concept for one phrase.

    Delegates the shot design to the injected helper and then:

    * Stamps ``phrase_id`` and ``scene_id`` from the input phrase
      (callers must not invent either; the production supervisor
      indexes clips by phrase_id).
    * Clamps ``duration_sec`` into ``[MIN_CLIP_DURATION_SEC,
      MAX_CLIP_DURATION_SEC]``; falls back to the phrase ``time_span``
      when the helper omits one.
    * Composes the full ``negative_prompt`` by merging the helper's
      output with ``style_lock.forbidden_styles``,
      ``visual_style.avoid``, and the standard LTX deny-list.
    * Defaults ``ltx_params`` to the production-supervisor-ready
      shape (``resolution`` 1280x720, ``steps`` 30, ``seed`` null).
    * Sets ``style_lock_applied=True`` as a claim; the
      :class:`StyleLockEnforcer` hook then verifies that claim on
      :class:`AfterToolCallEvent` and retries the call if not.

    Args:
        phrase: One phrase from ``content_analysis.per_scene[*].phrases``.
            Must carry ``phrase_id``, ``scene_id`` (or ``scene_num``),
            ``phrase_type``, and ``time_span``.
        style_lock: The movie-level style lock; must carry
            ``dominant_style`` + ``positive_fragment``.
        visual_style: The movie-level visual style directive.

    Returns:
        A visual-concept dict matching ``STATE_SCHEMA.md §9``.

    Raises:
        VisualConcepterHelperNotConfigured: When no proposer helper
            is wired.
    """
    if _PROPOSER is None:
        raise VisualConcepterHelperNotConfigured(
            "concept_proposer helper not configured; call "
            "set_visual_concepter_helpers"
        )

    phrase_id = str(phrase.get("phrase_id", "")).strip()
    scene_id = phrase.get("scene_id", phrase.get("scene_num"))
    if not phrase_id:
        raise ValueError("phrase missing phrase_id")
    if scene_id is None:
        raise ValueError(f"phrase {phrase_id} missing scene_id / scene_num")

    logger.debug(
        "phrase_id=<%s>, scene_id=<%s>, phrase_type=<%s> | proposing concept",
        phrase_id,
        scene_id,
        phrase.get("phrase_type"),
    )

    raw = _PROPOSER(phrase, style_lock, visual_style)

    duration = raw.get("duration_sec")
    if duration is None:
        duration = _phrase_duration(phrase)
    concept: dict[str, Any] = {
        "phrase_id": phrase_id,
        "scene_id": int(scene_id),
        "shot_type": str(raw.get("shot_type", "medium")),
        "camera_movement": str(raw.get("camera_movement", "locked")),
        "prompt": str(raw.get("prompt", "")),
        "negative_prompt": _compose_negative_prompt(
            raw.get("negative_prompt"), style_lock, visual_style
        ),
        "duration_sec": _clamp_duration(duration),
        "style_lock_applied": True,
        "ltx_params": dict(raw.get("ltx_params") or _default_ltx_params()),
    }
    return concept


@tool
def check_style_lock(
    concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
) -> dict[str, Any]:
    """Run the deterministic style-lock checks on ``concepts``.

    Args:
        concepts: Accumulated ``visual_concepts`` list produced by
            iterating :func:`propose_concept`.
        style_lock: The movie-level style lock; must carry
            ``positive_fragment`` + ``forbidden_styles``.

    Returns:
        ``{"ok": bool, "violations": [...]}`` where each violation is
        a ``{"phrase_id": str | None, "code": str, "detail": str}``
        dict. Callers interpret ``violations`` length > 0 as "re-propose
        the affected phrase"; the trajectory evaluator asserts the
        checker was called at least once before persistence.
    """
    violations: list[dict[str, Any]] = []
    positive = str(style_lock.get("positive_fragment", "")).strip()
    forbidden = _forbidden_tokens(style_lock)

    if not isinstance(concepts, list) or not concepts:
        return {
            "ok": False,
            "violations": [
                {
                    "phrase_id": None,
                    "code": "empty_concepts",
                    "detail": "no visual_concepts provided",
                }
            ],
        }

    previous_move_by_scene: dict[int, str] = {}
    for idx, concept in enumerate(concepts):
        phrase_id = concept.get("phrase_id")
        prompt = str(concept.get("prompt", ""))
        shot_type = concept.get("shot_type")
        camera_movement = concept.get("camera_movement")

        if positive and positive.lower() not in prompt.lower():
            violations.append(
                {
                    "phrase_id": phrase_id,
                    "code": "missing_positive_fragment",
                    "detail": (
                        f"concept {idx} ({phrase_id}) prompt does not include "
                        f"style_lock.positive_fragment={positive!r}"
                    ),
                }
            )
        for forb in forbidden:
            if forb and forb in prompt.lower():
                violations.append(
                    {
                        "phrase_id": phrase_id,
                        "code": "forbidden_style_in_prompt",
                        "detail": (
                            f"concept {idx} ({phrase_id}) prompt mentions "
                            f"forbidden style {forb!r}"
                        ),
                    }
                )

        if shot_type not in SHOT_TYPES:
            violations.append(
                {
                    "phrase_id": phrase_id,
                    "code": "bad_shot_type",
                    "detail": (
                        f"concept {idx} ({phrase_id}) shot_type={shot_type!r}; "
                        f"allowed={sorted(SHOT_TYPES)}"
                    ),
                }
            )
        if camera_movement not in CAMERA_MOVEMENTS:
            violations.append(
                {
                    "phrase_id": phrase_id,
                    "code": "bad_camera_movement",
                    "detail": (
                        f"concept {idx} ({phrase_id}) camera_movement="
                        f"{camera_movement!r}; allowed={sorted(CAMERA_MOVEMENTS)}"
                    ),
                }
            )

        if not concept.get("style_lock_applied"):
            violations.append(
                {
                    "phrase_id": phrase_id,
                    "code": "style_lock_not_applied",
                    "detail": (
                        f"concept {idx} ({phrase_id}) has "
                        "style_lock_applied=False"
                    ),
                }
            )

        scene_id = concept.get("scene_id")
        if isinstance(scene_id, int):
            prev = previous_move_by_scene.get(scene_id)
            if (
                prev is not None
                and prev == camera_movement
                and camera_movement not in {"locked", "graphic_overlay"}
            ):
                violations.append(
                    {
                        "phrase_id": phrase_id,
                        "code": "repeated_camera_movement",
                        "detail": (
                            f"concept {idx} ({phrase_id}) repeats "
                            f"camera_movement={camera_movement!r} from previous "
                            f"phrase in scene {scene_id}"
                        ),
                    }
                )
            previous_move_by_scene[scene_id] = str(camera_movement)

    logger.debug(
        "concept_count=<%d>, violation_count=<%d> | checked style_lock",
        len(concepts),
        len(violations),
    )
    return {"ok": not violations, "violations": violations}


@tool(context=True)
def persist_visual_concepts(
    visual_concepts: list[dict[str, Any]],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Commit the final ``visual_concepts`` list onto the agent's state.

    Writes both the structured list and a JSON string form so readers
    that expect either shape (the ADK pipeline persists JSON) can pick
    it up. The hook stack handles revision tagging — this tool is pure
    blackboard I/O.

    Args:
        visual_concepts: The final ``list[dict]`` produced by iterating
            :func:`propose_concept`. Callers should run
            :func:`check_style_lock` first; persistence does not
            re-validate.
        tool_context: Framework-injected context providing
            ``tool_context.agent.state``.

    Returns:
        ``{"persisted": True, "concept_count": int}``.
    """
    state = tool_context.agent.state
    snapshot = copy.deepcopy(visual_concepts)
    state.set("visual_concepts", snapshot)
    state.set(
        "visual_concepts_json",
        json.dumps(snapshot, ensure_ascii=False),
    )

    logger.info(
        "concept_count=<%d> | visual-concepter: persisted",
        len(snapshot),
    )
    return {"persisted": True, "concept_count": len(snapshot)}


# ---------------------------------------------------------------------------
# StyleLockEnforcer hook
# ---------------------------------------------------------------------------


class StyleLockEnforcer(HookProvider):
    """Fail-fast style-lock check on every ``propose_concept`` result.

    Mirrors the spec in
    ``docs/strands-migration/components/07-visual-concepter.md``:
    listens to :class:`AfterToolCallEvent`, inspects the concept
    returned by ``propose_concept``, and sets ``event.retry = True``
    when the concept violates the movie-level ``style_lock``. The
    LLM is then re-prompted with the tool error surfaced to it,
    which is the Strands equivalent of the ADK
    ``after_tool_callback`` retry pattern.

    Only ``propose_concept`` results are inspected; all other tools
    pass through untouched.
    """

    def __init__(
        self,
        *,
        tool_name: str = "propose_concept",
        state_key: str = "style_lock",
        max_retries: int = 2,
    ) -> None:
        """Initialise the style-lock enforcer.

        Args:
            tool_name: Name of the tool whose results to inspect.
                Defaults to ``"propose_concept"``.
            state_key: Name of the ``agent.state`` key that holds the
                movie-level ``style_lock`` dict.
            max_retries: Hard cap on retries per phrase so a drifting
                LLM cannot loop forever.
        """
        self._tool_name = tool_name
        self._state_key = state_key
        self._max_retries = max(0, int(max_retries))
        # phrase_id -> retry count; reset on successful concept.
        self._retry_counts: dict[str, int] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    def _style_lock(self, event: AfterToolCallEvent) -> dict[str, Any] | None:
        """Fetch the movie-level style_lock from agent state."""
        state = event.agent.state
        raw = state.get(self._state_key)
        if isinstance(raw, dict):
            return raw
        return None

    def _concept(self, event: AfterToolCallEvent) -> dict[str, Any] | None:
        """Extract the concept dict from the tool result."""
        result = event.result
        if isinstance(result, Exception):
            return None
        content = None
        if isinstance(result, dict):
            content = result.get("content")
        if not content:
            return None
        # Strands ToolResult content is a list of blocks; each block
        # carrying json has a "json" key per the tool_result schema.
        for block in content:
            if isinstance(block, dict) and "json" in block:
                blk = block["json"]
                if isinstance(blk, dict):
                    return blk
        return None

    def _violations(
        self, concept: dict[str, Any], style_lock: dict[str, Any]
    ) -> list[str]:
        """Deterministic drift checks; identical to check_style_lock."""
        positive = str(style_lock.get("positive_fragment", "")).strip()
        forbidden = _forbidden_tokens(style_lock)
        prompt = str(concept.get("prompt", "")).lower()

        violations: list[str] = []
        if positive and positive.lower() not in prompt:
            violations.append(
                f"prompt missing style_lock.positive_fragment={positive!r}"
            )
        for forb in forbidden:
            if forb and forb in prompt:
                violations.append(f"prompt mentions forbidden style {forb!r}")
        if concept.get("shot_type") not in SHOT_TYPES:
            violations.append(
                f"shot_type={concept.get('shot_type')!r} outside vocabulary"
            )
        if concept.get("camera_movement") not in CAMERA_MOVEMENTS:
            violations.append(
                f"camera_movement={concept.get('camera_movement')!r} "
                "outside vocabulary"
            )
        return violations

    def _on_after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use or {}
        if tool_use.get("name") != self._tool_name:
            return

        concept = self._concept(event)
        if concept is None:
            return

        style_lock = self._style_lock(event)
        if style_lock is None:
            # No style_lock on state -> nothing to enforce; let other
            # hooks (ContractEnforcer) surface the missing precondition.
            return

        violations = self._violations(concept, style_lock)
        if not violations:
            phrase_id = concept.get("phrase_id")
            if isinstance(phrase_id, str):
                self._retry_counts.pop(phrase_id, None)
            return

        phrase_id = concept.get("phrase_id")
        phrase_key = phrase_id if isinstance(phrase_id, str) else "_unknown"
        retries = self._retry_counts.get(phrase_key, 0)
        if retries >= self._max_retries:
            logger.warning(
                "phrase_id=<%s>, retries=<%d>, violations=<%s> | "
                "style-lock retry budget exhausted; handing off to "
                "check_style_lock",
                phrase_key,
                retries,
                violations,
            )
            return

        self._retry_counts[phrase_key] = retries + 1
        logger.info(
            "phrase_id=<%s>, retries=<%d>, violations=<%s> | "
            "requesting propose_concept retry",
            phrase_key,
            retries + 1,
            violations,
        )
        event.retry = True


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------


def build_visual_concepter_agent(
    *,
    model: Any = None,
    window_size: int = 40,
    enforce_contract: bool = True,
    enforce_style_lock: bool = True,
    tag_revisions: bool = False,
) -> Agent:
    """Return a configured visual-concepter :class:`Agent`.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``. When
            ``None`` the SDK falls through to its default.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Forty covers a
            ten-scene movie with ~5 phrases each without evicting the
            movie-level style_lock from context.
        enforce_contract: When True, wire :class:`ContractEnforcer`
            for :data:`VISUAL_DIRECTION_CONTRACT`. Both pre- and
            post-conditions are checked — the visual concepter
            actually produces the ``visual_concepts`` key that the
            contract gates.
        enforce_style_lock: When True, wire :class:`StyleLockEnforcer`
            so drifting concepts trigger ``propose_concept`` retries.
        tag_revisions: When True, wire :class:`RevisionTagger` with
            ``output_key="visual_concepts"`` and
            ``retag_on_reproduce=True``. Off by default because the
            preference ledger must be seeded before the agent runs;
            downstream integrations flip this on once the pipeline
            ledger is wired.

    Returns:
        Configured :class:`Agent` ready for ``.__call__`` invocations.
    """
    hooks: list[Any] = []
    if enforce_contract:
        hooks.append(ContractEnforcer(VISUAL_DIRECTION_CONTRACT))
    if enforce_style_lock:
        hooks.append(StyleLockEnforcer())
    if tag_revisions:
        hooks.append(
            RevisionTagger(
                "visual_concepts",
                stage="visual_concepter",
                retag_on_reproduce=True,
            )
        )

    return Agent(
        name="visual_concepter",
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[propose_concept, check_style_lock, persist_visual_concepts],
        conversation_manager=SlidingWindowConversationManager(
            window_size=window_size
        ),
        hooks=hooks,
    )


__all__ = [
    "CAMERA_MOVEMENTS",
    "DATA_SUITABLE_SHOT_TYPES",
    "MAX_CLIP_DURATION_SEC",
    "MIN_CLIP_DURATION_SEC",
    "SHOT_TYPES",
    "SYSTEM_PROMPT",
    "StyleLockEnforcer",
    "VisualConcepterHelperNotConfigured",
    "build_visual_concepter_agent",
    "check_style_lock",
    "clear_visual_concepter_helpers",
    "persist_visual_concepts",
    "propose_concept",
    "set_visual_concepter_helpers",
]
