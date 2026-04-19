"""UI-01 — Narrator that turns pipeline events into chat turns.

Parent issue: #186 (UI-01 Narrator in chat).  Children closed by this module:

* #193 (UI-01a event filter) -- :func:`should_promote` decides which
  reasoning-digest + pipeline events deserve a chat turn.
* #194 (UI-01b plain-English formatter) -- :func:`format_turn` renders a
  promoted event as a one-sentence assistant turn with ``[[slot:ID]]`` and
  ``[[preview:BOUND]]`` tokens the frontend (UI-01c) renders as chips.

The narrator sits alongside the existing reasoning-digest writer
(:mod:`dashboard.reasoning_digest`) and the pipeline event bus
(:func:`agui.emit_agui_event`).  Both feed into :func:`emit_narrator_event`,
which filters + formats and pushes a plain-English line onto a bounded
subscriber queue.  The unified ``POST /`` SSE endpoint drains that queue and
emits AG-UI ``TEXT_MESSAGE_*`` events so CopilotKit renders them as normal
assistant chat messages on the same connection -- no new channel.

Design invariants (enforced by ``tests/test_chat_narrator.py``):

1. **Never block the pipeline on narrator I/O.**  Emission is a bounded
   :class:`collections.deque` append; slow subscribers drop oldest events.
2. **Fail-loud on unknown kind.**  :func:`format_turn` raises ``ValueError``
   rather than silently swallowing an unknown narrator-event kind.
3. **Closed vocabulary.**  The set of narrator kinds is exactly
   :data:`NARRATOR_EVENT_KINDS`; adding a kind requires a template + test.
4. **Deduplication.**  Two promoted events within :data:`DEDUP_WINDOW_SEC`
   seconds for the same slot + kind collapse to a single turn.
5. **Tag filtering.**  ``internal`` and ``debug`` tags always suppress
   promotion, even on event kinds that would otherwise promote.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed vocabulary of narrator event kinds (issue #194 shape block).
# ---------------------------------------------------------------------------

#: Narrator-event kinds that always promote to chat (UI-01a hard list).
#:
#: These are *semantic* kinds (a deliberately thinner layer than the raw
#: reasoning-digest vocabulary in :data:`dashboard.reasoning_digest.EVENT_KINDS`):
#: the narrator bridge maps the raw kinds + pipeline events onto these.
NARRATOR_EVENT_KINDS: tuple[str, ...] = (
    "stage_started",
    "stage_completed",
    "approval_gate_opened",
    "take_failed",
    "take_retried",
    "reconciliation_converged",
    "preview_ready",
    "directive_applied",
    "halt_fired",
)

#: Seconds within which two promoted events with equivalent semantics for
#: the same slot collapse into a single chat turn.
DEDUP_WINDOW_SEC: float = 2.0

#: Tags that, when present on an event, *always* suppress promotion.
SUPPRESSING_TAGS: frozenset[str] = frozenset({"internal", "debug"})

#: Maximum number of queued narrator events per subscriber before the oldest
#: is dropped.  The UI is best-effort; we never block the pipeline on a slow
#: consumer.
_QUEUE_MAX = 1024


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class NarratorEvent:
    """A single promoted event ready to be formatted into a chat turn.

    Attributes
    ----------
    kind:
        One of :data:`NARRATOR_EVENT_KINDS`.
    fields:
        Template fields used by :func:`format_turn` (e.g. ``stage``,
        ``slot_id``, ``qa_axis``).  Templates tolerate missing keys and
        substitute a reasonable placeholder.
    timestamp:
        Unix timestamp (``time.time()``) -- used for deduplication and UI
        ordering.  Set automatically when absent.
    tags:
        Free-form tags (e.g. ``internal``, ``debug``) that influence
        promotion; see :data:`SUPPRESSING_TAGS`.
    """

    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()
        if not isinstance(self.tags, frozenset):
            self.tags = frozenset(self.tags)


# ---------------------------------------------------------------------------
# UI-01a: promotion filter
# ---------------------------------------------------------------------------


def should_promote(
    kind: str,
    *,
    tags: Iterable[str] = (),
    promote_to_chat: bool = False,
) -> bool:
    """Return ``True`` iff an event should become a chat turn.

    Rules (UI-01a):

    * ``internal`` / ``debug`` tags always suppress (take precedence over
      every other signal).
    * Event kinds in :data:`NARRATOR_EVENT_KINDS` always promote.
    * Any other event promotes iff ``promote_to_chat=True`` (opt-in flag
      used by reasoning-digest agents for user-salient moments).

    Deduplication is *not* applied here -- that lives in :class:`Narrator`
    because it needs temporal state.
    """
    tag_set = set(tags)
    if tag_set & SUPPRESSING_TAGS:
        return False
    if kind in NARRATOR_EVENT_KINDS:
        return True
    return bool(promote_to_chat)


# ---------------------------------------------------------------------------
# UI-01c: slot-chip token helpers (shared with the frontend parser).
# ---------------------------------------------------------------------------


def slot_token(slot_id: str) -> str:
    """Serialise a slot id into the ``[[slot:ID]]`` chip token.

    The complementary parser lives in
    ``frontend/src/lib/chat-tokens.ts`` and must round-trip the same ids.
    """
    return f"[[slot:{slot_id}]]"


def preview_token(boundary: str) -> str:
    """Serialise a preview boundary into the ``[[preview:BOUND]]`` token."""
    return f"[[preview:{boundary}]]"


# ---------------------------------------------------------------------------
# UI-01b: plain-English templates (one per kind, past/present tense).
# ---------------------------------------------------------------------------

_STAGE_HUMAN_NAMES = {
    "scenario": "scenario",
    "audio": "audio",
    "visual": "visual direction",
    "visual_direction": "visual direction",
    "production": "production",
    "assembly": "assembly",
    "dashboard": "dashboard directive",
}


def _stage_name(raw: Any) -> str:
    """Return a human-readable stage name; fall back to the raw string."""
    if raw is None:
        return "the pipeline"
    name = str(raw).strip()
    if not name:
        return "the pipeline"
    return _STAGE_HUMAN_NAMES.get(name.lower(), name)


def _get(fields: Mapping[str, Any], key: str, default: Any = "") -> Any:
    """Read ``fields[key]`` with a string-coerced default on ``None``."""
    val = fields.get(key, default)
    if val is None:
        return default
    return val


def _format_duration_sec(raw: Any) -> str:
    try:
        return f"{float(raw):.1f}"
    except (TypeError, ValueError):
        return "?"


def _format_duration_whole(raw: Any) -> str:
    try:
        return f"{float(raw):.0f}"
    except (TypeError, ValueError):
        return "?"


def _tpl_stage_started(f: Mapping[str, Any]) -> str:
    return f"Starting {_stage_name(f.get('stage'))}\u2026"


def _tpl_stage_completed(f: Mapping[str, Any]) -> str:
    name = _stage_name(f.get("stage"))
    # Capitalise the leading character without touching embedded acronyms.
    headline = name[:1].upper() + name[1:] if name else name
    return f"{headline} complete."


def _tpl_approval_gate_opened(f: Mapping[str, Any]) -> str:
    return (
        f"{_stage_name(f.get('stage'))} ready \u2014 "
        "approve to proceed, or reject with a note."
    )


def _tpl_take_failed(f: Mapping[str, Any]) -> str:
    slot = _get(f, "slot_id")
    axis = _get(f, "qa_axis", "QA")
    reason = _get(f, "reason", "unspecified")
    chip = slot_token(str(slot)) if slot else "a take"
    return f"{chip} failed {axis} ({reason}) \u2014 retrying."


def _tpl_take_retried(f: Mapping[str, Any]) -> str:
    slot = _get(f, "slot_id")
    n = _get(f, "n", "?")
    change = _get(f, "change", "updated inputs")
    chip = slot_token(str(slot)) if slot else "Take"
    return f"{chip} take {n} retrying with {change}."


def _tpl_reconciliation_converged(f: Mapping[str, Any]) -> str:
    duration = _format_duration_sec(f.get("duration_sec"))
    return f"Narration locked at {duration}s \u2014 within tolerance."


def _tpl_preview_ready(f: Mapping[str, Any]) -> str:
    boundary = _get(f, "boundary") or _get(f, "preview_boundary") or "preview"
    duration = _format_duration_whole(f.get("duration_sec"))
    return f"{preview_token(str(boundary))} ready \u2014 {duration}s."


def _tpl_directive_applied(f: Mapping[str, Any]) -> str:
    directive = _get(f, "directive_text") or _get(f, "directive", "directive")
    n_drifted = _get(f, "n_drifted", 0)
    try:
        n = int(n_drifted)
    except (TypeError, ValueError):
        n = 0
    plural = "slot" if n == 1 else "slots"
    return f"Applied {directive!r}; {n} {plural} will re-run."


def _tpl_halt_fired(f: Mapping[str, Any]) -> str:
    stage = _stage_name(f.get("stage"))
    checkpoint = _get(f, "checkpoint", "unknown")
    return f"Paused at {stage}. Last safe checkpoint was {checkpoint}."


_TEMPLATES = {
    "stage_started": _tpl_stage_started,
    "stage_completed": _tpl_stage_completed,
    "approval_gate_opened": _tpl_approval_gate_opened,
    "take_failed": _tpl_take_failed,
    "take_retried": _tpl_take_retried,
    "reconciliation_converged": _tpl_reconciliation_converged,
    "preview_ready": _tpl_preview_ready,
    "directive_applied": _tpl_directive_applied,
    "halt_fired": _tpl_halt_fired,
}
assert tuple(sorted(_TEMPLATES)) == tuple(sorted(NARRATOR_EVENT_KINDS)), (
    "every narrator kind must have exactly one template"
)


def format_turn(event: NarratorEvent) -> str:
    """Render ``event`` into a single-sentence chat turn.

    The returned string contains ``[[slot:ID]]`` / ``[[preview:BOUND]]``
    tokens (UI-01c) that the frontend parses into clickable chips.

    Raises
    ------
    ValueError
        If ``event.kind`` is not in :data:`NARRATOR_EVENT_KINDS`.
    """
    tpl = _TEMPLATES.get(event.kind)
    if tpl is None:
        raise ValueError(
            f"unknown narrator event kind: {event.kind!r} "
            f"(known: {sorted(NARRATOR_EVENT_KINDS)})"
        )
    return tpl(event.fields)


# ---------------------------------------------------------------------------
# Dedup + subscriber bus
# ---------------------------------------------------------------------------


def _dedup_key(event: NarratorEvent) -> tuple[str, str]:
    """Key used for :data:`DEDUP_WINDOW_SEC` collapsing.

    Two events with equivalent ``(kind, slot_id-or-stage)`` within the
    window are treated as semantic duplicates and only the first is
    published.
    """
    slot = str(event.fields.get("slot_id") or event.fields.get("stage") or "")
    return (event.kind, slot)


class Narrator:
    """Filter + dedup + publish promoted narrator events.

    Instances are cheap; the module-level singleton (:func:`get_narrator`)
    is the canonical publisher but tests may instantiate their own.
    """

    def __init__(self, dedup_window_sec: float = DEDUP_WINDOW_SEC) -> None:
        self._dedup_window_sec = dedup_window_sec
        self._last_emit: dict[tuple[str, str], float] = {}
        self._subscribers: list[collections.deque] = []
        self._lock = threading.Lock()

    def subscribe(self) -> collections.deque:
        """Register a bounded subscriber queue and return it."""
        queue: collections.deque = collections.deque(maxlen=_QUEUE_MAX)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: collections.deque) -> None:
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass

    def emit(
        self,
        kind: str,
        *,
        fields: Optional[Mapping[str, Any]] = None,
        tags: Iterable[str] = (),
        promote_to_chat: bool = False,
        timestamp: Optional[float] = None,
    ) -> Optional[NarratorEvent]:
        """Filter + dedup + publish a narrator event.

        Returns the :class:`NarratorEvent` that was published, or ``None``
        if the event was suppressed (by the filter or dedup window).
        """
        if not should_promote(kind, tags=tags, promote_to_chat=promote_to_chat):
            return None

        event = NarratorEvent(
            kind=kind,
            fields=dict(fields or {}),
            timestamp=timestamp or time.time(),
            tags=frozenset(tags),
        )
        # Unknown kinds with ``promote_to_chat=True`` pass the filter but
        # have no template -- fail loud rather than produce a silent miss.
        if kind not in _TEMPLATES:
            raise ValueError(
                f"narrator event kind {kind!r} has no template "
                f"(known: {sorted(NARRATOR_EVENT_KINDS)})"
            )

        key = _dedup_key(event)
        with self._lock:
            last = self._last_emit.get(key)
            if last is not None and (event.timestamp - last) < self._dedup_window_sec:
                return None
            self._last_emit[key] = event.timestamp
            subs = list(self._subscribers)

        text = format_turn(event)
        payload = {
            "id": uuid.uuid4().hex,
            "kind": event.kind,
            "text": text,
            "timestamp": event.timestamp,
            "fields": dict(event.fields),
            "tags": sorted(event.tags),
        }
        for queue in subs:
            # ``deque.append`` is atomic under the GIL; bounded queue drops
            # oldest if a subscriber is wedged.  Never block the caller.
            queue.append(payload)
        return event


# ---------------------------------------------------------------------------
# Singleton + public facade
# ---------------------------------------------------------------------------


_narrator = Narrator()


def get_narrator() -> Narrator:
    """Return the process-wide :class:`Narrator` singleton."""
    return _narrator


def subscribe_narrator_events() -> collections.deque:
    """Register a subscriber queue on the singleton narrator."""
    return _narrator.subscribe()


def unsubscribe_narrator_events(queue: collections.deque) -> None:
    _narrator.unsubscribe(queue)


def emit_narrator_event(
    kind: str,
    *,
    fields: Optional[Mapping[str, Any]] = None,
    tags: Iterable[str] = (),
    promote_to_chat: bool = False,
) -> Optional[NarratorEvent]:
    """Filter + dedup + publish a narrator event on the singleton.

    This is the canonical call site.  Prefer it over constructing a
    :class:`Narrator` directly unless you need an isolated instance
    (tests).
    """
    return _narrator.emit(
        kind,
        fields=fields,
        tags=tags,
        promote_to_chat=promote_to_chat,
    )


# ---------------------------------------------------------------------------
# Reasoning-digest bridge
# ---------------------------------------------------------------------------
#
# The reasoning-digest writer (ARCH-H5) already covers most pipeline hook
# sites.  Rather than duplicate the hook fan-out, we translate promoted
# digests into narrator events.  Kinds without a digest counterpart
# (``halt_fired``, ``directive_applied``, ``reconciliation_converged``) emit
# narrator events directly from their call sites.

#: Mapping from reasoning-digest event kinds to narrator kinds.
_DIGEST_KIND_TO_NARRATOR: dict[str, str] = {
    "stage_start": "stage_started",
    "stage_end": "stage_completed",
    "gate_open": "approval_gate_opened",
    "preview_built": "preview_ready",
}


def _digest_slot_id(source_event: Mapping[str, Any]) -> Optional[str]:
    """Extract a frontend slot id from a raw digest source event.

    The slot id shape is ``{track}:{scene_num}:{phrase_idx}`` to match
    :mod:`agui`'s ``slot_state`` emissions.  Returns ``None`` when the
    event has no slot context.
    """
    scene = source_event.get("scene_num")
    phrase = source_event.get("phrase_idx", 0)
    if scene is None:
        return None
    artifact_type = str(source_event.get("artifact_type", "")).lower()
    if artifact_type in {"narration", "audio"}:
        track = "A1"
    elif artifact_type in {"video_clip", "video"}:
        track = "V1"
    else:
        track = "V1"
    return f"{track}:{scene}:{phrase}"


def bridge_from_reasoning_digest(
    digest: Mapping[str, Any],
) -> Optional[NarratorEvent]:
    """Translate a reasoning-digest payload into a narrator event.

    Returns the emitted :class:`NarratorEvent`, or ``None`` if the digest
    is suppressed by the promotion filter (tags or unknown kind without
    ``promote_to_chat``).
    """
    kind = str(digest.get("kind", ""))
    source = digest.get("source_event") or {}
    if not isinstance(source, Mapping):
        source = {}
    tags = tuple(source.get("tags") or [])
    promote_flag = bool(source.get("promote_to_chat", False))

    narrator_kind = _DIGEST_KIND_TO_NARRATOR.get(kind)
    if narrator_kind is None:
        # qa_verdict fail + retry map to take_failed / take_retried when
        # the source carries enough context; otherwise drop to avoid
        # chatty spam.
        if kind == "qa_verdict":
            verdict = str(source.get("verdict", "")).lower()
            if verdict not in {"fail", "failed"}:
                return None
            narrator_kind = "take_failed"
        elif kind == "ladder_step":
            action = str(source.get("action", "")).lower()
            if "retry" not in action:
                return None
            narrator_kind = "take_retried"
        else:
            if not promote_flag:
                return None
            # Digests flagged promote_to_chat but without a narrator kind
            # fall through as ``internal`` -- they never render in chat.
            return None

    fields: dict[str, Any] = {}
    slot = _digest_slot_id(source)
    if slot is not None:
        fields["slot_id"] = slot
    for key in (
        "stage",
        "duration_sec",
        "qa_axis",
        "reason",
        "check_name",
        "message",
        "boundary",
        "preview_boundary",
        "n",
        "change",
    ):
        val = source.get(key)
        if val is not None:
            fields[key] = val
    if narrator_kind == "take_failed":
        fields.setdefault("qa_axis", source.get("check_name", "QA"))
        fields.setdefault("reason", source.get("message", "unspecified"))
    return emit_narrator_event(
        narrator_kind,
        fields=fields,
        tags=tags,
        promote_to_chat=promote_flag,
    )


__all__ = [
    "DEDUP_WINDOW_SEC",
    "NARRATOR_EVENT_KINDS",
    "Narrator",
    "NarratorEvent",
    "SUPPRESSING_TAGS",
    "bridge_from_reasoning_digest",
    "emit_narrator_event",
    "format_turn",
    "get_narrator",
    "preview_token",
    "should_promote",
    "slot_token",
    "subscribe_narrator_events",
    "unsubscribe_narrator_events",
]
