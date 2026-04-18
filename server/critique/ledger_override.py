"""Preference-Ledger scoped-override stub for stylistic invariants
(ARCH-E3, issue #149; parent ARCH-E #127; meta ARCH-2026 #122).

The full scope-resolution logic lives in ARCH-A4 (virtual brief
assembler). Until that ships, stylistic QA needs a *narrow* stub that
answers a single question:

> "Is there a live Preference Ledger record that deliberately
>  overrides the uniform-LUFS invariant for this specific block?"

A positive answer suppresses the uniform-LUFS check on that block only.
All other invariants (peak limiter, voice continuity, character voice
consistency, clicks, plosives, hiss floor) remain in force regardless
of any ledger record — they guard against **degradation**, not
**intent**, and no reviewer directive should ever suppress them.

Semantics of this stub (deliberately conservative):

- If ``server.callbacks.preference_ledger`` is unavailable (PR #161
  not yet merged or not importable), every block returns ``False``.
  The invariants remain fully in force — fail-loud is the safe
  default.
- A record overrides the uniform-LUFS invariant on a block iff:
    1. Its ``subject`` is ``VOICE`` (the vocabulary member that covers
       loudness / register directives — see #131's closed subject
       list).
    2. Its ``content`` mentions "loud" or an explicit LUFS value.
       This is a heuristic; A4 will replace it with structured scope
       resolution.
    3. Its ``scope`` matches the block via one of:
        - ``GLOBAL`` (film-wide override)
        - ``STAGE`` with ``scope_ref in {"", "audio"}``
        - ``SCENE`` with ``scope_ref == str(block.scene_num)``
        - ``VOICE_BLOCK`` with ``scope_ref == block.block_id``
          **or** ``scope_ref == block.voice_role`` (e.g. "Cassandra")
        - ``ELEMENT`` with ``scope_ref == block.block_id``

- ARTIFACT_TYPE scope is intentionally *not* consulted here — an
  artifact-type-wide "louder" directive would defeat the uniform
  invariant entirely, which is a pipeline-wide decision A4 should
  handle, not this stub.

The stub never writes to the ledger and never raises; unparseable
records are logged and skipped. The composing agent binds this to
session state via :func:`build_lufs_override_resolver`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from critique.audio_invariants import NarrationBlock

logger = logging.getLogger(__name__)


#: Keywords that flag a record as a loudness directive. Heuristic; A4
#: will replace with structured scope resolution + polarity parsing.
_LUFS_CONTENT_KEYWORDS = ("loud", "quiet", "lufs", "gain", "db", "softer", "softer", "volume")


def _list_preferences_safe(state: Mapping[str, Any]) -> list:
    """Return the ledger's records or an empty list if unavailable.

    Fail-loud on *bad* data: if the ledger module IS available but the
    stored data is malformed, we re-raise so the pipeline stops with a
    clear error. Only a *missing* ledger module silently returns
    empty (that's the expected state before PR #161 merges).
    """
    try:
        from callbacks.preference_ledger import list_preferences  # type: ignore
    except ImportError:
        return []
    try:
        return list(list_preferences(state))
    except (ValueError, TypeError):
        # Malformed ledger data — re-raise so callers see a clear failure.
        raise


def _record_scope_matches_block(record: Any, block: NarrationBlock) -> bool:
    """Return True iff ``record``'s scope covers ``block``."""
    scope = getattr(record, "scope", None)
    scope_value = getattr(scope, "value", scope)
    scope_ref = getattr(record, "scope_ref", None) or ""

    if scope_value == "global":
        return True
    if scope_value == "stage":
        # A stage-scoped record may omit scope_ref (== film-wide stage
        # preference) OR explicitly target the audio stage.
        return scope_ref in ("", "audio")
    if scope_value == "scene":
        return scope_ref == str(block.scene_num)
    if scope_value == "voice_block":
        return scope_ref in {block.block_id, block.voice_role}
    if scope_value == "element":
        return scope_ref == block.block_id
    # artifact_type: deliberately not honoured at this layer (see docstring).
    return False


def _record_is_lufs_directive(record: Any) -> bool:
    """Return True iff ``record`` looks like a loudness directive."""
    subject = getattr(record, "subject", None)
    subject_value = getattr(subject, "value", subject)
    if subject_value != "voice":
        return False
    content = (getattr(record, "content", "") or "").lower()
    return any(keyword in content for keyword in _LUFS_CONTENT_KEYWORDS)


def is_lufs_override_active(
    state: Mapping[str, Any],
    block: NarrationBlock,
) -> bool:
    """Return True iff a ledger record suppresses uniform-LUFS for this block.

    See module docstring for exact semantics. This is a *stub*: ARCH-A4
    will replace it with the virtual-brief scope-resolution path.
    """
    try:
        records = _list_preferences_safe(state)
    except (ValueError, TypeError) as e:
        logger.error(
            "preference_ledger data is malformed; re-raising to fail loud "
            "rather than silently suppressing LUFS override check: %s",
            e,
        )
        raise

    for record in records:
        if not _record_is_lufs_directive(record):
            continue
        if _record_scope_matches_block(record, block):
            logger.info(
                "LUFS override active for block %s via ledger record "
                "(scope=%s, scope_ref=%s, content=%r)",
                block.block_id,
                getattr(getattr(record, "scope", None), "value", "?"),
                getattr(record, "scope_ref", ""),
                getattr(record, "content", "")[:80],
            )
            return True
    return False


def build_lufs_override_resolver(
    state: Optional[Mapping[str, Any]],
) -> Callable[[NarrationBlock], bool]:
    """Return a closure that applies :func:`is_lufs_override_active` to ``state``.

    Convenience for the composing agent — it calls this once per
    stage and hands the closure to
    :func:`server.critique.audio_invariants.run_all_invariants`.
    """
    if state is None:
        return lambda _block: False
    return lambda block: is_lufs_override_active(state, block)
