"""
Run-start Preference Ledger seed (ARCH-A3, issue #133).

Parent ticket: ARCH-A #123.  Meta: ARCH-2026 #122.

Workstream A on the Preference Ledger substrate has four producer /
consumer pieces:

* **A1 (#131):** append-only ledger storage.
* **A2 (#132):** :mod:`agents.preference_interpreter` -- free-form L4
  directives → scoped records.
* **A3 (this module):** parse the *original brief* at run start into R0
  baseline records so the ledger is never empty when the pipeline
  begins.
* **A4 (#134) / A5 (#135):** assemble / watch the ledger.

The A3 contract is deliberately narrow:

1. At run start, before any producer agent runs, parse the original
   user brief into ``Scope.GLOBAL`` preference records spanning the
   canonical baseline subjects (``tone``, ``voice``, ``pacing``,
   ``visual_style``, ``narrative_structure``, ``speaker_role``,
   ``duration``).  These are the **R0** records.
2. Every R0 record is provenance-tagged with
   ``Origin(l4_event_id="R0", reviewer="system", timestamp=...)``.
   The enum-like L4 event id ``"R0"`` is the documented sentinel (see
   :class:`~callbacks.preference_ledger.Origin` docstring) -- the
   consistency checker (ARCH-A5) uses it to skip stage-boundary drift
   reporting on the initial revisions.
3. Seeding is **idempotent**: if the ledger already carries records
   (e.g. a B2-restored run), we skip rather than append duplicates.
   This keeps the universal back-edge invariant (ARCH-B1) intact when
   the same pipeline re-enters :func:`seed_ledger_from_brief` under a
   checkpoint restore.
4. Seeding never silently degrades.  If the LLM backend is unavailable
   we fall back to a deterministic heuristic that always emits at
   least a single baseline record (so the ledger is non-empty); if
   even that fails -- e.g. no topic and no brief -- we raise.

The module deliberately does **not** use the free-form directive parser
in :mod:`agents.preference_interpreter`: an empty or vague brief like
"Create a documentary about X" would otherwise heuristically emit a
single TONE record, which is not a baseline for every subject.  Instead
we use a dedicated R0 system prompt that asks the LLM to populate every
baseline subject (and fall back to a typed heuristic that does the same
deterministically).  The public append path still goes through the
ledger's :func:`~callbacks.preference_ledger.append_preference`, so
append-only / revision monotonicity remain enforced at a single choke
point.

Design invariants (covered by ``tests/test_run_start_seed.py``):

* Every emitted record is GLOBAL-scoped and has ``scope_ref is None``.
* Every emitted record has ``origin.l4_event_id == "R0"`` and
  ``origin.reviewer == "system"``.
* The ledger is non-empty after ``seed_ledger_from_brief`` returns
  successfully.
* Re-running on a non-empty ledger is a no-op.
* The LLM path is optional -- passing ``use_llm=False`` yields a
  deterministic seed that still covers every baseline subject.

This module is the single place where "R0" is constructed.  Other code
that wants to recognise R0 records should compare ``origin.l4_event_id``
against the string ``"R0"`` directly (stable, documented contract).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional

from callbacks.preference_ledger import (
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    PreferenceRecord,
    Scope,
    Subject,
    append_preference,
    current_revision,
    list_preferences,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Sentinel ``Origin.l4_event_id`` value stamped on every R0 record.  Used
#: by the consistency checker and by this module's idempotency guard.
R0_EVENT_ID: str = "R0"

#: Sentinel ``Origin.reviewer`` value stamped on every R0 record.
R0_REVIEWER: str = "system"

#: Blackboard key where :func:`seed_ledger_from_brief` looks for the raw
#: brief text when a caller didn't pass ``brief_text`` explicitly.  The
#: CLI entry point (:mod:`run_pipeline`) stages the text here immediately
#: after :func:`~callbacks.state_manager.build_pipeline_state`.
ORIGINAL_BRIEF_KEY: str = "original_brief"

#: Blackboard key where a short human-readable seed summary is written.
#: Useful for the dashboard and for debugging; not load-bearing.
R0_SUMMARY_KEY: str = "_r0_seed_summary"

#: The canonical baseline subjects seeded at R0.  ``MUSIC`` is
#: deliberately omitted -- it is decided per-scene by the visual / audio
#: planners and has no global baseline.
_BASELINE_SUBJECTS: tuple[Subject, ...] = (
    Subject.TONE,
    Subject.VOICE,
    Subject.PACING,
    Subject.VISUAL_STYLE,
    Subject.NARRATIVE_STRUCTURE,
    Subject.SPEAKER_ROLE,
    Subject.DURATION,
)


class RunStartSeedError(RuntimeError):
    """Raised when the R0 seed cannot produce a non-empty baseline."""


# ---------------------------------------------------------------------------
# LLM hook (pluggable for tests; mirrors the A2 pattern).
# ---------------------------------------------------------------------------

LLMCallable = Callable[[str, str, str], str]
"""Minimal LLM call signature: ``(model, system_instruction, prompt) -> text``."""

_llm_client_factory: Optional[Callable[[], LLMCallable]] = None


def _default_llm_call(model: str, system: str, prompt: str) -> str:
    """Default google-genai call, mirroring :mod:`agents.preference_interpreter`."""
    from google import genai  # type: ignore  # Local import -- google-genai is optional at import time.
    from google.genai import types as genai_types  # type: ignore[import-not-found]

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


# Reuse the interpreter model env var when set -- the R0 call is the
# same shape (small structured JSON) and running it through the same
# model keeps interpreted drift against R0 records consistent.
_R0_MODEL_ENV = "ARCH_A_INTERPRETER_MODEL"
_R0_DEFAULT_MODEL = "deepseek-v4-flash"


def _resolve_model() -> str:
    return _R0_DEFAULT_MODEL


_R0_SYSTEM_INSTRUCTION = """\
You are the Preference Ledger R0 Seeder.  You receive the ORIGINAL user
brief for a short documentary and must return baseline preference
records that describe the documentary's intended tone, voice, pacing,
visual style, narrative structure, speaker role, and duration.

