"""
Preference Interpreter -- parses a raw L4 directive into scoped Preference
records (ARCH-A2, issue #132; parent ARCH-A #123; meta ARCH-2026 #122).

Diagram 2a in ``docs/ARCHITECTURE_DIAGRAMS.md`` is the reference.  The
pipeline is a manifestation of a scoped Preference Ledger, not of a flat
prompt.  L4 is the only trigger that writes to the ledger; every L4
directive flows through this interpreter which parses it into one or more
:class:`~server.callbacks.preference_ledger.PreferenceRecord` entries.

Design invariants (enforced by tests in
``server/tests/test_preference_interpreter.py``):

1. **One directive -> one or more records.**  "rewrite scene 3 and I prefer
   shorter narration" produces two records (scene-3 scoped + global scoped).
2. **Implicit scope inference.**  "Cassandra sounds flat" is speaker-scoped
   (voice_block, speaker_role subject), not global.
3. **Explicit scope override.**  When the dashboard passes a ``scope_hint``
   (slot selection from ARCH-H4), the interpreter respects it UNLESS the
   directive explicitly generalises ("globally", "in general",
   "all scenes", "every scene", "overall").
4. **Closed vocabularies enforced.**  Every emitted record's scope /
   polarity / subject must be a member of the enums declared in
   :mod:`server.callbacks.preference_ledger`.  Closed-vocab misses raise
   :class:`InterpreterError` -- no silent coercion, no "other" bucket.
5. **Fail loud.**  Empty directive, LLM parse failure (after the heuristic
   fallback also fails to produce any record), invalid scope for the
   inferred subject -- all raise immediately.
6. **Append-only integration.**  Records are appended through
   :func:`append_preference` so revisions / append-only invariants remain
   enforced in one place.

The module exposes two surfaces:

* :func:`interpret_directive` -- the pure-Python callable the dashboard
  proactive-L4 path (ARCH-H5) invokes.  Synchronous, dependency-injectable
  for tests (see :func:`set_llm_client_factory`).
* :data:`preference_interpreter_agent` -- a thin ADK :class:`Agent` wrapper
  with ``output_key`` set to the human-readable summary and
  ``after_agent_callback`` asserting the records actually landed in the
  ledger (Timeline Guardian pattern, mirrors ``diagnostic_classifier``).
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
# Blackboard keys (dashboard ARCH-H5 and the ADK wrapper both use these)
# ---------------------------------------------------------------------------

#: Key the dashboard stages the pending directive under before invoking the
#: agent.  The value is a JSON object with ``text`` (required) and optional
#: ``reviewer``, ``l4_event_id``, ``scope_hint``, ``timestamp``.
PREFERENCE_INTERPRETER_INPUT_KEY = "preference_directive"

#: Secondary blackboard key -- holds a short human-readable summary of the
#: last interpretation.  Captured via ``output_key`` so callers see a
#: consistent "what just happened" text without re-parsing the ledger.
PREFERENCE_INTERPRETER_SUMMARY_KEY = "preference_interpreter_summary"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InterpreterError(RuntimeError):
    """Raised when a directive cannot be interpreted into any valid record.

    Fail-loud contract: an empty directive, a malformed LLM response with no
    heuristic salvage, or a closed-vocabulary miss all surface here rather
    than silently emitting a fabricated record.  Callers are expected to
    bounce the directive back to the human (the dashboard's L4 path).
    """


# ---------------------------------------------------------------------------
# LLM backend -- injectable so tests don't hit the network
# ---------------------------------------------------------------------------

LLMCallable = Callable[[str, str, str], str]
"""``(model, system_instruction, user_prompt) -> raw JSON text``."""

_llm_client_factory: Optional[Callable[[], LLMCallable]] = None


_INTERPRETER_MODEL = os.environ.get(
    "PREFERENCE_INTERPRETER_MODEL",
    os.environ.get("ADK_SYNTHESIS_MODEL", "gemini-2.0-flash"),
).split(":")[0]

# Strip ``litellm/`` routing prefix if present -- the default backend uses
# google-genai directly and only wants the bare model name.
if _INTERPRETER_MODEL.startswith("litellm/"):
    _INTERPRETER_MODEL = _INTERPRETER_MODEL[len("litellm/") :]


_INTERPRETER_SYSTEM_INSTRUCTION = (
    "You are the Preference Interpreter. You convert a single free-form "
    "L4 reviewer directive into ONE OR MORE scoped preference records that "
    "will be appended to an append-only Preference Ledger.\n"
    "\n"
    "Rules:\n"
    "- Emit a JSON object with a single key 'records' whose value is a "
    "non-empty list. Each list item is a record.\n"
    "- Each record has fields: scope, scope_ref, polarity, subject, "
    "content.  'scope_ref' may be null; the others are required.\n"
    "- Closed vocabularies. scope in "
    "['global','stage','scene','voice_block','artifact_type','element']. "
    "polarity in ['prefer','avoid','require','forbid']. subject in "
    "['tone','voice','pacing','visual_style','narrative_structure',"
    "'speaker_role','duration','music']. No other values.\n"
    "- A directive may legitimately split into multiple records. "
    "\"rewrite scene 3 and I prefer shorter narration\" -> one record "
    "scoped to scene 3, plus one global record about duration/pacing.\n"
    "- Implicit scope inference. \"Cassandra sounds flat\" -> voice_block "
    "scope, scope_ref the speaker name, subject speaker_role or voice.\n"
    "- If a scope_hint was provided by the UI (e.g. the reviewer had "
    "scene 3 selected), apply it to any record the directive does NOT "
    "otherwise scope. Do NOT apply the hint if the directive explicitly "
    "generalises ('globally', 'in general', 'overall', 'all scenes', "
    "'every scene').\n"
    "- scope_ref MUST be null when scope is 'global'.\n"
    "- 'content' is a short, imperative paraphrase of the directive slice "
    "this record covers.  Keep it under 200 characters.\n"
    "- No markdown. No prose outside the JSON."
)


_INTERPRETER_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": [s.value for s in Scope],
                    },
                    "scope_ref": {
                        "type": ["string", "null"],
                    },
                    "polarity": {
                        "type": "string",
                        "enum": [p.value for p in Polarity],
                    },
                    "subject": {
                        "type": "string",
                        "enum": [s.value for s in Subject],
                    },
                    "content": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
                "required": [
                    "scope",
                    "polarity",
                    "subject",
                    "content",
                ],
            },
        },
    },
    "required": ["records"],
}


def _default_llm_call(model: str, system: str, prompt: str) -> str:
    """Default LLM backend -- google-genai with structured output.

    Mirrors :func:`server.agents.diagnostic_classifier._default_llm_call`
    so both modules fail in identical ways when no provider key is
    configured.  Raises :class:`InterpreterError` on missing credentials
    so the heuristic fallback can take over cleanly (the callable interface
    catches :class:`InterpreterError` from the LLM and retries via
    heuristics before giving up).
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except Exception as exc:  # pragma: no cover -- defensive
        raise InterpreterError(
            f"google-genai is required for the default Preference Interpreter "
            f"LLM backend: {exc}"
        ) from exc

    api_key = (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_GENAI_API_KEY")
    )
    if not api_key:
        raise InterpreterError(
            "Preference Interpreter LLM step requires GOOGLE_API_KEY / "
            "GEMINI_API_KEY / GOOGLE_GENAI_API_KEY"
        )
    client = genai.Client(api_key=api_key)
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=_INTERPRETER_RESPONSE_SCHEMA,
        temperature=0.1,
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    return response.text or ""


# ---------------------------------------------------------------------------
# Heuristics -- used to salvage LLM parse failures and to enrich LLM output.
# ---------------------------------------------------------------------------


_GENERALISE_TOKENS: tuple[str, ...] = (
    "globally",
    "in general",
    "overall",
    "everywhere",
    "all scenes",
    "every scene",
    "throughout",
    "across the whole",
    "across the entire",
)

_SCENE_PATTERN = re.compile(
    r"scene[\s\-_]*(\d+)", re.IGNORECASE
)

_FORBID_TOKENS: tuple[str, ...] = (
    "never",
    "forbid",
    "must not",
    "must never",
    "do not ever",
)
_REQUIRE_TOKENS: tuple[str, ...] = (
    "must",
    "has to",
    "have to",
    "require",
    "required",
    "require that",
    "required to",
    "ensure that",
    "always",
)
_AVOID_TOKENS: tuple[str, ...] = (
    "avoid",
    "don't",
    "do not",
    "no more",
    "less",
    "stop",
    "sounds flat",
    "feels flat",
    "too",
)

# Subject hints -- (subject, token) pairs searched in directive order.
_SUBJECT_HINTS: tuple[tuple[Subject, tuple[str, ...]], ...] = (
    (Subject.DURATION, ("shorter", "longer", "duration", "length", "seconds", "minutes")),
    (Subject.PACING, ("pacing", "pace", "faster", "slower", "tighter", "snappier", "rushed")),
    (Subject.MUSIC, ("music", "score", "soundtrack", "synth", "bass")),
    (Subject.VISUAL_STYLE, ("visual", "color", "colour", "palette", "shot", "cinematography", "look")),
    (Subject.NARRATIVE_STRUCTURE, ("structure", "story", "arc", "narrative", "hook", "payoff")),
    (Subject.SPEAKER_ROLE, ("speaker", "sounds flat", "cassandra", "narrator", "voice-over")),
    (Subject.VOICE, ("voice", "tone of voice", "accent", "register", "intonation")),
    (Subject.TONE, ("tone", "warmer", "colder", "darker", "lighter", "serious", "playful")),
)


def _compile_token_pattern(tokens: Iterable[str]) -> re.Pattern[str]:
    """Compile a word-boundary alternation pattern for ``tokens``.

    Plain substring matching (``token in lower``) produces false positives
    where a short token appears embedded in an unrelated word -- e.g.
    ``"never"`` inside ``"whenever"`` or ``"avoid"`` inside ``"unavoidable"``
    (Devin Review flag on PR #171).  Using word boundaries around the
    alternation avoids that while still letting multi-word phrases
    (``"must not"``, ``"sounds flat"``) match cleanly.  ``re.escape``
    handles punctuation like apostrophes in ``"don't"``.
    """
    alternation = "|".join(re.escape(tok) for tok in tokens)
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


_FORBID_PATTERN = _compile_token_pattern(_FORBID_TOKENS)
_REQUIRE_PATTERN = _compile_token_pattern(_REQUIRE_TOKENS)
_AVOID_PATTERN = _compile_token_pattern(_AVOID_TOKENS)
_SUBJECT_PATTERNS: tuple[tuple[Subject, re.Pattern[str]], ...] = tuple(
    (subject, _compile_token_pattern(tokens))
    for subject, tokens in _SUBJECT_HINTS
)


def _detect_polarity(directive: str) -> Polarity:
    if _FORBID_PATTERN.search(directive):
        return Polarity.FORBID
    if _REQUIRE_PATTERN.search(directive):
        return Polarity.REQUIRE
    if _AVOID_PATTERN.search(directive):
        return Polarity.AVOID
    return Polarity.PREFER


def _detect_subject(directive: str) -> Subject:
    for subject, pattern in _SUBJECT_PATTERNS:
        if pattern.search(directive):
            return subject
    # Default bucket: tone is the most generic.
    return Subject.TONE


def _directive_generalises(directive: str) -> bool:
    lower = directive.lower()
    return any(token in lower for token in _GENERALISE_TOKENS)


def _extract_scene_scope_ref(directive: str) -> Optional[str]:
    match = _SCENE_PATTERN.search(directive)
    if match is None:
        return None
    return f"scene-{int(match.group(1))}"


def _split_directive_clauses(directive: str) -> list[str]:
    """Split on explicit conjunctions that typically separate preferences.

    Conservative on purpose: we only split on ``" and "``, ``"; "`` and
    ``". "`` so we don't fracture a single thought like
    "don't make Cassandra sound flat" into pieces.
    """
    # Normalise separators to a single token we can split on.
    normalised = re.sub(r"\s+;\s+", " ~SEP~ ", directive)
    normalised = re.sub(r"\.\s+(?=[A-Z])", " ~SEP~ ", normalised)
    normalised = re.sub(r"\s+and\s+(?=(also\s+)?(I\s+|we\s+|please\s+|prefer|avoid|make|keep|rewrite))", " ~SEP~ ", normalised, flags=re.IGNORECASE)
    parts = [p.strip() for p in normalised.split("~SEP~") if p.strip()]
    return parts or [directive.strip()]


@dataclass
class _DraftRecord:
    """Unvalidated draft used before enum coercion."""

    scope: str
    polarity: str
    subject: str
    content: str
    scope_ref: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


def _heuristic_parse(
    directive: str,
    *,
    scope_hint: Optional[Mapping[str, Any]],
) -> list[_DraftRecord]:
    """Last-resort heuristic parse.

    The goal is *not* to compete with the LLM -- the goal is to keep the
    system working on the directives we already see on the dashboard
    (scene references, speaker references, global shorter-narration asks)
    when the LLM backend is unavailable (CI, cost guards) or emits
    malformed JSON.
    """
    clauses = _split_directive_clauses(directive)
    drafts: list[_DraftRecord] = []
    generalises = _directive_generalises(directive)

    for clause in clauses:
        lower = clause.lower()
        polarity = _detect_polarity(clause)
        subject = _detect_subject(clause)

        scope: str
        scope_ref: Optional[str] = None

        scene_ref = _extract_scene_scope_ref(clause)
        if scene_ref is not None:
            scope = Scope.SCENE.value
            scope_ref = scene_ref
        elif subject is Subject.SPEAKER_ROLE or "cassandra" in lower:
            scope = Scope.VOICE_BLOCK.value
            # Capture a probable speaker name: first capitalised token that
            # isn't at sentence start.  Falls back to None (ledger accepts
            # None for non-GLOBAL scopes and A4 will treat it as "all
            # instances").
            name_match = re.search(r"\b([A-Z][a-z]{2,})\b", clause)
            if name_match is not None:
                scope_ref = name_match.group(1)
        elif scope_hint and not generalises:
            hint_scope = str(scope_hint.get("scope") or "").strip()
            hint_ref = scope_hint.get("scope_ref")
            if hint_scope:
                scope = hint_scope
                scope_ref = hint_ref if isinstance(hint_ref, str) else None
            else:
                scope = Scope.GLOBAL.value
        else:
            scope = Scope.GLOBAL.value

        if scope == Scope.GLOBAL.value:
            scope_ref = None

        drafts.append(
            _DraftRecord(
                scope=scope,
                scope_ref=scope_ref,
                polarity=polarity.value,
                subject=subject.value,
                content=clause.strip(),
                metadata={"parser": "heuristic"},
            )
        )

    if not drafts:
        raise InterpreterError(
            "Heuristic parse produced no records -- directive was empty after "
            "clause splitting"
        )
    return drafts


# ---------------------------------------------------------------------------
# LLM parse + validation
# ---------------------------------------------------------------------------


def _build_llm_prompt(
    directive: str,
    *,
    scope_hint: Optional[Mapping[str, Any]],
) -> str:
    payload = {
        "directive": directive,
        "scope_hint": dict(scope_hint) if scope_hint else None,
        "closed_vocabularies": {
            "scope": [s.value for s in Scope],
            "polarity": [p.value for p in Polarity],
            "subject": [s.value for s in Subject],
        },
    }
    return (
        "DIRECTIVE CONTEXT:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n\n"
        "Return a JSON object with key 'records' whose value is the list "
        "of scoped preference records."
    )


def _parse_llm_records(text: str) -> list[_DraftRecord]:
    if not text:
        raise InterpreterError("Empty interpreter LLM response")
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                raise InterpreterError(
                    f"Interpreter response is not valid JSON: {exc}"
                ) from exc
        else:
            raise InterpreterError(
                f"Interpreter response is not valid JSON: {exc}"
            ) from exc
    if not isinstance(data, Mapping):
        raise InterpreterError(
            f"Expected JSON object, got {type(data).__name__}"
        )
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise InterpreterError(
            "Interpreter response is missing a non-empty 'records' list"
        )

    drafts: list[_DraftRecord] = []
    for idx, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise InterpreterError(
                f"records[{idx}] must be an object, got {type(item).__name__}"
            )
        try:
            scope = str(item["scope"])
            polarity = str(item["polarity"])
            subject = str(item["subject"])
            content = str(item["content"])
        except KeyError as exc:
            raise InterpreterError(
                f"records[{idx}] missing required field: {exc}"
            ) from exc
        scope_ref_raw = item.get("scope_ref")
        if scope_ref_raw is not None and not isinstance(scope_ref_raw, str):
            raise InterpreterError(
                f"records[{idx}].scope_ref must be string or null, "
                f"got {type(scope_ref_raw).__name__}"
            )
        drafts.append(
            _DraftRecord(
                scope=scope,
                scope_ref=scope_ref_raw,
                polarity=polarity,
                subject=subject,
                content=content,
                metadata={"parser": "llm"},
            )
        )
    return drafts


def _validate_draft(draft: _DraftRecord) -> _DraftRecord:
    """Coerce enum strings; reject closed-vocabulary misses."""
    try:
        Scope(draft.scope)
    except ValueError as exc:
        raise InterpreterError(
            f"closed-vocab miss: scope={draft.scope!r} not in "
            f"{[s.value for s in Scope]}"
        ) from exc
    try:
        Polarity(draft.polarity)
    except ValueError as exc:
        raise InterpreterError(
            f"closed-vocab miss: polarity={draft.polarity!r} not in "
            f"{[p.value for p in Polarity]}"
        ) from exc
    try:
        Subject(draft.subject)
    except ValueError as exc:
        raise InterpreterError(
            f"closed-vocab miss: subject={draft.subject!r} not in "
            f"{[s.value for s in Subject]}"
        ) from exc
    if not draft.content.strip():
        raise InterpreterError("record content must be non-empty")
    # GLOBAL records may not carry a scope_ref.  This mirrors the ledger's
    # __post_init__ check but surfacing the error here gives a directive-
    # level message instead of a schema-level one.
    if draft.scope == Scope.GLOBAL.value and draft.scope_ref:
        raise InterpreterError(
            f"GLOBAL records must not carry a scope_ref "
            f"(got {draft.scope_ref!r})"
        )
    return draft


def _apply_scope_hint(
    drafts: Iterable[_DraftRecord],
    *,
    scope_hint: Optional[Mapping[str, Any]],
    directive: str,
) -> list[_DraftRecord]:
    """Apply a UI-supplied ``scope_hint`` to drafts that are still global.

    The hint is the slot the reviewer had selected on the dashboard.  We
    respect it unless the directive explicitly generalises.  Individual
    drafts that the LLM already scoped narrowly (scene-3, voice_block,
    etc.) are left alone -- the hint only fills in unscoped ones.
    """
    out: list[_DraftRecord] = []
    if not scope_hint or _directive_generalises(directive):
        out.extend(drafts)
        return out
    hint_scope = str(scope_hint.get("scope") or "").strip()
    hint_ref = scope_hint.get("scope_ref")
    if not hint_scope:
        out.extend(drafts)
        return out
    try:
        hint_scope_enum = Scope(hint_scope)
    except ValueError as exc:
        raise InterpreterError(
            f"scope_hint.scope={hint_scope!r} is not a valid Scope"
        ) from exc
    if hint_scope_enum is Scope.GLOBAL:
        # A GLOBAL hint is a no-op: unscoped drafts are already GLOBAL.
        out.extend(drafts)
        return out
    if hint_ref is not None and not isinstance(hint_ref, str):
        raise InterpreterError(
            f"scope_hint.scope_ref must be string or null, "
            f"got {type(hint_ref).__name__}"
        )
    for draft in drafts:
        if draft.scope == Scope.GLOBAL.value:
            out.append(
                _DraftRecord(
                    scope=hint_scope_enum.value,
                    scope_ref=hint_ref,
                    polarity=draft.polarity,
                    subject=draft.subject,
                    content=draft.content,
                    metadata={
                        **(draft.metadata or {}),
                        "scope_hint_applied": True,
                    },
                )
            )
        else:
            out.append(draft)
    return out


# ---------------------------------------------------------------------------
# Public callable -- the dashboard proactive-L4 entry point
# ---------------------------------------------------------------------------


def interpret_directive(
    directive_text: str,
    *,
    reviewer: str,
    l4_event_id: str,
    scope_hint: Optional[Mapping[str, Any]] = None,
    state: MutableMapping[str, Any],
    timestamp: Optional[str] = None,
    use_llm: bool = True,
) -> list[PreferenceRecord]:
    """Parse an L4 directive into scoped :class:`PreferenceRecord` entries.

    This is the single entry point the dashboard proactive-L4 path
    (ARCH-H5) should invoke when a reviewer submits free-form natural
    language.  Every returned record has already been appended to the
    ledger via :func:`append_preference`.

    Parameters
    ----------
    directive_text:
        The raw, free-form reviewer text.  Must be non-empty and non-blank.
    reviewer:
        Reviewer identity captured into ``Origin.reviewer``.
    l4_event_id:
        Identifier of the L4 event that produced this directive.  Every
        emitted record carries this as ``Origin.l4_event_id`` so the
        ledger entry is traceable back to the triggering event.
    scope_hint:
        Optional mapping from the dashboard (ARCH-H4 slot selection).
        Keys: ``scope`` (required string matching a :class:`Scope` value)
        and optional ``scope_ref`` (string).  Applied to draft records
        that are otherwise global, UNLESS the directive explicitly
        generalises ("globally", "all scenes", ...).
    state:
        ADK session state (or any mutable mapping).  The ledger lives
        under :data:`PREFERENCE_LEDGER_KEY` here.  This is a keyword-only
        argument by design -- callers should be explicit about which
        blackboard the records land in.
    timestamp:
        Optional ISO-8601 string stamped into :class:`Origin`.  Defaults
        to ``datetime.now(timezone.utc).isoformat()``.
    use_llm:
        When ``True`` (default), the interpreter tries the LLM first and
        falls back to heuristics on parse failure.  When ``False``, the
        LLM is skipped entirely -- useful for tests and for offline runs.

    Returns
    -------
    list[PreferenceRecord]
        The records that were just appended to the ledger, in append order.

    Raises
    ------
    ValueError
        If ``directive_text`` is blank or required identifiers are missing.
    InterpreterError
        If BOTH the LLM path and the heuristic fallback fail to produce a
        valid record set.  Never silently emit a fabricated record.
    """
    if not isinstance(directive_text, str) or not directive_text.strip():
        raise ValueError("directive_text must be a non-empty string")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer must be a non-empty string")
    if not isinstance(l4_event_id, str) or not l4_event_id.strip():
        raise ValueError("l4_event_id must be a non-empty string")
    if state is None:
        raise ValueError("state must be provided (the ADK session blackboard)")

    directive = directive_text.strip()
    ts = timestamp or datetime.now(timezone.utc).isoformat()

    # 1. Try the LLM path.
    drafts: Optional[list[_DraftRecord]] = None
    llm_error: Optional[Exception] = None
    if use_llm:
        try:
            call = (_llm_client_factory() if _llm_client_factory is not None
                    else _default_llm_call)
            raw = call(
                _INTERPRETER_MODEL,
                _INTERPRETER_SYSTEM_INSTRUCTION,
                _build_llm_prompt(directive, scope_hint=scope_hint),
            )
            drafts = _parse_llm_records(raw)
        except InterpreterError as exc:
            llm_error = exc
            logger.warning(
                "PreferenceInterpreter LLM parse failed (%s) -- falling back "
                "to heuristics",
                exc,
            )
        except Exception as exc:  # noqa: BLE001 -- defensive
            llm_error = exc
            logger.warning(
                "PreferenceInterpreter LLM backend error (%s) -- falling "
                "back to heuristics",
                exc,
            )

    # 2. Heuristic fallback.
    if drafts is None:
        try:
            drafts = _heuristic_parse(directive, scope_hint=scope_hint)
        except InterpreterError as exc:
            if llm_error is not None:
                raise InterpreterError(
                    f"Both LLM and heuristic parse failed: llm={llm_error}; "
                    f"heuristic={exc}"
                ) from exc
            raise

    # 3. Apply scope_hint to still-global drafts (unless the directive
    #    explicitly generalises).  For LLM drafts the model usually handled
    #    this already; this is the belt-and-braces pass.
    drafts = _apply_scope_hint(
        drafts, scope_hint=scope_hint, directive=directive
    )

    # 4. Validate closed vocabularies up front -- fail loud before mutating
    #    the ledger.  Validating all drafts before appending any keeps the
    #    append-only invariant clean (we never get half-way through).
    validated: list[_DraftRecord] = [_validate_draft(d) for d in drafts]

    # 5. Append each record through the ledger API so revision assignment,
    #    append-only invariants, and blackboard persistence all stay in
    #    one place.
    origin = Origin(
        l4_event_id=l4_event_id,
        reviewer=reviewer,
        timestamp=ts,
    )
    appended: list[PreferenceRecord] = []
    for draft in validated:
        record = append_preference(
            state,
            scope=Scope(draft.scope),
            scope_ref=draft.scope_ref,
            polarity=Polarity(draft.polarity),
            subject=Subject(draft.subject),
            content=draft.content,
            origin=origin,
            metadata=draft.metadata or {},
        )
        appended.append(record)

    logger.info(
        "PreferenceInterpreter: event=%s reviewer=%s emitted %d record(s) "
        "(rev %d..%d)",
        l4_event_id,
        reviewer,
        len(appended),
        appended[0].revision,
        appended[-1].revision,
    )
    return appended


# ---------------------------------------------------------------------------
# ADK Agent wrapper -- composed via the normal SequentialAgent / LoopAgent
# ---------------------------------------------------------------------------

_INTERPRETER_AGENT_INSTRUCTION = """\
You are the Preference Interpreter ADK wrapper.  The directive is parsed
deterministically by ``interpret_directive`` (invoked from the before-agent
callback).  Your role is only to emit a short human-readable summary of
what just landed in the Preference Ledger.
"""


def _read_directive_blob(state: Any) -> Optional[dict[str, Any]]:
    """Pull the staged directive blob out of ``state``.

    The blob may be either a mapping (test-friendly) or a JSON-encoded
    string (how the dashboard will usually stage it, matching the
    ``scenes`` / ``otio_mutations`` convention already used elsewhere).
    """
    raw = state.get(PREFERENCE_INTERPRETER_INPUT_KEY)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise InterpreterError(
                f"{PREFERENCE_INTERPRETER_INPUT_KEY!r} is not valid JSON: "
                f"{exc}"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise InterpreterError(
                f"{PREFERENCE_INTERPRETER_INPUT_KEY!r} must decode to an "
                f"object, got {type(decoded).__name__}"
            )
        return dict(decoded)
    raise InterpreterError(
        f"{PREFERENCE_INTERPRETER_INPUT_KEY!r} must be a mapping or JSON "
        f"string, got {type(raw).__name__}"
    )


def _interpreter_before_agent_callback(callback_context: Any) -> Any:
    """ADK ``before_agent_callback`` wrapper.

    Contract: the caller stages the directive under
    :data:`PREFERENCE_INTERPRETER_INPUT_KEY` (either a mapping or a JSON
    string with keys ``text`` / ``reviewer`` / ``l4_event_id`` / optional
    ``scope_hint`` / ``timestamp``).  The callback runs
    ``interpret_directive``, writes the new records into the ledger, and
    returns a short ``Content`` so the wrapped LLM call is skipped
    (mirroring the Timeline Guardian / diagnostic-classifier pattern).
    The ``output_key`` captures the summary text.
    """
    from google.genai import types as genai_types

    state = callback_context.state
    blob = _read_directive_blob(state)
    if blob is None:
        # Agent invoked without a staged directive -- emit a no-op summary
        # so the pipeline continues.  Fail-loud is reserved for malformed
        # directives; absence of a directive is a legitimate skip.
        summary = "PREFERENCE_INTERPRETER: no directive staged (noop)"
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=summary)],
        )

    text = blob.get("text") or blob.get("directive")
    if not text:
        raise InterpreterError(
            f"{PREFERENCE_INTERPRETER_INPUT_KEY!r} must contain a non-empty "
            f"'text' (or 'directive') field"
        )
    reviewer = blob.get("reviewer") or "l4-dashboard"
    l4_event_id = blob.get("l4_event_id")
    if not l4_event_id:
        raise InterpreterError(
            f"{PREFERENCE_INTERPRETER_INPUT_KEY!r} must contain a non-empty "
            f"'l4_event_id' field"
        )
    scope_hint = blob.get("scope_hint")
    if scope_hint is not None and not isinstance(scope_hint, Mapping):
        raise InterpreterError(
            f"{PREFERENCE_INTERPRETER_INPUT_KEY!r}.scope_hint must be an "
            f"object, got {type(scope_hint).__name__}"
        )
    timestamp = blob.get("timestamp")

    pre_revision = current_revision(state)
    records = interpret_directive(
        str(text),
        reviewer=str(reviewer),
        l4_event_id=str(l4_event_id),
        scope_hint=scope_hint,
        state=state,
        timestamp=str(timestamp) if timestamp else None,
    )
    state["_preference_interpreter_last_revisions"] = [r.revision for r in records]
    state["_preference_interpreter_pre_revision"] = pre_revision

    summary = (
        f"PREFERENCE_INTERPRETER: event={l4_event_id} reviewer={reviewer} "
        f"emitted {len(records)} record(s) "
        f"(rev {records[0].revision}..{records[-1].revision}) -- "
        + ", ".join(
            f"[{r.scope.value}"
            + (f":{r.scope_ref}" if r.scope_ref else "")
            + f" {r.polarity.value} {r.subject.value}]"
            for r in records
        )
    )
    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=summary)],
    )


