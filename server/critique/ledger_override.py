"""Preference-Ledger scoped-override for stylistic invariants
(ARCH-E4, issue #150; parent ARCH-E #127; meta ARCH-2026 #122).

Stylistic QA (ARCH-E3, #149) enforces a uniform LUFS target across
every narration block. That blanket rule is correct for most films,
but a deliberate reviewer directive — "Cassandra is louder in scene 3
by +3 LU" — must be honoured without disabling the invariant for
everyone else.

This module answers a single question at the invariant-checker path:

> "Is there a live Preference Ledger record that deliberately
>  overrides the uniform-LUFS invariant for THIS specific block?"

A positive answer suppresses the uniform-LUFS check on that block
only. All other invariants (peak limiter, voice continuity, character
voice consistency, clicks, plosives, hiss floor) stay in force
regardless of any ledger record — they guard against
**degradation**, not **intent**, and no reviewer directive should
ever suppress them.

Scope resolution (ARCH-A4 virtual-brief assembler, #134):

The resolver walks from the narrowest block-identifying scope towards
the broadest (STAGE / GLOBAL) and asks the virtual-brief assembler
for applicable ``VOICE`` records at each level:

1. ``VOICE_BLOCK`` with ``scope_ref == block.block_id`` (e.g.
   ``"scene_003_V1_RU"``) — the most specific "this one block"
   directive.
2. ``VOICE_BLOCK`` with ``scope_ref == block.voice_role`` (e.g.
   ``"V1"``, ``"Cassandra"``) — "this role across the film".
3. ``SCENE`` with ``scope_ref == str(block.scene_num)`` (e.g.
   ``"3"``) — "every block in this scene".
4. ``STAGE`` / ``GLOBAL`` with no ``scope_ref`` — "every audio
   block" (in practice rare, but the vocabulary allows it).

At each level, a record is a loudness override iff:

* It carries :attr:`Subject.VOICE` (the closed-vocabulary member that
  covers loudness / register directives — see #131).
* Its effective polarity is ``PREFER`` or ``REQUIRE`` (AVOID /
  FORBID would *enforce* the invariant, not suppress it).
* Either ``metadata["aspect"] == "loudness"`` (the structured
  interpreter-written form, per ARCH-A2 #132) OR the record's
  free-form ``content`` names a loudness directive via keywords
  (``loud``, ``quiet``, ``lufs``, ``softer``, ``volume``, …). The
  metadata form is preferred; keyword fallback keeps reviewer
  directives written in plain prose honoured.

Hard-conflict handling (diagram 2a): when the virtual brief reports a
:class:`HardConflict` on ``Subject.VOICE`` at a level we would
otherwise consult, this module raises ``RuntimeError`` — the
invariant checker MUST NOT silently honour one side of a review
contradiction. ARCH-H5 re-escalates the conflict to a human.

Fail-loud posture: malformed ledger data, unknown enum members, or
the virtual-brief assembler itself raising bubbles up. The ONLY
graceful fallback is a missing ledger module (pre-#131 world) — in
that case ``is_lufs_override_active`` returns ``False`` and the
invariants stay fully in force. Fail-loud is the safe default.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from critique.audio_invariants import NarrationBlock

logger = logging.getLogger(__name__)


#: Metadata key that flags a ``Subject.VOICE`` record as a structured
#: loudness directive. Preferred over the content keyword heuristic;
#: ARCH-A2's Preference Interpreter (#132) writes this field when it
#: parses an L4 "louder by +3 LU" directive into a ledger record.
LOUDNESS_ASPECT_KEY = "aspect"
LOUDNESS_ASPECT_VALUE = "loudness"

#: Keywords that flag a ``Subject.VOICE`` record as a loudness
#: directive in free-form ``content``. Heuristic fallback used when
#: ``metadata["aspect"]`` is absent — reviewer directives are often
#: written as plain prose ("Cassandra should be louder in scene 3")
#: and we must honour those even before A2 rewrites them.
_LUFS_CONTENT_KEYWORDS: tuple[str, ...] = (
    "loud",
    "quiet",
    "lufs",
    "lu ",
    "lu.",
    "gain",
    "softer",
    "volume",
    "loudness",
)

#: Polarities that suppress the uniform-LUFS invariant. ``PREFER`` and
#: ``REQUIRE`` both say "this block SHOULD be off-target" → suppress.
#: ``AVOID`` / ``FORBID`` say "this block MUST NOT deviate" → the
#: invariant stays in force (we don't add new checks here; we just
#: don't suppress).
_SUPPRESSING_POLARITY_VALUES: frozenset = frozenset({"prefer", "require"})


# ---------------------------------------------------------------------------
# Low-level helpers — still exported for backward-compat unit tests
# ---------------------------------------------------------------------------


def _list_preferences_safe(state: Mapping[str, Any]) -> list:
    """Return the ledger's records or an empty list if the ledger
    module is unavailable.

    Fail-loud on *bad* data: if the ledger module IS available but
    the stored data is malformed, we re-raise so the pipeline stops
    with a clear error. Only a *missing* ledger module silently
    returns empty (the expected state before #131 / the preference
    ledger ships).
    """
    try:
        from callbacks.preference_ledger import list_preferences  # type: ignore
    except ImportError:
        return []
    try:
        return list(list_preferences(state))
    except (ValueError, TypeError):
        raise


def _record_scope_matches_block(record: Any, block: NarrationBlock) -> bool:
    """Return True iff ``record``'s scope covers ``block``.

    This is the low-level (non-virtual-brief) scope matcher retained
    as a fallback when the virtual-brief assembler is unavailable
    (e.g. for unit tests that synthesise fake records without the
    full ledger machinery).
    """
    scope = getattr(record, "scope", None)
    scope_value = getattr(scope, "value", scope)
    scope_ref = getattr(record, "scope_ref", None) or ""

    if scope_value == "global":
        return True
    if scope_value == "stage":
        return scope_ref in ("", "audio")
    if scope_value == "scene":
        return scope_ref == str(block.scene_num)
    if scope_value == "voice_block":
        return scope_ref in {block.block_id, block.voice_role}
    if scope_value == "element":
        return scope_ref == block.block_id
    # artifact_type: deliberately not honoured at this layer.
    return False


def _record_is_lufs_directive(record: Any) -> bool:
    """Return True iff ``record`` looks like a loudness directive.

    Checks (in order): Subject must be ``voice``; then either the
    structured ``metadata["aspect"] == "loudness"`` flag, or the
    content contains a loudness keyword.
    """
    subject = getattr(record, "subject", None)
    subject_value = getattr(subject, "value", subject)
    if subject_value != "voice":
        return False

    metadata = getattr(record, "metadata", None)
    if isinstance(metadata, Mapping):
        aspect = metadata.get(LOUDNESS_ASPECT_KEY)
        if isinstance(aspect, str) and aspect.strip().lower() == LOUDNESS_ASPECT_VALUE:
            return True

    content = (getattr(record, "content", "") or "").lower()
    return any(keyword in content for keyword in _LUFS_CONTENT_KEYWORDS)


def _record_polarity_suppresses(record: Any) -> bool:
    """Return True iff ``record``'s polarity suppresses the invariant."""
    polarity = getattr(record, "polarity", None)
    polarity_value = getattr(polarity, "value", polarity)
    if not isinstance(polarity_value, str):
        return False
    return polarity_value.lower() in _SUPPRESSING_POLARITY_VALUES


