"""Recovery agents — diagnostic classifier + remanifestation agent.

Component 12 of the Strands migration. Lives as a single module because
the two agents share recovery vocabulary and are only ever invoked by
the production supervisor (component 10) or the escalation supervisor
(component 13).

The tools are deterministic (regex-based classification, rule-based
concept revision) so unit tests and CI run without LLM credentials.
The Strands :class:`Agent` factories wrap the tools for production use
where the LLM can reason about ambiguous cases; when the heuristics
fire unambiguously the agent can return the classification directly
without extra model calls.

See ``docs/strands-migration/components/12-recovery-agents.md``.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any, Literal

from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

logger = logging.getLogger(__name__)

Classification = Literal["transient", "fixable", "persistent", "catastrophic"]

VALID_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"transient", "fixable", "persistent", "catastrophic"}
)

# ---------------------------------------------------------------------------
# Heuristic pattern tables
# ---------------------------------------------------------------------------
# Kept in this module (not imported from ``server/agents/diagnostic_classifier.py``)
# so the strands recovery agents are independently reviewable and don't
# inherit changes that the legacy ADK classifier might later make to
# the pattern surface. Patterns mirror the legacy ones but collapse to
# the 4-class recovery vocabulary per the component spec.

_TRANSIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cuda_oom", re.compile(r"\b(?:cuda\s+)?out\s+of\s+memory\b|\bOOM\b", re.I)),
    ("cuda_error", re.compile(r"\bCUDA\s*(?:error|runtime|driver)\b", re.I)),
    (
        "connection_reset",
        re.compile(
            r"\b(?:connection\s+(?:refused|reset|aborted)|ECONNRESET|ECONNREFUSED)\b",
            re.I,
        ),
    ),
    ("timeout", re.compile(r"\b(?:timed\s*out|TimeoutError|socket\.timeout)\b", re.I)),
    ("model_reload", re.compile(r"\bmodel\s+reload(?:ing)?\b", re.I)),
)

_FIXABLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "style_mismatch",
        re.compile(
            r"\b(?:output\s+style\s+does(?:n['\u2019]?t|\s+not)\s+match|"
            r"wrong\s+style|style_lock\s+violat)",
            re.I,
        ),
    ),
    (
        "prompt_incoherent",
        re.compile(
            r"\b(?:generation\s+incoherent|prompt\s+too\s+vague|"
            r"bad\s+prompt|prompt\s+rejected)\b",
            re.I,
        ),
    ),
    (
        "qa_rejected",
        re.compile(
            r"\b(?:QA[_\s]+(?:rejected|reject|hints?)|quality[:\s]+(?:rejected|poor)|"
            r"gatekeeper\s+rejected)\b",
            re.I,
        ),
    ),
    (
        "content_mismatch",
        re.compile(
            r"\b(?:wrong\s+subject|off[- ]topic|visual\s+does\s+not\s+match)\b",
            re.I,
        ),
    ),
)

_CATASTROPHIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "all_workers_500",
        re.compile(
            r"\b(?:all\s+workers\s+(?:returned\s+)?500|"
            r"no\s+workers\s+available|"
            r"worker\s+pool\s+exhausted)\b",
            re.I,
        ),
    ),
    ("disk_full", re.compile(r"\b(?:no\s+space\s+left|disk\s+full|ENOSPC)\b", re.I)),
    (
        "gpu_driver",
        re.compile(r"\bNVIDIA-SMI\s+has\s+failed|nvml\s+error\b", re.I),
    ),
)


def _match_hits(
    text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> list[str]:
    """Return names of patterns whose regex matches ``text``."""
    if not text:
        return []
    return [name for name, regex in patterns if regex.search(text)]


def _count_same_error(
    error: str, recent_history: list[dict[str, Any]]
) -> int:
    """Count how many prior entries share the same root error string."""
    if not error or not recent_history:
        return 0
    needle = error.strip().lower()
    count = 0
    for entry in recent_history:
        hay = str(entry.get("error", "")).strip().lower()
        if hay and (hay == needle or hay in needle or needle in hay):
            count += 1
    return count


def _apply_classification(
    *,
    transient_hits: list[str],
    fixable_hits: list[str],
    catastrophic_hits: list[str],
    repeat_count: int,
    single_worker_500: bool,
) -> tuple[Classification, str, str]:
    """Return ``(class_, hint, reason)`` for a set of signal hits."""
    if catastrophic_hits:
        return (
            "catastrophic",
            "halt-and-page-operator: infrastructure is broken",
            "catastrophic signals: " + ", ".join(catastrophic_hits),
        )
    # Escalate when the same fixable error repeats past the budget.
    if fixable_hits and repeat_count >= 2:
        return (
            "persistent",
            "remanifestation has not helped; escalate to operator",
            (
                f"same fixable error {repeat_count + 1}x "
                f"({', '.join(fixable_hits)})"
            ),
        )
    if fixable_hits:
        return (
            "fixable",
            "adjust prompt / params to address content failure",
            "fixable signals: " + ", ".join(fixable_hits),
        )
    if transient_hits:
        if single_worker_500 and repeat_count >= 2:
            return (
                "catastrophic",
                "worker returning 500 persistently — infra is down",
                "single-worker 500 repeated across retries",
            )
        return (
            "transient",
            "retry once with same params",
            "transient signals: " + ", ".join(transient_hits),
        )
    return (
        "fixable",
        "no signals matched; attempt one remanifestation before escalating",
        "no signals matched heuristic table",
    )


# ---------------------------------------------------------------------------
# Public tools — 12a diagnostic classifier
# ---------------------------------------------------------------------------


@tool
def classify(
    error: str,
    recent_history: list[dict[str, Any]],
    concept: dict[str, Any],
) -> dict[str, Any]:
    """Classify a failure event into the 4-class recovery vocabulary.

    Deterministic on identical inputs; mirrors the decision tree in
    ``docs/strands-migration/components/12-recovery-agents.md`` §12a.

    Args:
        error: Raw error message from the failing worker / tool.
        recent_history: Prior failure entries for the same artifact.
            Each entry should carry an ``"error"`` key. Used to detect
            ``persistent`` (same fixable error 3+ times) and
            ``catastrophic`` (single worker returning 500 repeatedly).
        concept: The visual concept (or analogous payload) the failing
            stage was operating on. Currently unused by the heuristics
            but forwarded to the LLM fall-through step in production.

    Returns:
        ``{"class": ..., "hint": ..., "signals": [...], "reasoning": ...,
        "concept_scene_id": ...}``.
    """
    del concept  # reserved for the LLM fall-through step
    transient_hits = _match_hits(error, _TRANSIENT_PATTERNS)
    fixable_hits = _match_hits(error, _FIXABLE_PATTERNS)
    catastrophic_hits = _match_hits(error, _CATASTROPHIC_PATTERNS)
    repeat_count = _count_same_error(error, recent_history)
    single_worker_500 = "worker returned 500" in (error or "").lower()

    class_, hint, reason = _apply_classification(
        transient_hits=transient_hits,
        fixable_hits=fixable_hits,
        catastrophic_hits=catastrophic_hits,
        repeat_count=repeat_count,
        single_worker_500=single_worker_500,
    )
    logger.debug(
        "error=<%s>, class=<%s>, repeat_count=<%d> | recovery classify",
        (error or "")[:120],
        class_,
        repeat_count,
    )
    return {
        "class": class_,
        "hint": hint,
        "signals": [*catastrophic_hits, *fixable_hits, *transient_hits],
        "reasoning": reason,
        "repeat_count": repeat_count,
    }


@tool
def persist_classification(
    artifact_id: str, classification: dict[str, Any]
) -> dict[str, Any]:
    """Record a classification against an artifact.

    In production this writes to the Strands ``AgentState`` (and
    downstream to the preference ledger); in tests it is a pure
    function that returns a log-shaped envelope the
    :class:`RecoveryLogger` hook appends to ``recovery_log``.

    Args:
        artifact_id: Artifact the classification is about.
        classification: Payload returned by :func:`classify`.

    Returns:
        ``{"artifact_id": ..., "classification": ..., "persisted": True}``.
    """
    if not artifact_id:
        raise ValueError("artifact_id is required")
    if not classification or "class" not in classification:
        raise ValueError("classification must include 'class'")
    if classification["class"] not in VALID_CLASSIFICATIONS:
        raise ValueError(
            f"unknown classification: {classification['class']!r}"
        )
    return {
        "artifact_id": artifact_id,
        "classification": dict(classification),
        "persisted": True,
    }


# ---------------------------------------------------------------------------
# Public tools — 12b remanifestation agent
# ---------------------------------------------------------------------------

_PRESERVED_FIELDS: tuple[str, ...] = (
    "phrase_id",
    "scene_id",
    "duration_sec",
    "style_lock_applied",
)


_STYLE_FALLBACK_NEGATIVES = (
    "off-topic, text overlay, watermark, low quality, deformed, generic stock footage"
)


def _revise_prompt(original: str, hint: str) -> str:
    """Return a revised prompt string based on ``hint``."""
    base = (original or "").strip()
    hint_lower = (hint or "").lower()
    if "style" in hint_lower or "cinematic" in hint_lower:
        suffix = ", cinematic documentary style, film grain, 35mm lens"
    elif "vague" in hint_lower or "incoherent" in hint_lower:
        suffix = ", highly detailed, clear subject, well-composed"
    elif "subject" in hint_lower or "mismatch" in hint_lower:
        suffix = ", subject centered in frame, matches narration"
    else:
        suffix = ", higher quality, refined details"
    if suffix.strip(", ") in base.lower():
        return base
    return f"{base}{suffix}" if base else suffix.lstrip(", ")


def _revise_negative_prompt(original: str) -> str:
    base = (original or "").strip()
    if not base:
        return _STYLE_FALLBACK_NEGATIVES
    # Deduplicate tokens.
    existing = {tok.strip().lower() for tok in base.split(",") if tok.strip()}
    added = [
        tok
        for tok in _STYLE_FALLBACK_NEGATIVES.split(", ")
        if tok.strip().lower() not in existing
    ]
    if not added:
        return base
    return f"{base}, {', '.join(added)}"


@tool
def propose_revised_concept(
    original_concept: dict[str, Any],
    error: str,
    hint: str,
    style_lock: dict[str, Any],
) -> dict[str, Any]:
    """Return a revised visual concept addressing the failure cause.

    Deterministic rule-based revision. Preserves ``phrase_id``,
    ``scene_id``, ``duration_sec``, ``style_lock_applied``. Always
    emits at least one meaningful change to prompt or negative_prompt.

    Args:
        original_concept: The concept that failed.
        error: Raw error message (for logging).
        hint: ``classification.hint`` from :func:`classify`.
        style_lock: Pipeline-wide style constraints that the revision
            must still honor.

    Returns:
        A new ``dict`` suitable for the ``visual_concepts[]`` state
        slot. Keys preserved from ``original_concept``:
        ``phrase_id``, ``scene_id``, ``duration_sec``,
        ``style_lock_applied``.
    """
    del error  # logged upstream
    if not original_concept:
        raise ValueError("original_concept is required")
    for key in ("phrase_id", "scene_id"):
        if key not in original_concept:
            raise ValueError(f"original_concept missing required field: {key}")

    revised = deepcopy(original_concept)
    revised["prompt"] = _revise_prompt(
        original_concept.get("prompt", ""), hint
    )
    revised["negative_prompt"] = _revise_negative_prompt(
        original_concept.get("negative_prompt", "")
    )
    # Honor style lock: if the style lock demands a specific camera
    # movement and the original wasn't carrying one, apply it.
    lock_camera = (style_lock or {}).get("camera_movement")
    if lock_camera and not revised.get("camera_movement"):
        revised["camera_movement"] = lock_camera
    revised["style_lock_applied"] = True

    # Ensure preserved fields are intact (defensive — ``deepcopy``
    # already carries them; this catches callers that mutate in place).
    for key in _PRESERVED_FIELDS:
        if key in original_concept:
            revised[key] = original_concept[key]
    return revised


@tool
def diff_concept(
    original: dict[str, Any], revised: dict[str, Any]
) -> dict[str, Any]:
    """Return the set of fields that changed between ``original`` and ``revised``.

    Args:
        original: The concept before revision.
        revised: The concept after revision.

    Returns:
        ``{"changed_fields": [...], "preserved_fields": [...]}``. A field
        is counted as changed when it exists in either dict with
        differing values (or only in one of them).
    """
    changed: list[str] = []
    preserved: list[str] = []
    keys = set(original.keys()) | set(revised.keys())
    for key in sorted(keys):
        if original.get(key) != revised.get(key):
            changed.append(key)
        else:
            preserved.append(key)
    return {"changed_fields": changed, "preserved_fields": preserved}


# ---------------------------------------------------------------------------
# Agent factories
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM_PROMPT = """You are the Diagnostic Classifier for a
documentary production pipeline. You classify failure events into EXACTLY
one of four recovery classes:

