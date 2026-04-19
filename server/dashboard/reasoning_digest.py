"""
ARCH-H5 (issue #160) -- Reasoning Digest writer.

Parent: ARCH-H #130. Meta: ARCH-2026 #122. Diagram 10.

Every pipeline channel feeds a unified SSE stream consumable by the dashboard:
artifacts, gates, previews, ledger changes, L0-L3 resolutions, QA verdicts,
infra events, ETA revisions. This module is the *writer*: it takes each raw
pipeline event, produces a plain-english reasoning digest via a deterministic
rule-based summariser (no LLM), appends it to the blackboard log
``reasoning_digest_log``, and emits it onto the SSE channel the dashboard
subscribes to.

Design invariants (enforced by tests in ``tests/test_reasoning_digest.py``):

1. **Deterministic.**  No LLM. ``summarise_event`` is a pure rule table.
2. **Non-blocking.**   ``write_digest`` fire-and-forgets onto the SSE queue.
                       Emission is O(1) and must never block the pipeline.
3. **Truncation rule.** Summaries longer than :data:`MAX_SUMMARY_CHARS` are
                       truncated with an ellipsis character, preserving the
                       informative prefix (stage/scope is always first).
4. **Fail-loud on unknown kind.** An unknown ``event_kind`` raises
                       ``ValueError`` rather than silently swallowing.

Hook points (see parent ARCH-H #130):

* ``stage_start`` / ``stage_end`` -- ADK ``after_agent_callback`` per stage.
* ``gate_open`` / ``gate_close``  -- ``callbacks/approval_gate.py``
                                     (``mark_stage_ready`` / ``approve_stage``).
* ``preview_built``                -- G2 preview trigger.
* ``ledger_change``                -- A1 ``append_preference``.
* ``ladder_step``                  -- ``recovery.py`` L0-L3 resolutions.
* ``qa_verdict``                   -- E3 + A2 critique substrate.
* ``infra_event``                  -- C2 ``infra_ladder.py`` attempts.
* ``eta_revision``                 -- Fleet coordinator ETA emissions.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Blackboard key under which the chronological digest log is stored.
#: Cross-stage callers read/write via ADK session state under this key,
#: matching the ``output_key`` convention used by
#: :mod:`server.callbacks.preference_ledger` and
#: :mod:`server.callbacks.state_manager`.
REASONING_DIGEST_LOG_KEY = "reasoning_digest_log"

#: Closed vocabulary of event kinds the summariser knows how to render.
#: An unknown kind raises ``ValueError`` in :func:`summarise_event` -- new
#: kinds require a matching rule below plus a test.
EVENT_KINDS: tuple[str, ...] = (
    "stage_start",
    "stage_end",
    "gate_open",
    "gate_close",
    "preview_built",
    "ledger_change",
    "ladder_step",
    "qa_verdict",
    "infra_event",
    "eta_revision",
)

#: Maximum characters allowed in a ``summary`` before truncation.
MAX_SUMMARY_CHARS = 200

#: Scope vocabulary. ``scope`` is a coarse taxonomy of *what the digest is
#: about*; it is distinct from ``preference_ledger.Scope`` and intentionally
#: looser (the dashboard uses it for grouping, not for conflict resolution).
SCOPES: tuple[str, ...] = (
    "global",
    "stage",
    "scene",
    "clip",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ReasoningDigest:
    """A single human-readable summary of one raw pipeline event.

    Attributes
    ----------
    timestamp:
        Unix timestamp (``time.time()``) when the digest was produced.
    kind:
        One of :data:`EVENT_KINDS`.
    scope:
        One of :data:`SCOPES`. ``global`` is the default when the event
        has no natural scope (e.g. infra events).
    summary:
        Plain-english 1-2 sentence description, at most
        :data:`MAX_SUMMARY_CHARS` characters (truncated with a trailing
        ellipsis character if needed).
    source_event:
        The raw event dict that produced this digest. Preserved verbatim
        so drill-down consumers can recover the full context without
        re-querying the originating store.
    """

    timestamp: float
    kind: str
    scope: str
    summary: str
    source_event: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "scope": self.scope,
            "summary": self.summary,
            "source_event": dict(self.source_event),
        }


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Truncate ``text`` to ``limit`` chars, preserving the informative prefix.

    For every rule below, the prefix (stage / scope / operation name) is
    placed first, so a prefix-preserving truncation keeps the most useful
    identifier visible on the dashboard even for very long summaries.
    """
    if len(text) <= limit:
        return text
    # Reserve one character for the ellipsis.
    return text[: max(0, limit - 1)].rstrip() + "\u2026"