def _interpreter_after_agent_callback(callback_context: Any) -> None:
    """Stage-boundary check: every emitted record must be in the ledger.

    Timeline Guardian pattern.  Fail-loud: any revision we *said* we
    appended that is not actually present in the ledger raises
    :class:`InterpreterError` so the pipeline stops instead of silently
    drifting.  L0-L3 never write to the ledger, so the only way a rev
    could vanish is a catastrophic state-manager bug -- exactly the case
    worth screaming about.
    """
    state = callback_context.state
    revisions = state.get("_preference_interpreter_last_revisions") or []
    if not revisions:
        return None
    try:
        present = {record.revision for record in list_preferences(state)}
    except Exception as exc:  # noqa: BLE001 -- propagate as interpreter error
        raise InterpreterError(
            f"after_agent_callback could not read preference ledger: {exc}"
        ) from exc
    missing = [rev for rev in revisions if rev not in present]
    if missing:
        raise InterpreterError(
            f"Preference Interpreter appended revisions {revisions} but "
            f"ledger is missing {missing}; append-only invariant violated"
        )
    return None


def _build_preference_interpreter_agent() -> Any:
    """Build the ADK ``Agent`` wrapper for the preference interpreter.

    Returns ``None`` — ADK agent wrapper removed.  The pure-Python
    ``interpret_directive`` entry point still works.
    """
    return None


preference_interpreter_agent = None


__all__ = [
    "PREFERENCE_INTERPRETER_INPUT_KEY",
    "PREFERENCE_INTERPRETER_SUMMARY_KEY",
    "InterpreterError",
    "LLMCallable",
    "interpret_directive",
    "preference_interpreter_agent",
]
