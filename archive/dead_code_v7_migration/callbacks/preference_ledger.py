"""
Preference Ledger -- append-only storage substrate for scoped user intent
(ARCH-A1, issue #131; parent ARCH-A #123; meta ARCH-2026 #122).

Every user directive -- the original brief (R0), every L4 reviewer edit, every
supervisor constraint -- is recorded here as a scoped, polarised, subject-typed
preference record. Downstream stages assemble a *virtual brief* by querying
this ledger (see ARCH-A4, future ticket). This module is the storage
substrate only: record schema, append API, and a simple query-by-scope API.

Design invariants (enforced by tests in ``tests/test_preference_ledger.py``):

1. **Append-only.** Records are immutable once written. There is no delete and
   no in-place mutation API. Callers receive frozen dataclass instances.
2. **Monotonic revision.** Each record carries a strictly increasing
   ``revision`` integer scoped to the ledger instance in session state.
   The current revision is the max revision across all records (0 when empty).
3. **Blackboard-only access.** The ledger lives in ADK session state under
   :data:`PREFERENCE_LEDGER_KEY`. Cross-stage reads go through this key --
   never through direct imports between stages. This mirrors the
   ``output_key`` blackboard pattern used by
   :mod:`server.callbacks.state_manager` and
   :mod:`server.callbacks.timeline_guardian`.
4. **Fail loud.** Invalid scopes, polarities, subjects, missing origin fields,
   or malformed existing state all raise ``ValueError`` / ``TypeError``.
   There is no silent degradation.
5. **Closed vocabularies.** ``scope``, ``polarity``, and ``subject`` are
   string enums with fixed membership matching issue #131 exactly. New
   members require a follow-up PR.

This module is intentionally narrow: the Preference Interpreter (A2), the
virtual brief assembler (A4), the consistency checker (A5), and the impact
analyzer (A6) are separate tickets and must not be built here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State key
# ---------------------------------------------------------------------------

#: Blackboard key under which the Preference Ledger is stored in session state.
#: The value is a JSON-encoded list of record dicts (matching the JSON-string
#: convention used by ``build_pipeline_state`` for ``scenes`` and
#: ``otio_mutations``).
PREFERENCE_LEDGER_KEY = "preference_ledger"


# ---------------------------------------------------------------------------
# Closed vocabularies (per issue #131)
# ---------------------------------------------------------------------------


class Scope(str, Enum):
    """Hierarchical scope of a preference, from broadest to narrowest.

    Specificity sort order (used by A4 virtual brief assembler, not here) is
    the declaration order below: ``GLOBAL`` < ``STAGE`` < ``SCENE`` <
    ``VOICE_BLOCK`` < ``ARTIFACT_TYPE`` < ``ELEMENT``.
    """

    GLOBAL = "global"
    STAGE = "stage"
    SCENE = "scene"
    VOICE_BLOCK = "voice_block"
    ARTIFACT_TYPE = "artifact_type"
    ELEMENT = "element"


class Polarity(str, Enum):
    """Force with which a preference should be honoured.

    ``PREFER`` / ``AVOID`` are soft; ``REQUIRE`` / ``FORBID`` are hard.
    Hard-polarity dominance is resolved by A4, not here.
    """

    PREFER = "prefer"
    AVOID = "avoid"
    REQUIRE = "require"
    FORBID = "forbid"


class Subject(str, Enum):
    """Closed vocabulary of preference subjects."""

    TONE = "tone"
    VOICE = "voice"
    PACING = "pacing"
    VISUAL_STYLE = "visual_style"
    NARRATIVE_STRUCTURE = "narrative_structure"
    SPEAKER_ROLE = "speaker_role"
    DURATION = "duration"
    MUSIC = "music"


# ---------------------------------------------------------------------------
# Record schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Origin:
    """Provenance of a preference record.

    Every record must be traceable to an L4 reviewer event. During a run only
    L4 events may append, so ``l4_event_id`` is required. The run-start seed
    (R0, ARCH-A3) uses ``l4_event_id="R0"`` and ``reviewer="system"`` to mark
    records parsed from the original brief.
    """

    l4_event_id: str
    reviewer: str
    timestamp: str  # ISO-8601 string, kept as-is (not parsed) for lossless round-trip.

    def __post_init__(self) -> None:
        for field_name in ("l4_event_id", "reviewer", "timestamp"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"Origin.{field_name} must be a non-empty string, "
                    f"got {value!r}"
                )


@dataclass(frozen=True)
class PreferenceRecord:
    """A single append-only preference entry.

    ``scope_ref`` optionally identifies the specific scope target (e.g. a
    scene id for ``Scope.SCENE``, a voice-block id for ``Scope.VOICE_BLOCK``).
    For ``Scope.GLOBAL`` it must be ``None``. For other scopes it is optional
    at this layer -- A4 will decide whether a missing ``scope_ref`` matches
    all instances of that scope or is a drift signal.
    """

    scope: Scope
    polarity: Polarity
    subject: Subject
    content: str
    origin: Origin
    revision: int
    scope_ref: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise TypeError(f"scope must be Scope, got {type(self.scope).__name__}")
        if not isinstance(self.polarity, Polarity):
            raise TypeError(
                f"polarity must be Polarity, got {type(self.polarity).__name__}"
            )
        if not isinstance(self.subject, Subject):
            raise TypeError(
                f"subject must be Subject, got {type(self.subject).__name__}"
            )
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError(
                f"content must be a non-empty string, got {self.content!r}"
            )
        if not isinstance(self.origin, Origin):
            raise TypeError(
                f"origin must be Origin, got {type(self.origin).__name__}"
            )
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise TypeError(
                f"revision must be int, got {type(self.revision).__name__}"
            )
        if self.revision < 1:
            raise ValueError(
                f"revision must be >= 1 (1-indexed, monotonically increasing), "
                f"got {self.revision}"
            )
        if self.scope is Scope.GLOBAL and self.scope_ref is not None:
            raise ValueError(
                "scope_ref must be None for Scope.GLOBAL "
                f"(got {self.scope_ref!r})"
            )
        if self.scope_ref is not None and not isinstance(self.scope_ref, str):
            raise TypeError(
                f"scope_ref must be str or None, got {type(self.scope_ref).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-friendly dict."""
        return {
            "scope": self.scope.value,
            "scope_ref": self.scope_ref,
            "polarity": self.polarity.value,
            "subject": self.subject.value,
            "content": self.content,
            "origin": asdict(self.origin),
            "revision": self.revision,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PreferenceRecord":
        """Deserialise from a dict produced by :meth:`to_dict`.

        Raises ``ValueError`` / ``TypeError`` on any malformed input --
        never silently drops fields or coerces unknown enum members.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"PreferenceRecord.from_dict expects a mapping, "
                f"got {type(data).__name__}"
            )
        missing = {"scope", "polarity", "subject", "content", "origin", "revision"} - set(data)
        if missing:
            raise ValueError(
                f"PreferenceRecord is missing required fields: {sorted(missing)}"
            )
        try:
            scope = Scope(data["scope"])
        except ValueError as exc:
            raise ValueError(f"unknown scope: {data['scope']!r}") from exc
        try:
            polarity = Polarity(data["polarity"])
        except ValueError as exc:
            raise ValueError(f"unknown polarity: {data['polarity']!r}") from exc
        try:
            subject = Subject(data["subject"])
        except ValueError as exc:
            raise ValueError(f"unknown subject: {data['subject']!r}") from exc

        origin_raw = data["origin"]
        if not isinstance(origin_raw, Mapping):
            raise TypeError(
                f"origin must be a mapping, got {type(origin_raw).__name__}"
            )
        origin_missing = {"l4_event_id", "reviewer", "timestamp"} - set(origin_raw)
        if origin_missing:
            raise ValueError(
                f"origin is missing required fields: {sorted(origin_missing)}"
            )
        origin = Origin(
            l4_event_id=origin_raw["l4_event_id"],
            reviewer=origin_raw["reviewer"],
            timestamp=origin_raw["timestamp"],
        )

        metadata_raw = data.get("metadata", {})
        if not isinstance(metadata_raw, Mapping):
            raise TypeError(
                f"metadata must be a mapping, got {type(metadata_raw).__name__}"
            )

        return cls(
            scope=scope,
            scope_ref=data.get("scope_ref"),
            polarity=polarity,
            subject=subject,
            content=data["content"],
            origin=origin,
            revision=data["revision"],
            metadata=dict(metadata_raw),
        )


# ---------------------------------------------------------------------------
# Storage helpers -- low-level, operate on a JSON-encoded list of dicts.
# ---------------------------------------------------------------------------


def _load_raw(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the raw list of record dicts currently stored in ``state``.

    Accepts both JSON-string storage (the blackboard convention) and already
    decoded ``list`` storage (convenient for unit tests). An absent key is
    treated as an empty ledger.
    """
    raw = state.get(PREFERENCE_LEDGER_KEY)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{PREFERENCE_LEDGER_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError(
                f"{PREFERENCE_LEDGER_KEY!r} must decode to a list, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{PREFERENCE_LEDGER_KEY!r} must be a JSON string or list, "
        f"got {type(raw).__name__}"
    )


def _store_raw(state: MutableMapping[str, Any], records: list[dict[str, Any]]) -> None:
    """Serialise ``records`` back into ``state`` as a JSON string."""
    state[PREFERENCE_LEDGER_KEY] = json.dumps(records, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_revision(state: Mapping[str, Any]) -> int:
    """Return the highest revision currently in the ledger, or 0 if empty.

    Useful for consistency-check tagging (ARCH-B1, future ticket). Does not
    mutate state.
    """
    raw = _load_raw(state)
    if not raw:
        return 0
    revisions: list[int] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or "revision" not in entry:
            raise ValueError(
                "preference_ledger contains an entry without a revision field: "
                f"{entry!r}"
            )
        rev = entry["revision"]
        if not isinstance(rev, int) or isinstance(rev, bool):
            raise TypeError(
                f"preference_ledger entry has non-int revision: {rev!r}"
            )
        revisions.append(rev)
    return max(revisions)


def list_preferences(state: Mapping[str, Any]) -> list[PreferenceRecord]:
    """Return all ledger entries in insertion order, as typed records.

    Raises ``ValueError`` / ``TypeError`` if the stored ledger is malformed
    -- no silent degradation.
    """
    return [PreferenceRecord.from_dict(entry) for entry in _load_raw(state)]


def append_preference(
    state: MutableMapping[str, Any],
    *,
    scope: Scope | str,
    polarity: Polarity | str,
    subject: Subject | str,
    content: str,
    origin: Origin | Mapping[str, Any],
    scope_ref: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> PreferenceRecord:
    """Append a new preference record to the ledger in ``state``.

    The record's ``revision`` is assigned automatically as
    ``current_revision(state) + 1``. Existing entries are never mutated.

    Args:
        state: ADK session state (or any mutable mapping) the ledger lives in.
        scope: Scope enum value, or its string form (e.g. ``"scene"``).
        polarity: Polarity enum value, or its string form.
        subject: Subject enum value, or its string form.
        content: Free-form natural-language directive (non-empty).
        origin: Either an :class:`Origin` instance or a mapping with
            ``l4_event_id`` / ``reviewer`` / ``timestamp`` keys.
        scope_ref: Optional string identifying the scope target (e.g. a scene
            id). Must be ``None`` for :attr:`Scope.GLOBAL`.
        metadata: Optional extra metadata mapping (stored verbatim; not used
            for scope matching at this layer).

    Returns:
        The newly created :class:`PreferenceRecord`.

    Raises:
        ValueError / TypeError: On any invalid field (unknown enum member,
            empty content, malformed origin, etc.). Fail-loud by design.
    """
    scope_enum = scope if isinstance(scope, Scope) else Scope(scope)
    polarity_enum = polarity if isinstance(polarity, Polarity) else Polarity(polarity)
    subject_enum = subject if isinstance(subject, Subject) else Subject(subject)

    if isinstance(origin, Origin):
        origin_obj = origin
    elif isinstance(origin, Mapping):
        origin_missing = {"l4_event_id", "reviewer", "timestamp"} - set(origin)
        if origin_missing:
            raise ValueError(
                f"origin mapping is missing required fields: {sorted(origin_missing)}"
            )
        origin_obj = Origin(
            l4_event_id=origin["l4_event_id"],
            reviewer=origin["reviewer"],
            timestamp=origin["timestamp"],
        )
    else:
        raise TypeError(
            f"origin must be Origin or mapping, got {type(origin).__name__}"
        )

    raw = _load_raw(state)
    next_revision = (max((entry["revision"] for entry in raw), default=0) + 1) if raw else 1

    record = PreferenceRecord(
        scope=scope_enum,
        scope_ref=scope_ref,
        polarity=polarity_enum,
        subject=subject_enum,
        content=content,
        origin=origin_obj,
        revision=next_revision,
        metadata=dict(metadata) if metadata else {},
    )

    raw.append(record.to_dict())
    _store_raw(state, raw)
    logger.info(
        "preference_ledger append rev=%d scope=%s polarity=%s subject=%s origin=%s",
        record.revision,
        record.scope.value,
        record.polarity.value,
        record.subject.value,
        record.origin.l4_event_id,
    )
    # ARCH-H5 (issue #160): ledger_change digest.  Fire-and-forget --
    # never let the digest bus block the A1 append path.
    try:
        from dashboard.reasoning_digest import emit_digest

        emit_digest(
            state,
            "ledger_change",
            {
                "revision": record.revision,
                "scope": record.scope.value,
                "scope_ref": record.scope_ref or "",
                "polarity": record.polarity.value,
                "subject": record.subject.value,
                "content": record.content,
                "origin": record.origin.l4_event_id,
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("reasoning_digest ledger_change emission failed: %s", exc)
    return record


def query_by_scope(
    state: Mapping[str, Any],
    scope: Scope | str,
    *,
    scope_ref: Optional[str] = None,
) -> list[PreferenceRecord]:
    """Return ledger entries whose scope matches ``scope`` (and ``scope_ref``).

    This is the minimal query surface promised to the A4 virtual brief
    assembler. A4 will layer specificity sort, recency sort, and
    hard-polarity dominance on top of this result. Here we only do exact
    scope matching:

    * ``scope`` is matched by enum equality.
    * If ``scope_ref`` is provided, records whose ``scope_ref`` differs are
      excluded. If ``scope_ref`` is ``None``, all records with the given
      scope are returned regardless of their ``scope_ref``.

    Results are returned in insertion (i.e. revision-ascending) order. The
    ledger itself is not mutated.
    """
    scope_enum = scope if isinstance(scope, Scope) else Scope(scope)
    out: list[PreferenceRecord] = []
    for record in list_preferences(state):
        if record.scope is not scope_enum:
            continue
        if scope_ref is not None and record.scope_ref != scope_ref:
            continue
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Slot-oriented scope resolution (UI-04b / issue #202)
# ---------------------------------------------------------------------------


#: Mapping from canonical OTIO track name -> set of stage names the slot is
#: "owned by". Stage-scoped ledger records whose ``scope_ref`` matches any of
#: these names are considered applicable to the slot. Records carrying no
#: ``scope_ref`` apply to every stage (same convention as virtual_brief).
_TRACK_STAGES: dict[str, frozenset[str]] = {
    "V1_Video": frozenset({"video", "visual_direction", "production"}),
    "A1_Narration": frozenset({"audio", "scenario", "narration"}),
    "A2_Music": frozenset({"audio", "music"}),
}


#: Mapping from canonical OTIO track name -> the artifact type string used by
#: ARTIFACT_TYPE-scoped ledger records.
_TRACK_ARTIFACT_TYPES: dict[str, str] = {
    "V1_Video": "video_clip",
    "A1_Narration": "narration",
    "A2_Music": "music",
}


def _parse_slot(slot_id: str) -> tuple[str, int, int]:
    """Parse ``{short}:{scene}:{phrase}`` into a canonical track + ints.

    Kept inline (not imported from otio_timeline_model) to avoid a module
    cycle — preference_ledger is a lower layer than otio_timeline_model.
    """
    parts = slot_id.split(":")
    if len(parts) != 3:
        raise ValueError(f"malformed slot id: {slot_id!r}")
    short, scene_s, phrase_s = parts
    try:
        scene = int(scene_s)
        phrase = int(phrase_s)
    except ValueError as exc:
        raise ValueError(f"malformed slot id: {slot_id!r}") from exc
    long_track = {
        "V1": "V1_Video",
        "A1": "A1_Narration",
        "A2": "A2_Music",
    }.get(short)
    if long_track is None:
        raise ValueError(f"unknown track {short!r} in slot id {slot_id!r}")
    return long_track, scene, phrase


def _scene_ref_matches(ref: Optional[str], scene_num: int) -> bool:
    """True if a SCENE-scope record applies to the given scene.

    Conventions observed across the pipeline:

    * ``str(scene_num)`` (e.g. ``"1"``),
    * ``"scene-{n}"``, ``"scene_{n}"``, ``"scene-{n:03d}"``, ``"scene_{n:03d}"``,
    * ``None`` (applies to every scene — same convention as virtual_brief).
    """
    if ref is None:
        return True
    candidates = {
        str(scene_num),
        f"scene-{scene_num}",
        f"scene_{scene_num}",
        f"scene-{scene_num:03d}",
        f"scene_{scene_num:03d}",
    }
    return ref in candidates


def _element_ref_matches(
    ref: Optional[str], slot_id: str, scene_num: int, phrase_idx: int
) -> bool:
    """True if an ELEMENT-scope record applies to the given slot.

    Accepts the canonical ``{short}:{scene}:{phrase}`` form as well as the
    ``scene_{s}_phrase_{p}`` / ``s{s}_p{p}`` conventions used by the
    critique store. ``ref=None`` applies to any element.
    """
    if ref is None:
        return True
    candidates = {
        slot_id,
        f"{scene_num}:{phrase_idx}",
        f"{scene_num}_{phrase_idx}",
        f"scene_{scene_num}_phrase_{phrase_idx}",
        f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}",
        f"s{scene_num}_p{phrase_idx}",
        f"s{scene_num:03d}_p{phrase_idx:03d}",
    }
    return ref in candidates


def records_applying_to_slot(
    state: Mapping[str, Any],
    slot_id: str,
    *,
    voice_block_ref: Optional[str] = None,
) -> list[PreferenceRecord]:
    """Return every ledger record that applies to ``slot_id``.

    This is the read-side contract consumed by the slot-detail aggregator
    (UI-04a / issue #201). The rules mirror the virtual brief's
    containment semantics, specialised to the slot:

    * ``GLOBAL`` records always apply.
    * ``STAGE`` records apply when ``scope_ref`` is ``None``, or when it
      matches one of the stages owning the slot's track (see
      :data:`_TRACK_STAGES`).
    * ``SCENE`` records apply when ``scope_ref`` is ``None`` or matches
      the slot's scene number (accepts the common string conventions —
      ``"3"``, ``"scene-3"``, ``"scene_3"``, ``"scene_003"``, ...).
    * ``VOICE_BLOCK`` records apply only to narration slots. They match
      when ``scope_ref`` is ``None`` or equal to ``voice_block_ref`` (the
      caller supplies this from the slot's OTIO metadata; callers that do
      not know the voice block pass ``None`` and only get the unrefed
      records).
    * ``ARTIFACT_TYPE`` records apply when ``scope_ref`` is ``None`` or
      matches the slot's artifact type (``video_clip``, ``narration``, or
      ``music``).
    * ``ELEMENT`` records apply when ``scope_ref`` is ``None`` or matches
      any of the canonical slot-id / scene-phrase forms (see
      :func:`_element_ref_matches`).

    Results are ordered by revision ascending. Records that have been
    superseded by a strictly newer record with the **same** subject,
    scope and scope_ref carry a ``superseded=True`` attribute on the
    returned dataclass copy (the stored record is never mutated).

    Args:
        state: ADK session state containing the Preference Ledger.
        slot_id: Canonical slot id, ``{track_short}:{scene}:{phrase}``.
        voice_block_ref: Optional voice-block identifier, used to match
            ``VOICE_BLOCK``-scoped records with a specific ``scope_ref``.

    Returns:
        List of :class:`PreferenceRecord` instances that apply to the
        slot, in revision-ascending order. Superseded entries carry a
        ``superseded=True`` metadata hint on their returned copies.

    Raises:
        ValueError: On a malformed ``slot_id``. The ledger itself may
            also raise when the stored state is invalid — fail loud.
    """
    track, scene_num, phrase_idx = _parse_slot(slot_id)
    stages = _TRACK_STAGES.get(track, frozenset())
    artifact_type = _TRACK_ARTIFACT_TYPES.get(track)

    applicable: list[PreferenceRecord] = []
    for record in list_preferences(state):
        rec_scope = record.scope
        rec_ref = record.scope_ref

        if rec_scope is Scope.GLOBAL:
            applicable.append(record)
            continue

        if rec_scope is Scope.STAGE:
            if rec_ref is None or rec_ref in stages:
                applicable.append(record)
            continue

        if rec_scope is Scope.SCENE:
            if _scene_ref_matches(rec_ref, scene_num):
                applicable.append(record)
            continue

        if rec_scope is Scope.VOICE_BLOCK:
            # Voice-block records only apply to narration slots.
            if track != "A1_Narration":
                continue
            if rec_ref is None or (
                voice_block_ref is not None and rec_ref == voice_block_ref
            ):
                applicable.append(record)
            continue

        if rec_scope is Scope.ARTIFACT_TYPE:
            if rec_ref is None or (artifact_type and rec_ref == artifact_type):
                applicable.append(record)
            continue

        if rec_scope is Scope.ELEMENT:
            if _element_ref_matches(rec_ref, slot_id, scene_num, phrase_idx):
                applicable.append(record)
            continue

    applicable.sort(key=lambda r: r.revision)

    # Mark superseded records: for every (subject, scope, scope_ref) triple,
    # only the highest-revision entry is "live"; older ones are flagged.
    latest_rev: dict[tuple[Any, Any, Optional[str]], int] = {}
    for record in applicable:
        key = (record.subject, record.scope, record.scope_ref)
        if record.revision > latest_rev.get(key, -1):
            latest_rev[key] = record.revision

    out: list[PreferenceRecord] = []
    for record in applicable:
        key = (record.subject, record.scope, record.scope_ref)
        is_superseded = record.revision < latest_rev[key]
        meta = dict(record.metadata)
        if is_superseded:
            meta["superseded"] = True
        out.append(
            PreferenceRecord(
                scope=record.scope,
                scope_ref=record.scope_ref,
                polarity=record.polarity,
                subject=record.subject,
                content=record.content,
                origin=record.origin,
                revision=record.revision,
                metadata=meta,
            )
        )
    return out


__all__ = [
    "PREFERENCE_LEDGER_KEY",
    "Scope",
    "Polarity",
    "Subject",
    "Origin",
    "PreferenceRecord",
    "append_preference",
    "query_by_scope",
    "records_applying_to_slot",
    "list_preferences",
    "current_revision",
]
