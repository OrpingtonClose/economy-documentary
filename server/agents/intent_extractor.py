"""Intent Extractor — parse the user's free-text brief into BriefIntent (R0).

INTENT-01 (#265, FATAL parent #263).  This module is the first producer
on every documentary run: before the scenario director writes a single
scene, the Intent Extractor parses the raw brief into a typed
:class:`BriefIntent` and writes it into:

1. ADK session state under :data:`BRIEF_INTENT_KEY` (JSON-serialised).
   Every downstream stage — scenario director, constraint gate
   (INTENT-02), per-stage verifier (INTENT-04), chat narrator —
   reads the typed intent through this single blackboard key.
2. The Preference Ledger as **R0** records via
   :mod:`callbacks.run_start_seed`.  The ledger stays the canonical
   store for scope-aware preference records; :class:`BriefIntent` is
   the structured *constraint* facet (duration, audience, required /
   forbidden topics) that the gates consume.

The extraction is fail-soft: a google-genai call is attempted when
credentials are present, with a deterministic regex-based heuristic as
fallback.  Either path must populate every hard-constraint field so the
gates are never left guessing.

Design invariants (enforced by ``tests/test_intent_extractor.py``):

* The PAG reference brief (``"Make a 7-minute ADHD-friendly documentary
  about the Periaqueductal Gray (PAG)..."``) parses to
  ``duration_sec == 420.0 ± 1`` regardless of LLM availability.
* Every parsed :class:`BriefIntent` has ``tolerance_sec > 0`` (default
  ``30.0`` seconds — the timing loop's soft budget) so the gate always
  has a finite window to validate against.
* ``confidence`` is a ``dict[str, float]`` keyed by field name with
  values in ``[0.0, 1.0]`` so downstream code can distinguish "derived
  from brief" from "defaulted".
* :func:`run_intent_extractor` is idempotent: re-running it on a state
  that already carries a ``BriefIntent`` short-circuits and returns the
  existing record, preserving the R0-before-anything-else invariant on
  B2-restore re-entry.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Blackboard key under which the extracted :class:`BriefIntent` is stored
#: (as a JSON string, matching the convention used by ``scenes`` /
#: ``preference_ledger`` in :mod:`callbacks.state_manager`).
BRIEF_INTENT_KEY: str = "brief_intent"

#: Default tolerance window applied when the brief doesn't mention one.
#: 30 seconds matches the timing loop's soft budget and is the largest
#: drift the constraint gate will accept without fail-closed halting.
DEFAULT_TOLERANCE_SEC: float = 30.0

#: Fallback duration for briefs where no numeric duration can be parsed.
#: Matches ``max_scene_duration`` heuristics elsewhere — a ~3-minute
#: short that the constraint gate will still validate against.
DEFAULT_DURATION_SEC: float = 180.0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class BriefIntent(BaseModel):
    """Typed representation of a user's documentary brief.

    Every field corresponds to a hard or soft constraint the pipeline
    will honour.  Hard constraints (``duration_sec``, ``required_topics``,
    ``forbidden_topics``, ``audience``) are checked fail-closed by the
    pre-flight gate (INTENT-02) and by per-stage verifiers (INTENT-04).
    """

    duration_sec: float = Field(
        ...,
        description=(
            "Target total documentary duration in seconds.  "
            "Hard constraint; the scenario director's scene sum must "
            "land within ± tolerance_sec."
        ),
        gt=0.0,
    )
    tolerance_sec: float = Field(
        default=DEFAULT_TOLERANCE_SEC,
        description=(
            "Symmetric tolerance window around duration_sec.  The "
            "constraint gate fails closed on drift larger than this."
        ),
        ge=0.0,
    )
    audience: str = Field(
        default="general",
        description=(
            "Target audience label (e.g. 'adhd-friendly', 'general', "
            "'expert').  Used by the scenario evaluator and per-stage "
            "verifier to check tonal fit."
        ),
    )
    tone: list[str] = Field(
        default_factory=list,
        description="Free-form tone hints, e.g. 'cinematic', 'playful'.",
    )
    corpus_paths: list[Path] = Field(
        default_factory=list,
        description=(
            "Paths to corpus documents supplied alongside the brief.  "
            "The visual-direction verifier (INTENT-04) checks clip "
            "grounding against these."
        ),
    )
    required_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Topics that MUST appear in the scenario.  Missing any of "
            "these triggers auto-critique retry in the pre-flight gate."
        ),
    )
    forbidden_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Topics that must NOT appear in the scenario.  Drift "
            "detected by the per-stage verifier is a fail-closed halt."
        ),
    )
    format_hints: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured hints about output format — aspect ratio, "
            "language, voice count, etc."
        ),
    )
    confidence: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-field confidence in [0, 1].  Values < 0.5 indicate "
            "the field was defaulted rather than derived from the brief."
        ),
    )

    @field_validator("corpus_paths", mode="before")
    @classmethod
    def _coerce_corpus_paths(cls, value: Any) -> list[Path]:
        if value is None:
            return []
        if isinstance(value, (str, Path)):
            return [Path(value)]
        return [Path(p) for p in value]

    def to_json(self) -> str:
        """JSON-serialise the intent for storage on the ADK blackboard."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str) -> "BriefIntent":
        return cls.model_validate_json(payload)