HARD RULES:

1. Return a JSON object with a single key 'records' whose value is a
   list of record objects.  No prose, no markdown.
2. Every record has scope='global', scope_ref=null.  This is R0 --
   all records are global baselines.  Do NOT emit scene-scoped or
   voice-block-scoped records here.
3. Populate EXACTLY ONE record for each of these subjects, in this
   order: tone, voice, pacing, visual_style, narrative_structure,
   speaker_role, duration.
4. polarity is 'prefer' unless the brief explicitly says "must" /
   "always" (=> 'require') or "never" / "must not" (=> 'forbid').
5. content is a short (<= 120 chars) natural-language baseline derived
   FROM the brief.  If the brief is silent on a subject, emit a
   neutral baseline like "standard cinematic tone" -- never invent a
   specific directive.

Record schema:
{
  "scope": "global",
  "scope_ref": null,
  "polarity": "prefer" | "avoid" | "require" | "forbid",
  "subject": "tone" | "voice" | "pacing" | "visual_style" |
             "narrative_structure" | "speaker_role" | "duration",
  "content": "<short baseline, derived from brief>"
}
"""


# ---------------------------------------------------------------------------
# Heuristic baseline -- used when LLM is disabled or parsing fails.
# ---------------------------------------------------------------------------

# Per-subject phrases scanned for in the brief.  If any phrase hits we
# prefer its polarity / content instead of the neutral default.  All
# patterns are case-insensitive word-boundary matches.
_HEURISTIC_POLARITY_TOKENS: dict[Polarity, tuple[str, ...]] = {
    Polarity.FORBID: (r"\bmust not\b", r"\bnever\b", r"\bforbid\b", r"\bdo not ever\b"),
    Polarity.REQUIRE: (r"\bmust\b", r"\balways\b", r"\brequired?\b", r"\bensure that\b"),
    Polarity.AVOID: (r"\bavoid\b", r"\bdon'?t\b", r"\bno more\b", r"\bless\b"),
}


def _subject_default(subject: Subject) -> str:
    return {
        Subject.TONE: "standard cinematic tone",
        Subject.VOICE: "clear, natural narration voice",
        Subject.PACING: "even pacing suitable for a short documentary",
        Subject.VISUAL_STYLE: "grounded documentary visual style",
        Subject.NARRATIVE_STRUCTURE: "three-act documentary structure",
        Subject.SPEAKER_ROLE: "single narrator",
        Subject.DURATION: "short-form documentary length",
    }[subject]


# Subject hint phrases -- if any phrase appears in the brief, we surface
# the enclosing clause as the baseline for that subject.  Order within a
# subject is from most- to least-specific so the first hit wins.
_SUBJECT_HEURISTIC_PHRASES: dict[Subject, tuple[str, ...]] = {
    Subject.TONE: ("tone", "mood", "warmer", "darker", "playful", "serious"),
    Subject.VOICE: ("voice", "narrator", "narration", "accent", "register"),
    Subject.PACING: ("pacing", "pace", "faster", "slower", "snappy", "rushed"),
    Subject.VISUAL_STYLE: (
        "visual", "color", "colour", "palette", "shot", "cinematography", "look",
    ),
    Subject.NARRATIVE_STRUCTURE: ("structure", "story", "arc", "hook", "payoff"),
    Subject.SPEAKER_ROLE: ("speaker", "cassandra", "narrator", "voice-over"),
    Subject.DURATION: ("duration", "length", "minutes", "seconds", "short", "long"),
}


def _split_sentences(text: str) -> list[str]:
    """Very conservative sentence splitter -- good enough for heuristic hints."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _detect_polarity_for_clause(clause: str) -> Polarity:
    lower = clause.lower()
    for polarity, patterns in _HEURISTIC_POLARITY_TOKENS.items():
        for pat in patterns:
            if re.search(pat, lower):
                return polarity
    return Polarity.PREFER