# ---------------------------------------------------------------------------
# Rule table -- one rule per event kind. Deterministic. No LLM.
# ---------------------------------------------------------------------------


def _str(event: Mapping[str, Any], key: str, default: str = "") -> str:
    """Coerce ``event[key]`` to a short display string; ``default`` on miss."""
    val = event.get(key, default)
    if val is None:
        return default
    return str(val)


def _rule_stage_start(event: Mapping[str, Any]) -> tuple[str, str]:
    stage = _str(event, "stage", "(unknown)")
    return "stage", f"Stage {stage!r} started."


def _rule_stage_end(event: Mapping[str, Any]) -> tuple[str, str]:
    stage = _str(event, "stage", "(unknown)")
    status = _str(event, "status", "ok")
    duration = event.get("duration_sec")
    detail = _str(event, "detail", "")
    suffix = ""
    if duration is not None:
        try:
            suffix = f" in {float(duration):.1f}s"
        except (TypeError, ValueError):
            suffix = ""
    tail = f" -- {detail}" if detail else ""
    return "stage", f"Stage {stage!r} ended (status={status}){suffix}.{tail}"


def _rule_gate_open(event: Mapping[str, Any]) -> tuple[str, str]:
    stage = _str(event, "stage", "(unknown)")
    return (
        "stage",
        f"Approval gate opened for stage {stage!r} -- awaiting human review.",
    )


def _rule_gate_close(event: Mapping[str, Any]) -> tuple[str, str]:
    stage = _str(event, "stage", "(unknown)")
    decision = _str(event, "decision", "approved")
    reviewer = _str(event, "reviewer", "")
    by = f" by {reviewer}" if reviewer else ""
    return "stage", f"Approval gate closed for stage {stage!r}: {decision}{by}."


def _rule_preview_built(event: Mapping[str, Any]) -> tuple[str, str]:
    artifact_type = _str(event, "artifact_type", "preview")
    scene_num = event.get("scene_num")
    url = _str(event, "preview_url", "")
    duration = event.get("duration_sec")
    scope = "scene" if scene_num is not None else "clip"
    where = f"scene {scene_num}" if scene_num is not None else "clip"
    parts = [f"Preview built ({artifact_type}) for {where}"]
    if duration is not None:
        try:
            parts.append(f"duration {float(duration):.1f}s")
        except (TypeError, ValueError):
            pass
    if url:
        parts.append(url)
    return scope, ": ".join(parts[:1]) + (" -- " + ", ".join(parts[1:]) if len(parts) > 1 else "") + "."


def _rule_ledger_change(event: Mapping[str, Any]) -> tuple[str, str]:
    revision = event.get("revision")
    scope = _str(event, "scope", "global")
    polarity = _str(event, "polarity", "prefer")
    subject = _str(event, "subject", "(subject)")
    content = _str(event, "content", "")
    scope_ref = _str(event, "scope_ref", "")
    rev_tag = f"R{revision}" if revision is not None else "R?"
    digest_scope = "global" if scope == "global" else (
        "scene" if scope == "scene" else (
            "clip" if scope in ("voice_block", "artifact_type", "element") else "stage"
        )
    )
    target = f" in {scope}:{scope_ref}" if scope_ref else f" ({scope})"
    suffix = f" -- {content}" if content else ""
    return (
        digest_scope,
        f"Ledger {rev_tag}: {polarity} {subject}{target}.{suffix}",
    )


