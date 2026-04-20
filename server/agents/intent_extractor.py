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


def set_llm_client_factory(
    factory: Optional[Callable[[], LLMCallable]],
) -> None:
    """Install a zero-arg factory that returns an :data:`LLMCallable`.

    Tests inject deterministic stubs here; production leaves the factory
    unset and falls back to google-genai.
    """
    global _llm_client_factory
    _llm_client_factory = factory


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
4. Each element of required_topics and forbidden_topics MUST be a
   SINGLE atomic concept — one noun phrase, never a full sentence
   and never two topics glued together.  Break at sentence
   boundaries ('.', '!', '?').  Strip leading negation verbs
   ("do not discuss", "don't mention", "avoid", "never show") —
   keep only the subject-matter noun phrase.
   Examples:
     brief: "Must cover opioid chemistry and fight-flight-freeze
             circuitry. Do not discuss recreational drug use."
     required_topics: ["opioid chemistry", "fight-flight-freeze
                       circuitry"]
     forbidden_topics: ["recreational drug use"]
   NEVER emit: ["fight-flight-freeze circuitry. Do not discuss
                 recreational drug use"]  (two topics in one string)
   NEVER emit: ["do not discuss recreational drug use"]  (keeps the
                 negation verb)
5. Confidence < 0.5 means the field was defaulted rather than inferred.
6. No prose, no markdown fences, no trailing commentary."""


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


# Sentence break: ``.!?`` preceded by EITHER
#   (a) 4+ word characters (normal words like "circuitry.", "WWII.")
#   (b) 3+ uppercase letters (all-caps acronyms like "PAG.", "RNA.",
#       "DNA.", "GDP.", "API.")
# AND followed by whitespace + an uppercase letter (or end-of-string
# trailing punctuation).
#
# This combination catches 3-letter ALL-CAPS acronyms common in
# documentary briefs while protecting mixed-case 3-4 char abbreviations
# that are NOT sentence boundaries:
#   - "Dr. Smith"   (2 chars, not uppercase triple) — not split ✓
#   - "St. Louis"   (2 chars) — not split ✓
#   - "Gen. Patton" (3 mixed case, only 1 uppercase) — not split ✓
#   - "Sen. Brown", "Rep. Jones", "Gov. Reed" — not split ✓
#   - "Fig. 1 shows", "Sgt. Doe" — not split ✓
#   - "U.S. policy"  (single-char tokens) — not split ✓
# And it DOES split on genuine sentence ends:
#   - "circuitry. Do not" ✓ (4 word chars)
#   - "PAG. Do not"      ✓ (3 uppercase)
#   - "WWII. The next"   ✓ (4 word chars)
_SENTENCE_BREAK = re.compile(
    r"(?:(?<=\w{4})|(?<=[A-Z]{3}))[.!?]\s+(?=[A-Z])|[.!?]$"
)


def _split_sentence_concat(topic: str) -> list[str]:
    """Break a single topic string on sentence boundaries.

    Observed in PAG run #4: gemini-2.5-flash emitted
    ``required_topics = ['fight-flight-freeze circuitry. Do not discuss
    recreational drug use']`` — both clauses in one item.  No scenario
    can contain that literal string, so the verifier halted every
    draft.  The heuristic path had the same bug when the "must cover"
    clause ran into a following sentence.

    A sentence break is ``.!?`` followed by whitespace + an uppercase
    letter (new-sentence marker), or trailing punctuation at end of
    string.  This preserves abbreviations like "U.S. economic policy"
    and "Dr. Smith research" as single topics.
    """
    if not isinstance(topic, str):
        return []
    stripped = topic.strip()
    if not stripped:
        return []
    if not _SENTENCE_BREAK.search(stripped):
        return [stripped]
    parts = [p.strip().rstrip(".!?") for p in _SENTENCE_BREAK.split(stripped) if p and p.strip()]
    return [p for p in parts if p and len(p) >= 3]


# Forbidden-clause prefix.  "no" requires an explicit verb after it
# (e.g. "no discuss", "no show") so legitimate proper nouns like "No
# Child Left Behind Act" or "No Fly List" aren't silently dropped /
# corrupted on either path.
_FORBIDDEN_PREFIX = re.compile(
    r"^((?:do not|don'?t|avoid|exclude)(?:\s+(?:discuss|show|mention|include|cover))?|"
    r"no\s+(?:discuss|show|mention|include|cover))\s+",
    re.I,
)


def _filter_required_topics(
    required: list[str],
    audience: str,
    *,
    is_forbidden: bool = False,
) -> list[str]:
    """Drop audience-descriptor tokens from a topic list.

    A required topic must be subject matter the documentary covers
    (e.g. "PAG", "opioid analgesia"), not an audience attribute
    (e.g. "ADHD", "expert").  We filter against a fixed stopword set
    plus the detected ``audience`` label so every extraction path
    produces the same invariant.

    We also split each input item on sentence boundaries so multi-
    sentence topics emitted by the LLM (e.g. "X circuitry. Do not
    discuss Y") get decomposed before the stopword filter — see
    :func:`_split_sentence_concat`.

    ``is_forbidden`` toggles two path-specific behaviours:

    * ``False`` (required-topics path): reject items beginning with
      "do not" / "don't" / "avoid" / "exclude" / "no " — those clauses
      belong in :attr:`BriefIntent.forbidden_topics`, not here.  The
      PAG run-#4 failure mode that motivated this filter was the LLM
      merging "X. Do not discuss Y" into a single required-topic item;
      after sentence-splitting we drop the residual "Do not discuss Y".
    * ``True`` (forbidden-topics path): **preserve** such items,
      stripping only the negation prefix so "do not discuss violence"
      becomes "violence".  Forbidden topics are a fail-closed safety
      constraint; silently dropping them would weaken the verifier.
    """
    audience_norm = (audience or "").strip().lower()
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in required:
        for topic in _split_sentence_concat(raw):
            stripped = topic.strip()
            if not stripped:
                continue
            key = stripped.lower()
            if key in _AUDIENCE_STOPWORDS:
                continue
            if audience_norm and key == audience_norm:
                continue
            # Trim off "-friendly" variants like "adhd-friendly" that the
            # LLM sometimes emits as a topic.
            if audience_norm and key.startswith(audience_norm + "-"):
                continue
            prefix_match = _FORBIDDEN_PREFIX.match(stripped)
            if prefix_match:
                if is_forbidden:
                    # Strip the negation prefix so the forbidden topic
                    # is the subject matter itself, not the command.
                    stripped = stripped[prefix_match.end():].strip()
                    if not stripped:
                        continue
                    key = stripped.lower()
                    # Re-check audience stopwords on the stripped
                    # subject so "do not discuss ADHD" and bare "ADHD"
                    # get the same treatment on the forbidden path.
                    if key in _AUDIENCE_STOPWORDS:
                        continue
                    if audience_norm and key == audience_norm:
                        continue
                    if audience_norm and key.startswith(audience_norm + "-"):
                        continue
                else:
                    # Required-topics path: drop the forbidden-clause
                    # fragment entirely.
                    continue
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(stripped)
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
    # INTENT-EXTR-B: a new sentence is a new clause, not a continuation
    # of the "must cover" list.  Observed in PAG run #4: the brief
    # "It must cover opioid chemistry and fight-flight-freeze circuitry.
    # Do not discuss recreational drug use." used to merge into a single
    # required topic "fight-flight-freeze circuitry. Do not discuss
    # recreational drug use" because the splitter walked 240 chars past
    # "must cover" without honouring sentence terminators.  We now
    # truncate ``tail`` at the first sentence-ending punctuation.
    for match in _REQUIRED_TOPIC_SPLIT.finditer(brief):
        tail = brief[match.end() : match.end() + 240]
        sentence_end = _SENTENCE_BREAK.search(tail)
        if sentence_end:
            tail = tail[: sentence_end.start()]
        for item in re.split(r",|;|\band\b", tail, flags=re.I):
            cleaned = item.strip().rstrip(".")
            if not cleaned:
                continue
            if len(cleaned) > 80:
                continue
            # Defensive: if an item still contains sentence-break
            # punctuation after splitting, keep only the first clause.
            if _SENTENCE_BREAK.search(cleaned):
                cleaned = _SENTENCE_BREAK.split(cleaned, maxsplit=1)[0].strip()
                if not cleaned:
                    continue
            key = cleaned.lower()
            if key in seen_required:
                continue
            seen_required.add(key)
            required.append(cleaned)

    # Same sentence-boundary treatment for forbidden clauses: "Do not X.
    # It must cover Y" must not leak "X. It must cover Y" as forbidden.
    for match in _FORBIDDEN_TOPIC_SPLIT.finditer(brief):
        tail = brief[match.end() : match.end() + 120]
        sentence_end = _SENTENCE_BREAK.search(tail)
        if sentence_end:
            tail = tail[: sentence_end.start()]
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
    # INTENT-EXTR-B: forbidden path preserves negation clauses (strips the
    # prefix so the remaining subject-matter is retained as a real
    # fail-closed constraint).
    required = _filter_required_topics(required, audience)
    forbidden = _filter_required_topics(forbidden, audience, is_forbidden=True)
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


# Negation prefixes that an LLM might leak into a forbidden topic string.
# We strip them so the forbidden-topic value is just the subject-matter
# noun phrase (e.g. "recreational drug use"), matching what strict
# verbatim matching expects the scenario text to either contain or avoid.
_NEGATION_PREFIX = re.compile(
    r"^\s*(?:please\s+)?"
    r"(?:do\s+not|don'?t|never|avoid(?:\s+any)?|exclude|skip|do\s+not\s+discuss|"
    r"do\s+not\s+mention|don'?t\s+discuss|don'?t\s+mention)\s+"
    r"(?:discuss(?:ing)?\s+|mention(?:ing)?\s+|show(?:ing)?\s+|"
    r"reference\s+|refer(?:ring)?\s+to\s+|talk(?:ing)?\s+about\s+)?",
    re.I,
)


def _split_llm_topic(raw: str, *, is_forbidden: bool = False) -> list[str]:
    """Defensively split and clean a single LLM-emitted topic string.

    Even with an explicit prompt instruction, an LLM may still emit a
    glued sentence pair like
    ``"fight-flight-freeze circuitry. Do not discuss recreational drug use"``
    as a single required topic.  This helper:

    1. Splits on sentence boundaries (``_SENTENCE_BREAK``) so each
       sentence becomes its own candidate topic.
    2. For forbidden topics, strips leading negation verbs
       ("do not discuss", "avoid", ...) so the remaining phrase is just
       the subject-matter noun phrase that strict matching will compare
       against.
    3. For required topics, discards any sentence whose body starts
       with a negation verb (it does not belong under ``required`` at
       all — it is a forbidden clause that leaked in).
    4. Strips trailing punctuation and discards empties / overlong
       results (>80 chars).
    """
    cleaned_out: list[str] = []
    if not raw:
        return cleaned_out
    parts: list[str] = [p for p in _SENTENCE_BREAK.split(raw) if p and p.strip()]
    if not parts:
        parts = [raw]
    for part in parts:
        candidate = part.strip().strip(".!?;,:").strip()
        if not candidate:
            continue
        stripped = _NEGATION_PREFIX.sub("", candidate).strip().strip(".!?;,:").strip()
        if is_forbidden:
            # Forbidden path: keep the stripped phrase.
            final = stripped or candidate
        else:
            # Required path: if the ORIGINAL candidate started with a
            # negation verb, this sentence is a forbidden clause leak
            # — drop it entirely rather than strip the verb.
            if stripped != candidate:
                continue
            final = candidate
        if len(final) > 80 or not final:
            continue
        cleaned_out.append(final)
    return cleaned_out


def _flatten_llm_topics(
    topics: list[str], *, is_forbidden: bool = False
) -> list[str]:
    """Apply :func:`_split_llm_topic` to every element + dedupe."""
    flat: list[str] = []
    seen: set[str] = set()
    for raw in topics or []:
        if not isinstance(raw, str):
            continue
        for t in _split_llm_topic(raw, is_forbidden=is_forbidden):
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            flat.append(t)
    return flat


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
    #
    # INTENT-EXTR-C (observed in PAG run v20): the LLM emitted a single
    # required_topic string ``"fight-flight-freeze circuitry. Do not
    # discuss recreational drug use"`` — two topics glued across a
    # sentence boundary.  ``_flatten_llm_topics`` splits each topic on
    # sentence-break punctuation, strips leading negation verbs
    # ("do not discuss", "avoid", ...) from forbidden topics, and drops
    # any required-topic sentence that looks like a forbidden clause.
    required_clean = _flatten_llm_topics(
        list(intent.required_topics), is_forbidden=False
    )
    # If the LLM glued a forbidden clause onto a required topic, the
    # clause is now dropped from required_topics; we also surface the
    # stripped subject-matter into forbidden_topics so fail-closed
    # enforcement still applies.
    required_leak_forbidden: list[str] = []
    for raw in intent.required_topics:
        if not isinstance(raw, str):
            continue
        for part in _SENTENCE_BREAK.split(raw):
            candidate = part.strip().strip(".!?;,:").strip()
            if not candidate:
                continue
            stripped = _NEGATION_PREFIX.sub(
                "", candidate
            ).strip().strip(".!?;,:").strip()
            if stripped and stripped != candidate:
                required_leak_forbidden.append(stripped)
    forbidden_clean = _flatten_llm_topics(
        list(intent.forbidden_topics) + required_leak_forbidden,
        is_forbidden=True,
    )
    intent = intent.model_copy(
        update={
            "required_topics": _filter_required_topics(
                required_clean, intent.audience
            ),
            "forbidden_topics": _filter_required_topics(
                forbidden_clean, intent.audience, is_forbidden=True
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


def extract_intent(brief: str, *, use_llm: bool = True) -> BriefIntent:
    """Parse ``brief`` into a :class:`BriefIntent`, LLM-first with fallback.

    Parameters
    ----------
    brief:
        Raw free-text user brief.  Empty strings are permitted but yield
        a fully-defaulted intent with low confidence.
    use_llm:
        When True (default), attempt a google-genai call first and only
        fall back to the heuristic on failure.  Tests and offline runs
        pass False to force the deterministic path.
    """
    if use_llm:
        intent = _llm_intent(brief)
        if intent is not None:
            return intent
    return _heuristic_intent(brief)


def run_intent_extractor(
    state: MutableMapping[str, Any],
    *,
    brief_text: Optional[str] = None,
    use_llm: bool = True,
) -> BriefIntent:
    """Extract intent and write it into ADK session state.

    The canonical entry point from :mod:`agents.pipeline`.  Behaviour:

    1. If ``state[BRIEF_INTENT_KEY]`` already parses to a valid
       :class:`BriefIntent` we return it unchanged (idempotent for
       B2-restore re-entry).
    2. Otherwise we extract from ``brief_text`` or
       ``state['original_brief']`` or ``state['topic']`` and write the
       JSON form back under :data:`BRIEF_INTENT_KEY`.

    Raises
    ------
    IntentExtractionError
        When no brief text is available in any of the accepted sources.
    """
    existing = state.get(BRIEF_INTENT_KEY)
    if existing:
        try:
            if isinstance(existing, BriefIntent):
                return existing
            if isinstance(existing, Mapping):
                return BriefIntent.model_validate(dict(existing))
            return BriefIntent.from_json(str(existing))
        except Exception as exc:
            logger.warning(
                "intent_extractor: existing state[%s] is invalid, "
                "re-extracting: %s",
                BRIEF_INTENT_KEY, exc,
            )

    source = brief_text
    if not source:
        from callbacks.run_start_seed import ORIGINAL_BRIEF_KEY

        raw = state.get(ORIGINAL_BRIEF_KEY) or state.get("topic") or ""
        source = str(raw).strip()
    if not source:
        raise IntentExtractionError(
            "intent_extractor: no brief_text, state['original_brief'], "
            "or state['topic'] available"
        )

    intent = extract_intent(source, use_llm=use_llm)
    state[BRIEF_INTENT_KEY] = intent.to_json()
    # Mirror the extracted duration as ``target_duration_sec`` so the
    # scenario evaluator's structural checks (which cap the verdict at
    # POOR when sum(scene.duration_sec) < 95% of target) can enforce
    # the user's target.  Without this, target remains 0 and the
    # evaluator approves short drafts that the R0 constraint gate
    # then has to reject.
    state["target_duration_sec"] = float(intent.duration_sec)
    _write_intent_backup(intent)
    logger.info(
        "intent_extractor: R0 extracted — duration_sec=%.1f ± %.1f, "
        "audience=%s, required_topics=%d, forbidden_topics=%d",
        intent.duration_sec,
        intent.tolerance_sec,
        intent.audience,
        len(intent.required_topics),
        len(intent.forbidden_topics),
    )
    return intent


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


def get_brief_intent(state: Mapping[str, Any]) -> Optional[BriefIntent]:
    """Read the cached :class:`BriefIntent` from session state, or ``None``.

    Downstream consumers (constraint gate, per-stage verifier, chat
    narrator, ``/agui/restated_brief``) use this helper rather than
    touching the blackboard key directly.
    """
    raw = state.get(BRIEF_INTENT_KEY)
    if raw is None:
        return None
    try:
        if isinstance(raw, BriefIntent):
            return raw
        if isinstance(raw, Mapping):
            return BriefIntent.model_validate(dict(raw))
        return BriefIntent.from_json(str(raw))
    except Exception as exc:
        logger.warning("intent_extractor: could not decode %s: %s",
                       BRIEF_INTENT_KEY, exc)
        return None


__all__ = [
    "BRIEF_INTENT_BACKUP_FILENAME",
    "BRIEF_INTENT_KEY",
    "BriefIntent",
    "DEFAULT_DURATION_SEC",
    "DEFAULT_TOLERANCE_SEC",
    "IntentExtractionError",
    "extract_intent",
    "get_brief_intent",
    "read_intent_backup",
    "run_intent_extractor",
    "set_llm_client_factory",
]
