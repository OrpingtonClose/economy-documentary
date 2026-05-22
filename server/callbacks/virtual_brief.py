"""
Virtual Brief assembler (ARCH-A4, issue #134).

Every pipeline stage used to "read the prompt". That flat-prompt model was
replaced by the Preference Ledger (ARCH-A1 / #131, module
:mod:`server.callbacks.preference_ledger`), which stores user intent as
scoped, polarised, subject-typed, revision-stamped preference records.

This module is the callable that stages will use *instead of* reading a
prompt. Given a request (stage, scope, scope_ref, optional subject), it
walks the ledger in session state and returns a :class:`VirtualBrief`:

* the applicable ledger records, sorted less-specific-first then
  older-first (the canonical display / audit order),
* the effective decision per subject (which record wins),
* any *hard* conflicts surfaced -- these MUST re-escalate to a human
  (per diagram 2a) and cannot be resolved silently.

Spec (per issue #134 / diagram 2a in ``docs/ARCHITECTURE_DIAGRAMS.md``):

1. **Scope filter** -- include only records whose scope matches or is a
   container of the requested scope (hierarchical containment).
2. **Specificity sort** -- less specific first:
   ``global < stage < scene < voice_block < artifact_type < element``.
3. **Recency sort within same specificity** -- newer (higher revision)
   wins.
4. **Hard polarity dominance** -- ``require`` / ``forbid`` dominate
   ``prefer`` / ``avoid`` when they apply to the same subject+scope.
5. **Hard contradiction** -- two hard records with opposite polarities on
   the same subject+scope CANNOT be silently resolved. They surface as a
   :class:`HardConflict` which downstream (ARCH-H5) displays to a
   reviewer for re-escalation.

This delivery is the assembler surface only. It does NOT rewire existing
stages to consume it -- that is a separate "virtual-brief consumer"
ticket. Stages continue to read their prompts in this PR.

Design invariants (fail loud, no silent degradation):

* Unknown ``scope`` / ``subject`` / ``polarity`` string → ``ValueError``.
* ``scope_ref`` passed without a ``scope`` → ``ValueError``.
* ``scope=GLOBAL`` with a ``scope_ref`` → ``ValueError``.
* Malformed ledger state (bad JSON, missing fields, unknown enum members)
  bubbles up from
  :func:`server.callbacks.preference_ledger.list_preferences`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from callbacks.preference_ledger import (
    Polarity,
    PreferenceRecord,
    Scope,
    Subject,
    list_preferences,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scope hierarchy
# ---------------------------------------------------------------------------

#: Specificity ranking per issue #131 / diagram 2a. ``GLOBAL`` is least
#: specific (rank 0); ``ELEMENT`` is most specific. Used for sort order and
#: for "broader/narrower" comparisons in the scope-containment filter.
_SPECIFICITY: dict[Scope, int] = {
    Scope.GLOBAL: 0,
    Scope.STAGE: 1,
    Scope.SCENE: 2,
    Scope.VOICE_BLOCK: 3,
    Scope.ARTIFACT_TYPE: 4,
    Scope.ELEMENT: 5,
}


def _specificity(scope: Scope) -> int:
    try:
        return _SPECIFICITY[scope]
    except KeyError as exc:  # pragma: no cover -- all Scope members covered.
        raise ValueError(f"unknown scope for specificity: {scope!r}") from exc


_HARD_POLARITIES = frozenset({Polarity.REQUIRE, Polarity.FORBID})


def _is_hard(polarity: Polarity) -> bool:
    return polarity in _HARD_POLARITIES


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveDecision:
    """The winning preference for a given subject after conflict resolution.

    ``record`` is the ledger record that produced the decision; keeping it
    makes the brief fully auditable (a dashboard can show "scene-3 tone is
    REQUIRE 'warm' because of L4 event X at revision N").
    """

    subject: Subject
    polarity: Polarity
    content: str
    record: PreferenceRecord


@dataclass(frozen=True)
class HardConflict:
    """Two hard records with opposite polarities at the same subject+scope.

    Per diagram 2a this cannot be silently resolved; the caller MUST
    re-escalate to a human reviewer (ARCH-H5). Both colliding records are
    carried so the dashboard can show provenance.
    """

    subject: Subject
    scope: Scope
    scope_ref: Optional[str]
    records: tuple[PreferenceRecord, ...]
    message: str


@dataclass(frozen=True)
class VirtualBrief:
    """Merged scope-filtered view of the Preference Ledger for one request.

    ``applicable_records`` is sorted less-specific-first, then older-first
    (revision-ascending). ``decisions`` is keyed by :class:`Subject`; a
    subject appears iff at least one applicable record carries that subject
    AND it is not blocked by a :class:`HardConflict`. ``hard_conflicts``
    is empty for the common case.
    """

    stage: Optional[str]
    scope: Optional[Scope]
    scope_ref: Optional[str]
    subject: Optional[Subject]
    applicable_records: tuple[PreferenceRecord, ...]
    decisions: Mapping[Subject, EffectiveDecision]
    hard_conflicts: tuple[HardConflict, ...]

    @property
    def has_hard_conflict(self) -> bool:
        """True iff at least one :class:`HardConflict` was surfaced."""
        return bool(self.hard_conflicts)

    def decision_for(self, subject: Subject | str) -> Optional[EffectiveDecision]:
        """Convenience lookup. Returns ``None`` when no decision exists."""
        subject_enum = subject if isinstance(subject, Subject) else Subject(subject)
        return self.decisions.get(subject_enum)


# ---------------------------------------------------------------------------
# Scope containment filter
# ---------------------------------------------------------------------------


def _record_applies(
    record: PreferenceRecord,
    *,
    request_stage: Optional[str],
    request_scope: Optional[Scope],
    request_scope_ref: Optional[str],
) -> bool:
    """Return True iff ``record`` applies to the requested view.

    Containment semantics (diagram 2a): broader-or-equal scopes apply to
    narrower requests; narrower scopes NEVER leak into broader requests.

    Implementation rules (conservative -- matches only when we can prove
    the record is relevant; otherwise excludes):

    * ``GLOBAL`` records always apply.
    * ``STAGE`` records apply iff ``record.scope_ref is None`` (stage
      record that fires on every stage) or ``record.scope_ref ==
      request_stage`` (stage record scoped to this specific stage; only
      matches when the caller told us which stage we're in).
    * Deeper records (``SCENE`` / ``VOICE_BLOCK`` / ``ARTIFACT_TYPE`` /
      ``ELEMENT``) apply iff the caller narrowed the request to at least
      that scope (``request_scope`` is at the record's level or narrower)
      AND either the record is unrefed (``record.scope_ref is None``,
      applies to any instance of that level) or its ``scope_ref`` matches
      the request's ``scope_ref`` at the same level.

    We deliberately do NOT try to infer cross-level ref containment
    (e.g. "voice block vb-1 is inside scene-3 so the scene-3 record
    applies to a vb-1 request"). The request carries only one
    ``scope_ref``; if a stage needs to pull in intermediate-level
    scoped records, ledger authors must either (a) leave those records
    unrefed, or (b) a future ticket will add an explicit scope chain.
    """
    rec_scope = record.scope
    rec_ref = record.scope_ref

    if rec_scope is Scope.GLOBAL:
        return True

    if rec_scope is Scope.STAGE:
        if rec_ref is None:
            return True
        return request_stage is not None and rec_ref == request_stage

    # rec_scope is SCENE / VOICE_BLOCK / ARTIFACT_TYPE / ELEMENT.
    if request_scope is None:
        # Caller didn't narrow past STAGE. Narrower records don't apply.
        return False

    rec_rank = _specificity(rec_scope)
    req_rank = _specificity(request_scope)
    if rec_rank > req_rank:
        # Record is strictly narrower than request -- exclude.
        return False
    if rec_rank < req_rank:
        # Record is strictly broader. Without a scope chain we only
        # accept unrefed broader records (applies to any narrower
        # instance).
        return rec_ref is None
    # Same level -- scope_refs must match, or the record is unrefed
    # (applies to any instance at this level).
    return rec_ref is None or rec_ref == request_scope_ref


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def _sort_key(record: PreferenceRecord) -> tuple[int, int]:
    """Sort key: less-specific-first, then revision-ascending (older first).

    This is the canonical display and audit order. The effective-decision
    iterator relies on "later in this list = wins" (most specific / most
    recent at the end).
    """
    return (_specificity(record.scope), record.revision)


# ---------------------------------------------------------------------------
# Decision + conflict resolution
# ---------------------------------------------------------------------------


def _find_hard_conflicts(
    records: Iterable[PreferenceRecord],
) -> list[HardConflict]:
    """Return one :class:`HardConflict` per (subject, scope, scope_ref) group
    where both ``REQUIRE`` and ``FORBID`` appear.

    Soft-vs-soft contradictions (``PREFER`` vs ``AVOID``) are NOT flagged
    -- they are resolved by dominance and recency, not by human
    re-escalation. Two hard records with the same polarity but different
    content (e.g. two ``REQUIRE``s on tone) are also NOT a
    :class:`HardConflict` at this layer: the ledger treats them as
    overrides (later revision wins), and semantic contradiction of
    free-form content is a separate concern (ARCH-A5).
    """
    by_group: dict[
        tuple[Subject, Scope, Optional[str]],
        dict[Polarity, list[PreferenceRecord]],
    ] = {}
    for record in records:
        if not _is_hard(record.polarity):
            continue
        key = (record.subject, record.scope, record.scope_ref)
        by_group.setdefault(key, {}).setdefault(record.polarity, []).append(record)

    conflicts: list[HardConflict] = []
    for (subject, scope, scope_ref), polarity_map in by_group.items():
        requires = polarity_map.get(Polarity.REQUIRE, [])
        forbids = polarity_map.get(Polarity.FORBID, [])
        if requires and forbids:
            combined = tuple(sorted(requires + forbids, key=_sort_key))
            scope_label = scope.value + (f"[{scope_ref}]" if scope_ref else "")
            conflicts.append(
                HardConflict(
                    subject=subject,
                    scope=scope,
                    scope_ref=scope_ref,
                    records=combined,
                    message=(
                        f"hard contradiction on subject={subject.value} "
                        f"scope={scope_label}: "
                        f"REQUIRE(revs={[r.revision for r in requires]}) "
                        f"vs FORBID(revs={[r.revision for r in forbids]})"
                    ),
                )
            )
    return conflicts


def _compute_decisions(
    sorted_records: list[PreferenceRecord],
    conflicted_subjects: frozenset[tuple[Subject, Scope, Optional[str]]],
) -> dict[Subject, EffectiveDecision]:
    """Collapse ``sorted_records`` into one effective decision per subject.

    Iterates in sorted order (less-specific-first, older-first). For each
    subject tracks the current winner, applying hard-polarity dominance:

    * Hard beats soft regardless of specificity / recency.
    * Within the same hardness tier, the later record in sort order wins
      (= more specific, then more recent).

    Subjects that appear in ``conflicted_subjects`` (any
    ``(subject, scope, scope_ref)`` carrying a :class:`HardConflict`) are
    excluded from the decision map: the caller must resolve the conflict
    before trusting a decision for that subject.
    """
    winner: dict[Subject, PreferenceRecord] = {}
    subjects_with_hard_conflict = {s for (s, _sc, _sr) in conflicted_subjects}

    for record in sorted_records:
        if record.subject in subjects_with_hard_conflict:
            continue
        current = winner.get(record.subject)
        if current is None:
            winner[record.subject] = record
            continue
        cur_hard = _is_hard(current.polarity)
        new_hard = _is_hard(record.polarity)
        if cur_hard and not new_hard:
            # Soft cannot displace a hard winner, even if newer.
            continue
        if new_hard and not cur_hard:
            winner[record.subject] = record
            continue
        # Same hardness tier: later in sort order wins.
        winner[record.subject] = record

    return {
        subject: EffectiveDecision(
            subject=subject,
            polarity=record.polarity,
            content=record.content,
            record=record,
        )
        for subject, record in winner.items()
    }


# ---------------------------------------------------------------------------
# Public API -- pure callable
# ---------------------------------------------------------------------------


def assemble_virtual_brief(
    state: Mapping[str, Any],
    *,
    stage: Optional[str] = None,
    scope: Optional[Scope | str] = None,
    scope_ref: Optional[str] = None,
    subject: Optional[Subject | str] = None,
) -> VirtualBrief:
    """Assemble the virtual brief for a single request.

    Args:
        state: ADK session state (or any mapping) that contains the
            Preference Ledger under
            :data:`server.callbacks.preference_ledger.PREFERENCE_LEDGER_KEY`.
            An absent / empty ledger is treated as "no preferences".
        stage: Optional pipeline stage name (e.g. ``"audio"``,
            ``"visual_direction"``). Used to match ``Scope.STAGE``
            records that carry a matching ``scope_ref``.
        scope: Optional narrower scope (``Scope`` or its string form).
            If ``None``, only ``GLOBAL`` and ``STAGE`` records apply.
        scope_ref: Optional identifier for the specific instance at
            ``scope`` (e.g. ``"scene-3"``). Must be ``None`` when
            ``scope`` is ``None`` or ``Scope.GLOBAL``.
        subject: Optional subject filter. When set, only records with
            this subject are considered for inclusion and decisions.

    Returns:
        A :class:`VirtualBrief` with applicable records (sorted),
        effective decisions per subject, and any :class:`HardConflict`
        instances surfaced.

    Raises:
        ValueError / TypeError: On malformed input (unknown enum
            string, ``scope_ref`` without ``scope``, ``scope_ref`` on
            ``GLOBAL``, malformed ledger state, etc.). Fail-loud by
            design.
    """
    # --- normalise + validate inputs ---
    if stage is not None and (not isinstance(stage, str) or not stage):
        raise ValueError(f"stage must be a non-empty string or None, got {stage!r}")

    scope_enum: Optional[Scope]
    if scope is None:
        scope_enum = None
    elif isinstance(scope, Scope):
        scope_enum = scope
    elif isinstance(scope, str):
        try:
            scope_enum = Scope(scope)
        except ValueError as exc:
            raise ValueError(f"unknown scope: {scope!r}") from exc
    else:
        raise TypeError(
            f"scope must be Scope, str, or None, got {type(scope).__name__}"
        )

    if scope_ref is not None and not isinstance(scope_ref, str):
        raise TypeError(
            f"scope_ref must be str or None, got {type(scope_ref).__name__}"
        )
    if scope_ref is not None and not scope_ref:
        raise ValueError("scope_ref must be a non-empty string when provided")
    if scope_ref is not None and scope_enum is None:
        raise ValueError("scope_ref requires a scope to be specified")
    if scope_enum is Scope.GLOBAL and scope_ref is not None:
        raise ValueError(
            f"scope_ref must be None for Scope.GLOBAL (got {scope_ref!r})"
        )

    subject_enum: Optional[Subject]
    if subject is None:
        subject_enum = None
    elif isinstance(subject, Subject):
        subject_enum = subject
    elif isinstance(subject, str):
        try:
            subject_enum = Subject(subject)
        except ValueError as exc:
            raise ValueError(f"unknown subject: {subject!r}") from exc
    else:
        raise TypeError(
            f"subject must be Subject, str, or None, got {type(subject).__name__}"
        )

    # --- load ledger (may raise -- fail loud on malformed state) ---
    all_records = list_preferences(state)

    # --- scope filter ---
    applicable = [
        rec
        for rec in all_records
        if _record_applies(
            rec,
            request_stage=stage,
            request_scope=scope_enum,
            request_scope_ref=scope_ref,
        )
    ]

    # --- subject filter (narrows both the record list and decisions) ---
    if subject_enum is not None:
        applicable = [rec for rec in applicable if rec.subject is subject_enum]

    # --- sort: less-specific-first, older-first ---
    applicable.sort(key=_sort_key)

    # --- detect hard conflicts within the applicable set ---
    hard_conflicts = _find_hard_conflicts(applicable)
    conflicted_keys = frozenset(
        (c.subject, c.scope, c.scope_ref) for c in hard_conflicts
    )

    # --- compute effective decisions ---
    decisions = _compute_decisions(applicable, conflicted_keys)

    if hard_conflicts:
        for conflict in hard_conflicts:
            logger.warning(
                "virtual_brief surfaced HardConflict: %s", conflict.message
            )

    return VirtualBrief(
        stage=stage,
        scope=scope_enum,
        scope_ref=scope_ref,
        subject=subject_enum,
        applicable_records=tuple(applicable),
        decisions=decisions,
        hard_conflicts=tuple(hard_conflicts),
    )


# ---------------------------------------------------------------------------
# ADK Agent wrapper
# ---------------------------------------------------------------------------


#: Blackboard key under which ``assemble_virtual_brief_tool`` stashes the
#: most recently assembled brief (JSON-friendly dict). Downstream stages
#: SHOULD read via this key, not by importing this module (blackboard-only
#: cross-stage access, per meta #122 DoD).
VIRTUAL_BRIEF_OUTPUT_KEY = "virtual_brief"


def _brief_to_dict(brief: VirtualBrief) -> dict[str, Any]:
    """Serialise a :class:`VirtualBrief` to a JSON-friendly dict."""
    return {
        "stage": brief.stage,
        "scope": brief.scope.value if brief.scope is not None else None,
        "scope_ref": brief.scope_ref,
        "subject": brief.subject.value if brief.subject is not None else None,
        "applicable_records": [rec.to_dict() for rec in brief.applicable_records],
        "decisions": {
            subject.value: {
                "polarity": decision.polarity.value,
                "content": decision.content,
                "revision": decision.record.revision,
                "scope": decision.record.scope.value,
                "scope_ref": decision.record.scope_ref,
            }
            for subject, decision in brief.decisions.items()
        },
        "hard_conflicts": [
            {
                "subject": c.subject.value,
                "scope": c.scope.value,
                "scope_ref": c.scope_ref,
                "message": c.message,
                "records": [r.to_dict() for r in c.records],
            }
            for c in brief.hard_conflicts
        ],
    }


def assemble_virtual_brief_tool(
    state: Mapping[str, Any],
    *,
    stage: Optional[str] = None,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    subject: Optional[str] = None,
) -> dict[str, Any]:
    """ADK tool wrapper: calls :func:`assemble_virtual_brief` and returns a
    JSON-serialisable dict.

    Registered on the :class:`Agent` as a plain callable (meta #122 DoD:
    "Tools as plain callables"). Fails loud on any invalid input.
    """
    brief = assemble_virtual_brief(
        state,
        stage=stage,
        scope=scope,
        scope_ref=scope_ref,
        subject=subject,
    )
    return _brief_to_dict(brief)


def virtual_brief_after_agent_callback(callback_context):  # pragma: no cover
    """Stage-boundary check: fail loud if the last-assembled brief carries
    a :class:`HardConflict`.

    Per meta #122 DoD stage-boundary checks run via
    ``after_agent_callback``. Consumer stages (a future ticket) will
    attach this callback so a hard contradiction halts the pipeline
    until a human re-escalates. Kept advisory-off in this delivery
    because no stage is wired to the assembler yet.
    """
    state = callback_context.state
    raw = state.get(VIRTUAL_BRIEF_OUTPUT_KEY)
    if not raw:
        return None
    if isinstance(raw, dict) and raw.get("hard_conflicts"):
        conflicts = raw["hard_conflicts"]
        raise RuntimeError(
            f"virtual_brief HardConflict ({len(conflicts)}): "
            + "; ".join(c.get("message", "") for c in conflicts)
        )
    return None


__all__ = [
    "VIRTUAL_BRIEF_OUTPUT_KEY",
    "EffectiveDecision",
    "HardConflict",
    "VirtualBrief",
    "assemble_virtual_brief",
    "assemble_virtual_brief_tool",
        "virtual_brief_after_agent_callback",
]