def _rule_ladder_step(event: Mapping[str, Any]) -> tuple[str, str]:
    level = event.get("level")
    level_name = _str(event, "level_name", "")
    operation = _str(event, "operation", "(op)")
    action = _str(event, "action", "(action)")
    explanation = _str(event, "explanation", "")
    success = event.get("success")
    level_tag = f"L{level}" if level is not None else "L?"
    name_tag = f" ({level_name})" if level_name else ""
    outcome = ""
    if success is True:
        outcome = " succeeded"
    elif success is False:
        outcome = " failed"
    detail = f" -- {explanation}" if explanation else ""
    return (
        "stage",
        f"Ladder {level_tag}{name_tag} on {operation!r}: {action}{outcome}.{detail}",
    )


def _rule_qa_verdict(event: Mapping[str, Any]) -> tuple[str, str]:
    source = _str(event, "source", "(gate)")
    check = _str(event, "check_name", "")
    verdict = _str(event, "verdict", "pass")
    message = _str(event, "message", "")
    artifact_type = _str(event, "artifact_type", "")
    artifact_id = _str(event, "artifact_id", "")
    scope = _scope_from_artifact(artifact_type)
    check_tag = f".{check}" if check else ""
    where = f" on {artifact_type}:{artifact_id}" if (artifact_type and artifact_id) else ""
    tail = f" -- {message}" if message else ""
    return "stage" if scope == "global" else scope, (
        f"QA {source}{check_tag}: {verdict}{where}.{tail}"
    )


def _scope_from_artifact(artifact_type: str) -> str:
    if artifact_type in ("clip", "audio", "video_clip", "narration"):
        return "clip"
    if artifact_type in ("scene", "scene_script", "visual_concept"):
        return "scene"
    if artifact_type in ("scenario", "assembly", "assembled_video"):
        return "stage"
    return "global"


def _rule_infra_event(event: Mapping[str, Any]) -> tuple[str, str]:
    kind = _str(event, "event", "(event)")
    worker = _str(event, "worker", "")
    level = event.get("level")
    detail = _str(event, "detail", "")
    parts = [f"Infra {kind}"]
    if worker:
        parts.append(f"worker={worker}")
    if level is not None:
        parts.append(f"L{level}")
    header = " ".join(parts)
    tail = f" -- {detail}" if detail else ""
    return "global", f"{header}.{tail}"


def _rule_eta_revision(event: Mapping[str, Any]) -> tuple[str, str]:
    stage = _str(event, "stage", "(stage)")
    old = event.get("old_eta_sec")
    new = event.get("new_eta_sec")
    reason = _str(event, "reason", "")
    arrow = ""
    if old is not None and new is not None:
        try:
            arrow = f" {float(old):.0f}s -> {float(new):.0f}s"
        except (TypeError, ValueError):
            arrow = ""
    tail = f" ({reason})" if reason else ""
    return "stage", f"ETA revised for {stage!r}:{arrow}.{tail}"


_RULES = {
    "stage_start": _rule_stage_start,
    "stage_end": _rule_stage_end,
    "gate_open": _rule_gate_open,
    "gate_close": _rule_gate_close,
    "preview_built": _rule_preview_built,
    "ledger_change": _rule_ledger_change,
    "ladder_step": _rule_ladder_step,
    "qa_verdict": _rule_qa_verdict,
    "infra_event": _rule_infra_event,
    "eta_revision": _rule_eta_revision,
}
# Assertion: the rule table covers exactly the closed vocabulary.
assert tuple(sorted(_RULES)) == tuple(sorted(EVENT_KINDS))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarise_event(event_kind: str, event: Mapping[str, Any]) -> ReasoningDigest:
    """Produce a :class:`ReasoningDigest` from a raw pipeline event.

    Deterministic and side-effect free.  The returned digest has a
    ``timestamp`` set to ``time.time()`` unless ``event`` carries one.

    Raises
    ------
    ValueError
        If ``event_kind`` is not in :data:`EVENT_KINDS`.  Fail-loud by
        design -- see invariant (4) in the module docstring.
    TypeError
        If ``event`` is not a mapping.
    """
    if not isinstance(event, Mapping):
        raise TypeError(
            f"event must be a mapping, got {type(event).__name__}"
        )
    rule = _RULES.get(event_kind)
    if rule is None:
        raise ValueError(
            f"unknown reasoning-digest event kind: {event_kind!r} "
            f"(known: {sorted(EVENT_KINDS)})"
        )
    scope, raw_summary = rule(event)
    if scope not in SCOPES:
        # Defensive: rules must return a valid scope; a bug in a rule is
        # a bug that should fail loud, not silently produce an unknown
        # taxonomy value on the dashboard.
        raise ValueError(
            f"rule for {event_kind!r} produced unknown scope {scope!r} "
            f"(known: {sorted(SCOPES)})"
        )
    summary = _truncate(raw_summary)
    ts_raw = event.get("timestamp")
    try:
        timestamp = float(ts_raw) if ts_raw is not None else time.time()
    except (TypeError, ValueError):
        timestamp = time.time()
    return ReasoningDigest(
        timestamp=timestamp,
        kind=event_kind,
        scope=scope,
        summary=summary,
        source_event=dict(event),
    )