# ---------------------------------------------------------------------------
# Virtual-brief-driven scope resolution (ARCH-A4 path)
# ---------------------------------------------------------------------------


def _virtual_brief_imports():
    """Return (assemble_virtual_brief, Scope, Subject) or None if the
    ARCH-A4 module is unavailable.

    Import-time failures are swallowed to preserve the pre-#134
    world's behaviour (the simple record-walk fallback below still
    works against a bare ledger module).
    """
    try:
        from callbacks.virtual_brief import assemble_virtual_brief  # type: ignore
        from callbacks.preference_ledger import Scope, Subject  # type: ignore
    except ImportError:
        return None
    return assemble_virtual_brief, Scope, Subject


#: Scope specificity ranks (keep in sync with
#: :data:`server.callbacks.virtual_brief._SPECIFICITY`). Used below so
#: :func:`_check_scope_override` can detect when a returned decision
#: comes from a strictly broader scope than the one we queried at.
_SCOPE_SPECIFICITY: dict[str, int] = {
    "global": 0,
    "stage": 1,
    "scene": 2,
    "voice_block": 3,
    "artifact_type": 4,
    "element": 5,
}


def _scope_rank(scope: Any) -> int:
    """Return the specificity rank for ``scope`` (enum or string).

    Unknown scopes fall back to ``0`` (GLOBAL) so we never raise from
    inside an invariant-checker hot path.
    """
    scope_value = getattr(scope, "value", scope)
    if not isinstance(scope_value, str):
        return 0
    return _SCOPE_SPECIFICITY.get(scope_value.lower(), 0)