- ``transient`` — infrastructure hiccup; caller should retry. Signals:
  CUDA OOM, CUDA error, connection reset, timeout, model reload.
- ``fixable`` — wrong prompt / params / style; caller should remanifest.
  Signals: style mismatch, prompt incoherent, QA rejected, content mismatch.
- ``persistent`` — same fixable error 3+ times; escalate to operator.
- ``catastrophic`` — infrastructure broken; abort. Signals: all workers
  returning 500, disk full, GPU driver failure.

Rules:
- Always call ``classify`` first. If its output is unambiguous, return it.
- Call ``persist_classification`` exactly once per decision.
- Never invent a class. Never skip ``persist_classification``.
"""


_REMANIFEST_SYSTEM_PROMPT = """You are the Remanifestation Agent. Given a
``fixable`` classification + an original visual concept, you emit a revised
concept addressing the failure.

Rules:
- Always preserve ``phrase_id``, ``scene_id``, ``duration_sec``,
  ``style_lock_applied``. Never change them.
- Always honor ``style_lock`` — if the lock names a camera movement the
  concept must adopt it.
- Emit at least one meaningful change to prompt or negative_prompt.
- Call ``propose_revised_concept`` first, then ``diff_concept`` on
  ``(original, revised)``. Do NOT call ``propose_revised_concept`` twice.