# ---------------------------------------------------------------------------
# LLM hook (pluggable for tests; mirrors A2 / A3 pattern).
# ---------------------------------------------------------------------------


LLMCallable = Callable[[str, str, str], str]
"""Minimal LLM call signature: ``(model, system_instruction, prompt) -> text``."""

_llm_client_factory: Optional[Callable[[], LLMCallable]] = None


def _default_llm_call(model: str, system: str, prompt: str) -> str:
    from google import genai  # optional dep
    from google.genai import types as genai_types

    client = genai.Client()
    config = genai_types.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        system_instruction=system,
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    return response.text or ""


_INTENT_MODEL_ENV = "INTENT_EXTRACTOR_MODEL"
_INTENT_DEFAULT_MODEL = "gemini-2.5-flash"


def _resolve_model() -> str:
    return os.environ.get(_INTENT_MODEL_ENV, _INTENT_DEFAULT_MODEL)


_INTENT_SYSTEM_INSTRUCTION = """\
You are the Intent Extractor for a documentary pipeline.  You receive
a free-text user brief and return a strict JSON object describing the
typed user intent.

HARD RULES:

1. Return a single JSON object with EXACTLY these keys:
     duration_sec        (float, seconds)
     tolerance_sec       (float, seconds; default 30.0 if not specified)
     audience            (string; 'general', 'adhd-friendly', 'expert', ...)
     tone                (array of strings)
     corpus_paths        (array of strings)
     required_topics     (array of strings)
     forbidden_topics    (array of strings)
     format_hints        (object)
     confidence          (object mapping field name -> number in [0,1])
2. duration_sec MUST be a positive float.  Convert phrases like
   "7-minute", "5 mins", "90 seconds" into seconds.
3. required_topics SHOULD include every proper noun or technical term
   the brief names as something the documentary must cover.  Do not
   invent topics not present in the brief.
4. Confidence < 0.5 means the field was defaulted rather than inferred.
5. No prose, no markdown fences, no trailing commentary."""


# ---------------------------------------------------------------------------
# Heuristic fallback — deterministic, always produces a valid BriefIntent.
# ---------------------------------------------------------------------------


_DURATION_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?\s*(?:minute|min)s?\b", re.I), 60.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?\s*(?:second|sec)s?\b", re.I), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?\s*(?:hour|hr)s?\b", re.I), 3600.0),
)

_ADHD_HINTS = ("adhd", "adhd-friendly", "neurodivergent", "short attention")
_EXPERT_HINTS = ("expert", "technical", "academic", "peer-review")

# Audience-attribute tokens that describe HOW the film should be presented,
# not subject matter the film must cover.  These must never leak into
# ``required_topics`` — otherwise the per-stage verifier hunts for the
# literal token in scene text and fails on perfectly good scenarios.
# Observed in run #3: the heuristic acronym extractor picked up "ADHD"
# from "7-minute ADHD-friendly documentary" and emitted it as both
# audience="adhd-friendly" AND required_topics=["ADHD"], which halted
# the whole pipeline because no neuroscience scene naturally says
# "ADHD".  The filter is applied to both heuristic and LLM output
# paths so either extraction source stays safe.
_AUDIENCE_STOPWORDS: frozenset[str] = frozenset(
    {
        "adhd",
        "adhd-friendly",
        "adhd friendly",
        "add",
        "neurodivergent",
        "neurotypical",
        "general",
        "expert",
        "technical",
        "academic",
        "layperson",
        "beginner",
        "child",
        "children",
        "kids",
    }
)