def _check_scope_override(
    assemble_fn,
    state: Mapping[str, Any],
    scope_enum,
    scope_ref: Optional[str],
    subject_enum,
) -> Optional[bool]:
    """Ask the virtual brief whether a loudness override applies at
    a given ``(scope, scope_ref)``.

    Returns:
        * ``True`` — a suppressing loudness record at this scope (or
          the final ``scope_enum is None`` STAGE/GLOBAL step) wins.
        * ``False`` — a record at this scope wins but does not
          suppress (AVOID/FORBID, or a non-loudness VOICE decision).
        * ``None`` — no record at this scope drove the decision;
          either nothing applied at all or the winner came from a
          strictly broader scope than the one we asked about. The
          caller continues walking; a narrower record at a later
          (broader) step can still override the broader winner via
          dominance, and broader-only decisions are picked up at the
          final ``scope_enum is None`` step.

    Raises ``RuntimeError`` when the virtual brief surfaces a hard
    conflict on the VOICE subject — the invariant checker must
    NEVER silently pick a side of a review contradiction.
    """
    kwargs: dict[str, Any] = {
        "stage": "audio",
        "subject": subject_enum.VOICE,
    }
    if scope_enum is not None:
        kwargs["scope"] = scope_enum
        if scope_ref is not None:
            kwargs["scope_ref"] = scope_ref

    brief = assemble_fn(state, **kwargs)

    # Hard conflict on VOICE at any applicable scope → fail loud.
    for conflict in brief.hard_conflicts:
        conflict_subject = getattr(conflict, "subject", None)
        conflict_subject_value = getattr(
            conflict_subject, "value", conflict_subject,
        )
        if conflict_subject_value == subject_enum.VOICE.value:
            raise RuntimeError(
                "ledger_override: HARD CONFLICT on VOICE subject at "
                f"scope={getattr(conflict, 'scope', '?')!r} "
                f"scope_ref={getattr(conflict, 'scope_ref', None)!r} — "
                "cannot decide LUFS override without human re-escalation "
                f"(records: {getattr(conflict, 'records', ())!r})"
            )

    decision = brief.decision_for(subject_enum.VOICE)
    if decision is None:
        return None

    # Scope-specificity guard. ``assemble_virtual_brief`` with
    # ``scope=VOICE_BLOCK`` / ``scope=SCENE`` still pulls in broader
    # records (GLOBAL, unrefed STAGE, unrefed SCENE…), so a STAGE
    # PREFER leaks into the VOICE_BLOCK / SCENE query and wins there
    # even when a more-specific ref'd SCENE record exists (the
    # ref'd-at-broader-level record is excluded from the narrower
    # query by :func:`virtual_brief._record_applies`, line 227).
    # Skip broader-than-queried winners and let a later narrower
    # query in the walk surface the ref'd record — or, if none
    # exists, let the final ``scope_enum is None`` step pick the
    # broad record up authoritatively.
    if scope_enum is not None:
        queried_rank = _scope_rank(scope_enum)
        decision_rank = _scope_rank(decision.record.scope)
        if decision_rank < queried_rank:
            return None

    # Only polarity=PREFER/REQUIRE suppress the invariant. AVOID/FORBID
    # on VOICE do NOT suppress (they say "keep voice uniform").
    if not _record_polarity_suppresses(decision.record):
        return False
    if _record_is_lufs_directive(decision.record):
        logger.info(
            "LUFS override ACTIVE via virtual brief: "
            "scope=%s scope_ref=%s polarity=%s content=%r",
            getattr(decision.record.scope, "value", "?"),
            decision.record.scope_ref,
            getattr(decision.record.polarity, "value", "?"),
            (decision.content or "")[:80],
        )
        return True
    return False