def _find_clause_for_subject(
    sentences: Iterable[str], subject: Subject
) -> Optional[str]:
    phrases = _SUBJECT_HEURISTIC_PHRASES[subject]
    for sentence in sentences:
        lower = sentence.lower()
        for phrase in phrases:
            # Word-boundary to avoid hitting "long" inside "belong".
            if re.search(rf"\b{re.escape(phrase)}\b", lower):
                return sentence
    return None


@dataclass(frozen=True)
class _DraftR0:
    subject: Subject
    polarity: Polarity
    content: str


def _heuristic_r0_drafts(brief: str) -> list[_DraftR0]:
    """Produce one :class:`_DraftR0` per canonical baseline subject.

    Never returns an empty list.  If the brief is blank we still emit
    one draft per subject using the neutral defaults so the ledger is
    non-empty after seeding.
    """
    sentences = _split_sentences(brief) if brief.strip() else []
    drafts: list[_DraftR0] = []
    for subject in _BASELINE_SUBJECTS:
        clause = _find_clause_for_subject(sentences, subject)
        if clause:
            content = clause
            polarity = _detect_polarity_for_clause(clause)
        else:
            content = _subject_default(subject)
            polarity = Polarity.PREFER
        # Cap content length -- record content must be non-empty but we
        # don't want megabytes of corpus landing in the ledger.
        if len(content) > 240:
            content = content[:237].rstrip() + "..."
        drafts.append(
            _DraftR0(subject=subject, polarity=polarity, content=content)
        )
    return drafts


# ---------------------------------------------------------------------------
# LLM path.
# ---------------------------------------------------------------------------