def _filter_required_topics(
    required: list[str], audience: str
) -> list[str]:
    """Drop audience-descriptor tokens from ``required``.

    A required topic must be subject matter the documentary covers
    (e.g. "PAG", "opioid analgesia"), not an audience attribute
    (e.g. "ADHD", "expert").  We filter against a fixed stopword set
    plus the detected ``audience`` label so every extraction path
    produces the same invariant.
    """
    audience_norm = (audience or "").strip().lower()
    cleaned: list[str] = []
    seen: set[str] = set()
    for topic in required:
        if not isinstance(topic, str):
            continue
        key = topic.strip().lower()
        if not key:
            continue
        if key in _AUDIENCE_STOPWORDS:
            continue
        if audience_norm and key == audience_norm:
            continue
        # Trim off "-friendly" variants like "adhd-friendly" that the
        # LLM sometimes emits as a topic.
        if audience_norm and key.startswith(audience_norm + "-"):
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(topic)
    return cleaned


def _heuristic_duration_sec(brief: str) -> tuple[float, float]:
    """Return ``(duration_sec, confidence)`` from the brief text."""
    for pattern, multiplier in _DURATION_PATTERNS:
        match = pattern.search(brief)
        if match:
            try:
                return float(match.group(1)) * multiplier, 0.9
            except (TypeError, ValueError):
                continue
    return DEFAULT_DURATION_SEC, 0.2


def _heuristic_audience(brief: str) -> tuple[str, float]:
    lower = brief.lower()
    for hint in _ADHD_HINTS:
        if hint in lower:
            return "adhd-friendly", 0.9
    for hint in _EXPERT_HINTS:
        if hint in lower:
            return "expert", 0.8
    return "general", 0.3


_REQUIRED_TOPIC_SPLIT = re.compile(r"\bmust cover\b|\bshould cover\b|\bcovering\b", re.I)
_FORBIDDEN_TOPIC_SPLIT = re.compile(
    r"\bmust not\b|\bdo not\b|\bdon'?t\b|\bno\b|\bavoid\b|\bexclude\b", re.I
)
_TOPIC_ITEM = re.compile(r"[A-Z][A-Za-z0-9 \-/'()]+(?=[,.;]|$)")
_PAREN_ACRONYM = re.compile(r"\b([A-Z][A-Z0-9]{1,9})\b")


def _heuristic_topics(brief: str) -> tuple[list[str], list[str], float]:
    """Best-effort required / forbidden topic extraction.

    We use capitalised noun phrases + parenthesised acronyms as strong
    signals for required topics.  Forbidden topics come from clauses
    following ``must not`` / ``avoid`` etc.
    """
    required: list[str] = []
    forbidden: list[str] = []

    seen_required: set[str] = set()

    # Parenthesised acronyms ("Periaqueductal Gray (PAG)") — the
    # abbreviation is almost always a required topic.
    for m in _PAREN_ACRONYM.finditer(brief):
        token = m.group(1).strip()
        key = token.lower()
        if key in seen_required:
            continue
        seen_required.add(key)
        required.append(token)

    # Clauses following "must cover" / "covering" / ...
    for match in _REQUIRED_TOPIC_SPLIT.finditer(brief):
        tail = brief[match.end() : match.end() + 240]
        for item in re.split(r",|;|\band\b", tail, flags=re.I):
            cleaned = item.strip().rstrip(".")
            if not cleaned:
                continue
            if len(cleaned) > 80:
                continue
            key = cleaned.lower()
            if key in seen_required:
                continue
            seen_required.add(key)
            required.append(cleaned)

    for match in _FORBIDDEN_TOPIC_SPLIT.finditer(brief):
        tail = brief[match.end() : match.end() + 120]
        for item in re.split(r",|;|\band\b", tail, flags=re.I):
            cleaned = item.strip().rstrip(".")
            if cleaned and len(cleaned) <= 80:
                forbidden.append(cleaned)

    confidence = 0.75 if required else 0.2
    return required, forbidden, confidence


_TONE_HINT_TOKENS: tuple[str, ...] = (
    "cinematic", "playful", "serious", "dark", "warm", "gritty",
    "hopeful", "ironic", "reflective", "urgent", "curious",
)


def _heuristic_tone(brief: str) -> tuple[list[str], float]:
    lower = brief.lower()
    hits = [tok for tok in _TONE_HINT_TOKENS if re.search(rf"\b{tok}\b", lower)]
    return hits, (0.7 if hits else 0.2)