# ---------------------------------------------------------------------------
# SSE subscriber bus
# ---------------------------------------------------------------------------
#
# ``write_digest`` must be non-blocking -- the caller (a pipeline callback,
# recovery attempt, ledger append, ...) must never stall waiting for a slow
# dashboard consumer. We use ``collections.deque`` (thread-safe append +
# popleft) wrapped in a module-level lock only for subscriber add/remove,
# so emission is strictly O(len(subscribers)) of deque appends.
#
# The deque is bounded so a disconnected subscriber cannot cause unbounded
# memory growth -- the oldest events are dropped first.

_DIGEST_QUEUE_MAX = 2048

_subscribers_lock = threading.Lock()
_subscribers: list[collections.deque] = []


def subscribe_digest_stream() -> collections.deque:
    """Register a new subscriber queue and return it.

    Callers (typically the ``/api/reasoning_digest_stream`` SSE endpoint)
    must call :func:`unsubscribe_digest_stream` when done to avoid leaking
    queues.  The queue is a bounded :class:`collections.deque` -- if the
    subscriber lags, the *oldest* digests are dropped first.
    """
    queue: collections.deque = collections.deque(maxlen=_DIGEST_QUEUE_MAX)
    with _subscribers_lock:
        _subscribers.append(queue)
    return queue


def unsubscribe_digest_stream(queue: collections.deque) -> None:
    """Remove a subscriber queue previously returned by :func:`subscribe_digest_stream`."""
    with _subscribers_lock:
        try:
            _subscribers.remove(queue)
        except ValueError:
            pass


def _snapshot_subscribers() -> list[collections.deque]:
    with _subscribers_lock:
        return list(_subscribers)


# Module-level cache for the AG-UI bridge so the per-emit hot path never
# re-executes the ``agui`` module (and its FastAPI route registration).
# ``None`` means "not yet looked up"; ``False`` means "lookup failed --
# bridge disabled"; a callable means "ready to call".
_AGUI_EMIT: Any = None


def _agui_emitter() -> Any:
    """Return a cached callable for :func:`agui.emit_agui_event`, or ``False``.

    The first call performs a local import of :mod:`agui` (to avoid the
    circular import that would happen at module load time -- ``agui``
    itself imports routers that eventually reach us).  Subsequent calls
    return the cached callable without re-importing, keeping the emit
    hot path at sub-millisecond cost.
    """
    global _AGUI_EMIT
    if _AGUI_EMIT is not None:
        return _AGUI_EMIT
    try:
        from agui import emit_agui_event as _emit  # noqa: WPS433
    except Exception as exc:  # pragma: no cover -- bridge optional
        logger.debug("reasoning_digest: AG-UI bridge disabled (%s)", exc)
        _AGUI_EMIT = False
        return False
    _AGUI_EMIT = _emit
    return _emit