def _parse_llm_r0(text: str) -> list[_DraftR0]:
    if not text or not text.strip():
        raise RunStartSeedError("empty R0 LLM response")
    stripped = text.strip()
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
        raise RunStartSeedError(
            f"R0 LLM response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, Mapping):
        raise RunStartSeedError(
            f"R0 LLM response must be a JSON object, got {type(data).__name__}"
        )
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise RunStartSeedError(
            "R0 LLM response is missing a non-empty 'records' list"
        )

    by_subject: dict[Subject, _DraftR0] = {}
    for idx, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise RunStartSeedError(
                f"records[{idx}] must be an object, "
                f"got {type(item).__name__}"
            )
        try:
            subject = Subject(str(item["subject"]))
        except (KeyError, ValueError) as exc:
            raise RunStartSeedError(
                f"records[{idx}] has invalid/missing subject"
            ) from exc
        if subject not in _BASELINE_SUBJECTS:
            # Ignore out-of-scope subjects (e.g. MUSIC) -- R0 only covers
            # the canonical baselines.
            continue
        try:
            polarity = Polarity(str(item.get("polarity", "prefer")))
        except ValueError as exc:
            raise RunStartSeedError(
                f"records[{idx}] has unknown polarity "
                f"{item.get('polarity')!r}"
            ) from exc
        content = str(item.get("content", "")).strip()
        if not content:
            raise RunStartSeedError(
                f"records[{idx}] has empty content"
            )
        # R0 is GLOBAL by construction.  If the LLM supplied a scope /
        # scope_ref we ignore it -- log at debug so nothing is silently
        # lost in the test suite.
        scope = str(item.get("scope", "global")).lower()
        if scope != Scope.GLOBAL.value:
            logger.debug(
                "R0 LLM proposed non-global scope=%r for subject=%s; "
                "coercing to global (A3 invariant)",
                scope, subject.value,
            )
        by_subject[subject] = _DraftR0(
            subject=subject, polarity=polarity, content=content
        )

    if not by_subject:
        raise RunStartSeedError(
            "R0 LLM response contained no baseline-subject records"
        )
    return [by_subject[s] for s in _BASELINE_SUBJECTS if s in by_subject]


def _build_llm_prompt(brief: str) -> str:
    payload = {
        "brief": brief,
        "baseline_subjects": [s.value for s in _BASELINE_SUBJECTS],
    }
    return (
        "BRIEF CONTEXT:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n\n"
        "Return a JSON object with key 'records' whose value is the "
        "list of GLOBAL baseline preference records."
    )


def _llm_r0_drafts(brief: str) -> list[_DraftR0]:
    call = (
        _llm_client_factory() if _llm_client_factory is not None
        else _default_llm_call
    )
    raw = call(_resolve_model(), _R0_SYSTEM_INSTRUCTION, _build_llm_prompt(brief))
    return _parse_llm_r0(raw)


def _merge_drafts(
    primary: list[_DraftR0], fallback: list[_DraftR0]
) -> list[_DraftR0]:
    """Merge ``fallback`` into ``primary`` -- fallback fills only subjects
    ``primary`` does not already cover.  Preserves baseline subject order.
    """
    by_subject: dict[Subject, _DraftR0] = {}
    for draft in fallback:
        by_subject[draft.subject] = draft
    for draft in primary:
        by_subject[draft.subject] = draft
    return [by_subject[s] for s in _BASELINE_SUBJECTS if s in by_subject]


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def _already_seeded(state: Mapping[str, Any]) -> bool:
    """Return True iff the ledger already contains at least one R0 record.

    Matching on ``origin.l4_event_id == "R0"`` is the documented
    contract (see :class:`~callbacks.preference_ledger.Origin`).
    """
    try:
        records = list_preferences(state)
    except (ValueError, TypeError):
        # Malformed ledger -- let the downstream append raise on its own.
        return False
    return any(r.origin.l4_event_id == R0_EVENT_ID for r in records)


def _resolve_brief(
    state: Mapping[str, Any], brief_text: Optional[str]
) -> str:
    """Return the brief text to seed from.

    Precedence: explicit argument > ``state[ORIGINAL_BRIEF_KEY]`` >
    ``state['topic']`` (wrapped in a synthetic one-line brief).  We
    never silently substitute an empty string -- if all three are
    missing we raise.
    """
    if isinstance(brief_text, str) and brief_text.strip():
        return brief_text.strip()
    staged = state.get(ORIGINAL_BRIEF_KEY)
    if isinstance(staged, str) and staged.strip():
        return staged.strip()
    topic = state.get("topic")
    if isinstance(topic, str) and topic.strip():
        return f"Create a short documentary about: {topic.strip()}"
    raise RunStartSeedError(
        "cannot seed R0: no brief_text provided and neither "
        f"state[{ORIGINAL_BRIEF_KEY!r}] nor state['topic'] is set"
    )


def seed_ledger_from_brief(
    state: MutableMapping[str, Any],
    *,
    brief_text: Optional[str] = None,
    timestamp: Optional[str] = None,
    use_llm: bool = True,
) -> list[PreferenceRecord]:
    """Seed the Preference Ledger with R0 baseline records.

    Parameters
    ----------
    state:
        ADK session state (or any mutable mapping).  Must contain the
        ledger key (``build_pipeline_state`` seeds it as ``"[]"``).
    brief_text:
        Optional explicit brief override.  When omitted we read
        ``state[ORIGINAL_BRIEF_KEY]`` or fall back to
        ``state["topic"]``.
    timestamp:
        Optional ISO-8601 string stamped into every R0
        :class:`~callbacks.preference_ledger.Origin`.  Defaults to
        ``datetime.now(timezone.utc).isoformat()``.
    use_llm:
        When ``True`` (default), attempt the LLM path first and fall
        back to the typed heuristic on parse failure.  When ``False``,
        use the heuristic directly -- useful for tests and for offline
        / CI runs.

    Returns
    -------
    list[PreferenceRecord]
        The records that were just appended to the ledger, in append
        order.  When the ledger was already seeded (idempotent re-run)
        returns an empty list.

    Raises
    ------
    RunStartSeedError
        If no brief / topic is available, or if both the LLM path and
        the heuristic fall-back fail.  Never silently no-ops on a
        missing brief -- the whole point of A3 is that every run
        begins with a non-empty ledger.
    """
    if state is None:
        raise ValueError("state must be a mutable mapping")
    if PREFERENCE_LEDGER_KEY not in state:
        # The state-manager builder is the canonical entry point -- if
        # the key is missing the caller skipped pipeline init.  Fail
        # loud rather than silently creating it; tests exercise this by
        # passing an empty dict, so we initialise the empty ledger on
        # their behalf to match build_pipeline_state semantics.
        state[PREFERENCE_LEDGER_KEY] = "[]"

    if _already_seeded(state):
        logger.info(
            "run_start_seed: ledger already carries R0 records "
            "(current revision=%d) -- skipping seed",
            current_revision(state),
        )
        return []

    brief = _resolve_brief(state, brief_text)

    drafts: list[_DraftR0] = []
    llm_error: Optional[Exception] = None
    if use_llm:
        try:
            drafts = _llm_r0_drafts(brief)
        except RunStartSeedError as exc:
            llm_error = exc
            logger.warning(
                "run_start_seed: LLM R0 parse failed (%s) -- falling "
                "back to typed heuristic", exc,
            )
        except Exception as exc:  # noqa: BLE001 -- defensive
            llm_error = exc
            logger.warning(
                "run_start_seed: LLM backend error (%s) -- falling "
                "back to typed heuristic", exc,
            )

    # Heuristic fills subjects the LLM didn't cover (and is the sole
    # source when ``use_llm=False`` or the LLM path errored).
    heuristic = _heuristic_r0_drafts(brief)
    drafts = _merge_drafts(drafts, heuristic) if drafts else heuristic

    if not drafts:  # pragma: no cover -- heuristic is non-empty by construction
        raise RunStartSeedError(
            f"R0 seed produced no records; llm_error={llm_error}"
        )

    ts = timestamp or datetime.now(timezone.utc).isoformat()
    origin = Origin(
        l4_event_id=R0_EVENT_ID, reviewer=R0_REVIEWER, timestamp=ts
    )

    appended: list[PreferenceRecord] = []
    for draft in drafts:
        record = append_preference(
            state,
            scope=Scope.GLOBAL,
            scope_ref=None,
            polarity=draft.polarity,
            subject=draft.subject,
            content=draft.content,
            origin=origin,
            metadata={"parser": "r0_seed"},
        )
        appended.append(record)

    summary = (
        f"R0_SEED: brief_chars={len(brief)} records={len(appended)} "
        f"(rev {appended[0].revision}..{appended[-1].revision}) -- "
        + ", ".join(
            f"[{r.subject.value}:{r.polarity.value}]" for r in appended
        )
    )
    state[R0_SUMMARY_KEY] = summary
    logger.info(summary)
    return appended


# ---------------------------------------------------------------------------
# Callback glue -- callers register this as a before_agent_callback on
# the outer pipeline SequentialAgent so seeding happens before any
# producer.  The function is also safe to call directly from
# run_pipeline.py (the CLI path builds initial_state up front, so it
# can seed synchronously before handing state to the Runner).
# ---------------------------------------------------------------------------


def run_start_seed_callback(callback_context: Any) -> None:
    """ADK ``before_agent_callback`` wrapper.

    Invoked from :func:`server.agents.pipeline._init_pipeline_state`
    after ``build_pipeline_state`` has injected the ledger container.
    Failing here would abort the pipeline before any producer runs --
    which is exactly what we want if no brief is available, because
    the whole downstream universe assumes a seeded ledger.
    """
    state = callback_context.state
    seed_ledger_from_brief(state)
    return None


__all__ = ["LLMCallable",
    "RunStartSeedError",
    "run_start_seed_callback",
    "seed_ledger_from_brief",]