"""


def build_diagnostic_classifier(
    model: Any = None,
    *,
    window_size: int = 40,
) -> Agent:
    """Construct the diagnostic-classifier Strands agent.

    Args:
        model: Strands :class:`Model` / model id. ``None`` defers to the
            Strands default (the agent can also be invoked via
            :func:`classify` directly, no LLM needed).
        window_size: Sliding window size — recovery may bounce between
            classifier and remanifester over several turns; default 40.

    Returns:
        Configured :class:`strands.Agent` with the two classifier tools.
    """
    kwargs: dict[str, Any] = {
        "tools": [classify, persist_classification],
        "system_prompt": _CLASSIFIER_SYSTEM_PROMPT,
        "conversation_manager": SlidingWindowConversationManager(
            window_size=window_size
        ),
    }
    if model is not None:
        kwargs["model"] = model
    return Agent(**kwargs)


def build_remanifestation_agent(
    model: Any = None,
    *,
    window_size: int = 20,
) -> Agent:
    """Construct the remanifestation Strands agent.

    Args:
        model: Strands :class:`Model` / model id. ``None`` defers to the
            Strands default.
        window_size: Sliding window size — a single remanifestation is
            usually one propose + one diff turn; default 20.

    Returns:
        Configured :class:`strands.Agent` with the two remanifestation tools.
    """
    kwargs: dict[str, Any] = {
        "tools": [propose_revised_concept, diff_concept],
        "system_prompt": _REMANIFEST_SYSTEM_PROMPT,
        "conversation_manager": SlidingWindowConversationManager(
            window_size=window_size
        ),
    }
    if model is not None:
        kwargs["model"] = model
    return Agent(**kwargs)


__all__ = [
    "VALID_CLASSIFICATIONS",
    "build_diagnostic_classifier",
    "build_remanifestation_agent",
    "classify",
    "diff_concept",
    "persist_classification",
    "propose_revised_concept",
]