def _emit_sse(digest: ReasoningDigest) -> None:
    """Fire-and-forget emission of ``digest`` onto every subscriber queue.

    Also bridges the digest onto the unified AG-UI event bus (via
    :func:`agui.emit_agui_event`) so dashboards that only consume the
    single CopilotKit SSE stream still receive digests as ``reasoning_digest``
    custom events.  The bridge is best-effort: if the AG-UI module is
    not importable (e.g. during unit tests that only exercise the digest
    writer) the bridge is disabled after the first failed lookup.
    """
    payload = digest.to_dict()
    for queue in _snapshot_subscribers():
        # deque.append is atomic under CPython's GIL; no lock needed per-push.
        queue.append(payload)
    emit = _agui_emitter()
    if not emit:
        return
    try:
        emit("reasoning_digest", payload)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("emit_agui_event bridge for reasoning_digest failed: %s", exc)


# ---------------------------------------------------------------------------
# Blackboard log
# ---------------------------------------------------------------------------


def _load_log(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the raw digest log list stored in ``state``.

    Accepts both list storage (the default) and JSON-string storage (the
    blackboard convention used by :mod:`server.callbacks.preference_ledger`
    for ADK ``output_key`` round-tripping). An absent key is treated as
    an empty log.
    """
    raw = state.get(REASONING_DIGEST_LOG_KEY)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{REASONING_DIGEST_LOG_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError(
                f"{REASONING_DIGEST_LOG_KEY!r} must decode to a list, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{REASONING_DIGEST_LOG_KEY!r} must be a JSON string or list, "
        f"got {type(raw).__name__}"
    )


def _append_to_log(state: MutableMapping[str, Any], digest: ReasoningDigest) -> None:
    existing = _load_log(state)
    existing.append(digest.to_dict())
    # Preserve the storage shape if the caller seeded a JSON string; default
    # to plain list storage (simpler and cheaper).
    current = state.get(REASONING_DIGEST_LOG_KEY)
    if isinstance(current, str):
        state[REASONING_DIGEST_LOG_KEY] = json.dumps(existing, ensure_ascii=False)
    else:
        state[REASONING_DIGEST_LOG_KEY] = existing


def write_digest(
    state: Optional[MutableMapping[str, Any]],
    digest: ReasoningDigest,
) -> ReasoningDigest:
    """Append ``digest`` to the blackboard log and emit onto the SSE channel.

    Both writes are non-blocking.  The blackboard append is a list mutation
    on the caller-provided state mapping; the SSE emission is a bounded
    deque append per subscriber plus a best-effort bridge onto the unified
    AG-UI event bus.

    ``state`` may be ``None`` -- emission-only mode is useful for call sites
    that do not own a session state (e.g. fleet-level infra events).

    Returns the same digest for convenient chaining.
    """
    if state is not None:
        try:
            _append_to_log(state, digest)
        except Exception as exc:  # pragma: no cover -- defensive
            # The blackboard append must never break the emission.  Log
            # and continue so the dashboard still receives the signal.
            logger.warning(
                "reasoning_digest: failed to append to blackboard log: %s",
                exc,
            )
    _emit_sse(digest)
    return digest


def emit_digest(
    state: Optional[MutableMapping[str, Any]],
    event_kind: str,
    event: Mapping[str, Any],
) -> ReasoningDigest:
    """Convenience wrapper: summarise + write in one call.

    This is the canonical entry point used by every hook site in the
    codebase.  See the module docstring for the hook inventory.

    Raises
    ------
    ValueError
        If ``event_kind`` is not in :data:`EVENT_KINDS`.
    """
    digest = summarise_event(event_kind, event)
    return write_digest(state, digest)


def get_digest_log(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the digest log stored on ``state`` as a list of plain dicts.

    Useful for cross-stage consumers and tests. Malformed storage raises
    ``ValueError`` / ``TypeError`` -- no silent degradation.
    """
    return _load_log(state)


__all__ = [
    "EVENT_KINDS",
    "MAX_SUMMARY_CHARS",
    "REASONING_DIGEST_LOG_KEY",
    "ReasoningDigest",
    "SCOPES",
    "emit_digest",
    "get_digest_log",
    "subscribe_digest_stream",
    "summarise_event",
    "unsubscribe_digest_stream",
    "write_digest",
]