def _heuristic_intent(brief: str) -> BriefIntent:
    duration_sec, conf_duration = _heuristic_duration_sec(brief)
    audience, conf_audience = _heuristic_audience(brief)
    tone, conf_tone = _heuristic_tone(brief)
    required, forbidden, conf_topics = _heuristic_topics(brief)
    # INTENT-EXTR-A: audience descriptors must never become required_topics.
    required = _filter_required_topics(required, audience)
    forbidden = _filter_required_topics(forbidden, audience)
    return BriefIntent(
        duration_sec=duration_sec,
        tolerance_sec=DEFAULT_TOLERANCE_SEC,
        audience=audience,
        tone=tone,
        corpus_paths=[],
        required_topics=required,
        forbidden_topics=forbidden,
        format_hints={},
        confidence={
            "duration_sec": conf_duration,
            "tolerance_sec": 0.3,
            "audience": conf_audience,
            "tone": conf_tone,
            "required_topics": conf_topics,
            "forbidden_topics": 0.4 if forbidden else 0.2,
            "format_hints": 0.2,
        },
    )


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


class IntentExtractionError(RuntimeError):
    """Raised when the extractor can produce no usable BriefIntent."""


def _parse_llm_intent(text: str) -> BriefIntent:
    stripped = (text or "").strip()
    if not stripped:
        raise IntentExtractionError("empty intent-extractor LLM response")
    if stripped.startswith("```"):
        nl = stripped.find("\n")
        if nl >= 0:
            stripped = stripped[nl + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise IntentExtractionError(
            f"intent-extractor LLM response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, Mapping):
        raise IntentExtractionError(
            f"intent-extractor LLM response must be an object, "
            f"got {type(data).__name__}"
        )
    try:
        intent = BriefIntent.model_validate(dict(data))
    except Exception as exc:  # pydantic ValidationError, TypeError, ...
        raise IntentExtractionError(
            f"intent-extractor LLM response failed schema validation: {exc}"
        ) from exc
    # INTENT-EXTR-A: apply the same audience-stopword filter to LLM output.
    # The LLM (e.g. gemini-2.5-flash) has been observed to classify
    # "ADHD-friendly" as both audience AND required_topic; we scrub
    # required_topics / forbidden_topics defensively.
    intent = intent.model_copy(
        update={
            "required_topics": _filter_required_topics(
                intent.required_topics, intent.audience
            ),
            "forbidden_topics": _filter_required_topics(
                intent.forbidden_topics, intent.audience
            ),
        }
    )
    return intent


def _llm_intent(brief: str) -> Optional[BriefIntent]:
    factory = _llm_client_factory or (lambda: _default_llm_call)
    try:
        call = factory()
    except Exception as exc:
        logger.warning("intent_extractor: LLM factory failed: %s", exc)
        return None
    try:
        text = call(_resolve_model(), _INTENT_SYSTEM_INSTRUCTION, brief)
    except Exception as exc:  # google-genai network / auth errors
        logger.warning("intent_extractor: LLM call failed: %s", exc)
        return None
    try:
        return _parse_llm_intent(text)
    except IntentExtractionError as exc:
        logger.warning("intent_extractor: LLM parse failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: Backup file path for the restated brief (R0).  Persisted to disk so
#: the ``/agui/restated_brief`` endpoint (INTENT-03) can serve R0 across
#: process boundaries and reload after B2 restore.
BRIEF_INTENT_BACKUP_FILENAME: str = "_brief_intent_backup.json"


def _brief_intent_backup_path() -> Path:
    base = os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")
    return Path(base) / "timelines" / BRIEF_INTENT_BACKUP_FILENAME


def _write_intent_backup(intent: "BriefIntent") -> None:
    """Persist the extracted intent alongside the scenes backup.

    Best-effort — failures are logged but don't block the extractor.
    """
    path = _brief_intent_backup_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(intent.to_json(), encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "intent_extractor: could not write backup to %s: %s",
            path, exc,
        )


def read_intent_backup() -> Optional["BriefIntent"]:
    """Read the persisted :class:`BriefIntent` from disk, or ``None``.

    The ``/agui/restated_brief`` endpoint calls this so R0 remains
    visible to the dashboard even after the in-memory session state
    has been torn down.
    """
    path = _brief_intent_backup_path()
    if not path.exists():
        return None
    try:
        return BriefIntent.from_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "intent_extractor: could not read backup %s: %s", path, exc,
        )
        return None


__all__ = [
    "BRIEF_INTENT_BACKUP_FILENAME",
    "BRIEF_INTENT_KEY",
    "BriefIntent",
    "DEFAULT_DURATION_SEC",
    "DEFAULT_TOLERANCE_SEC",
    "IntentExtractionError",
        "get_brief_intent",
    "read_intent_backup",
    ]