def _is_lufs_override_active_via_virtual_brief(
    state: Mapping[str, Any],
    block: NarrationBlock,
) -> Optional[bool]:
    """Virtual-brief-driven scope walk. Returns ``True`` / ``False`` /
    ``None``.

    ``None`` means the virtual brief assembler is unavailable and the
    caller should fall back to the record-walk path. Any other
    returned value is authoritative.
    """
    imports = _virtual_brief_imports()
    if imports is None:
        return None
    assemble_fn, Scope, Subject = imports

    # Narrow → broad scope walk. More specific scopes win, per ARCH-A4
    # specificity order. We stop at the first scope that returns a
    # definite answer (True *or* False). A narrow PREFER/REQUIRE wins;
    # a narrow AVOID/FORBID stops the walk (the reviewer deliberately
    # disabled an override at that scope even if a broader PREFER
    # would otherwise apply).
    scope_chain: list[tuple[Any, Optional[str]]] = []
    if block.block_id:
        scope_chain.append((Scope.VOICE_BLOCK, block.block_id))
    if block.voice_role and block.voice_role != block.block_id:
        scope_chain.append((Scope.VOICE_BLOCK, block.voice_role))
    if block.scene_num:
        scope_chain.append((Scope.SCENE, str(block.scene_num)))
    # STAGE (audio) — matched via the stage=audio request param.
    scope_chain.append((None, None))

    for scope_enum, scope_ref in scope_chain:
        result = _check_scope_override(
            assemble_fn, state, scope_enum, scope_ref, Subject,
        )
        if result is not None:
            return result
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_lufs_override_active(
    state: Mapping[str, Any],
    block: NarrationBlock,
) -> bool:
    """Return True iff a ledger record suppresses uniform-LUFS for this block.

    Consults the ARCH-A4 virtual-brief assembler at the narrowest
    applicable scope first; falls back to the pre-A4 direct
    record-walk when the virtual-brief module is unavailable. See
    the module docstring for full semantics.
    """
    vb_result = _is_lufs_override_active_via_virtual_brief(state, block)
    if vb_result is not None:
        return vb_result

    # Pre-#134 fallback: direct record walk.
    records = _list_preferences_safe(state)
    for record in records:
        if not _record_is_lufs_directive(record):
            continue
        if not _record_polarity_suppresses(record):
            continue
        if _record_scope_matches_block(record, block):
            logger.info(
                "LUFS override ACTIVE via record walk: block=%s "
                "scope=%s scope_ref=%s polarity=%s content=%r",
                block.block_id,
                getattr(getattr(record, "scope", None), "value", "?"),
                getattr(record, "scope_ref", ""),
                getattr(getattr(record, "polarity", None), "value", "?"),
                (getattr(record, "content", "") or "")[:80],
            )
            return True
    return False


def build_lufs_override_resolver(
    state: Mapping[str, Any],
) -> Callable[[NarrationBlock], bool]:
    """Bind :func:`is_lufs_override_active` to ``state``.

    Returned callable takes a :class:`NarrationBlock` and returns a
    bool. The composing agent (:mod:`server.critique.stylistic_qa_agent`)
    calls this once per stage run and hands the resolver to
    :func:`server.critique.audio_invariants.run_all_invariants` as the
    ``override_resolver`` parameter.
    """
    def _resolver(block: NarrationBlock) -> bool:
        return is_lufs_override_active(state, block)
    return _resolver
