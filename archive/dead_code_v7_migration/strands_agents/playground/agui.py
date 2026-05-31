"""AG-UI wire format mapping for playground events.

Playground events carry two discriminators in every serialised envelope:

* ``kind`` — the internal, stable vocabulary used by the narrator
  prompt, the stall-budget table, and the regression tests. Stays
  untouched so every existing consumer keeps working.
* ``type`` — the `AG-UI`_ event type, plus any AG-UI-specific fixed
  fields (``step_name`` / ``source`` / ``name`` / …). Lets AG-UI
  clients (the AG-UI SDK, Langfuse dashboards, third-party tooling)
  consume the same stream without a custom decoder.

.. _AG-UI: https://docs.ag-ui.com

Mapping policy (see ``_KIND_TO_AGUI`` below for the authoritative
table):

* ``run.*`` maps to AG-UI ``RUN_STARTED`` / ``RUN_FINISHED`` /
  ``RUN_ERROR``. ``run.cancelled`` has no first-class AG-UI analog;
  we emit ``RUN_FINISHED`` with ``cancelled: true`` so clients
  tolerant to extra fields can still render the distinction without
  inventing a type.
* ``probe.*`` / ``task.*`` / ``evaluate.*`` map to ``STEP_STARTED``
  / ``STEP_FINISHED`` with an explicit ``step_name``.
* ``tool.called`` / ``tool.returned`` map to ``TOOL_CALL_START`` /
  ``TOOL_CALL_END``.
* ``narrate`` / ``interpret`` map to ``TEXT_MESSAGE_CONTENT`` with a
  ``source`` attribution so Langfuse can tell narrator output apart
  from interpreter output in the same trace.
* Anything not in the table falls back to ``CUSTOM`` with the
  legacy kind copied to ``name`` — the stream never silently drops
  an event and never lies about its protocol compliance.

This module is pure data + pure functions. No I/O, no logging, no
state. :func:`agui_envelope` is called from ``Event.to_dict`` on
every serialised event and must stay allocation-light.
"""

from __future__ import annotations

from typing import Any

# AG-UI event-type string constants. The spec is stringly-typed, so
# these are just names — keeping them as module-level constants
# means a typo in a test is a NameError, not a passing assertion
# against a stringly-compared literal.
RUN_STARTED = "RUN_STARTED"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
STEP_STARTED = "STEP_STARTED"
STEP_FINISHED = "STEP_FINISHED"
TOOL_CALL_START = "TOOL_CALL_START"
TOOL_CALL_END = "TOOL_CALL_END"
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
CUSTOM = "CUSTOM"

#: Authoritative map from legacy playground ``kind`` to (AG-UI ``type``,
#: fixed extra fields). Ordered by the sequence an event is typically
#: seen in during a run — reading top-to-bottom mirrors the expected
#: timeline, which is useful when staring at assertion failures.
_KIND_TO_AGUI: dict[str, tuple[str, dict[str, Any]]] = {
    "run.dispatched": (RUN_STARTED, {}),
    "probe.start": (STEP_STARTED, {"step_name": "probe"}),
    "probe.done": (STEP_FINISHED, {"step_name": "probe"}),
    "task.pick_model": (CUSTOM, {"name": "task.pick_model"}),
    "task.start": (STEP_STARTED, {"step_name": "task"}),
    "tool.called": (TOOL_CALL_START, {}),
    "tool.returned": (TOOL_CALL_END, {}),
    "task.done": (STEP_FINISHED, {"step_name": "task"}),
    "evaluate.start": (STEP_STARTED, {"step_name": "evaluate"}),
    "evaluate.scored": (STEP_FINISHED, {"step_name": "evaluate"}),
    "narrate": (TEXT_MESSAGE_CONTENT, {"source": "narrator"}),
    "interpret": (TEXT_MESSAGE_CONTENT, {"source": "interpreter"}),
    "run.ok": (RUN_FINISHED, {}),
    "run.error": (RUN_ERROR, {}),
    "run.cancelled": (RUN_FINISHED, {"cancelled": True}),
}

#: The complete set of kinds this module knows about. Used by the
#: unit test to assert the mapping covers every kind referenced in
#: the ``Event.kind`` docstring — a new kind without a mapping is a
#: test failure, not a silent ``CUSTOM`` fallback.
KNOWN_KINDS: frozenset[str] = frozenset(_KIND_TO_AGUI.keys())

#: Every AG-UI type this module can emit. Handy for AG-UI-shape
#: validation tests that assert every envelope's ``type`` is one of
#: these strings.
AGUI_TYPES: frozenset[str] = frozenset(
    {
        RUN_STARTED,
        RUN_FINISHED,
        RUN_ERROR,
        STEP_STARTED,
        STEP_FINISHED,
        TOOL_CALL_START,
        TOOL_CALL_END,
        TEXT_MESSAGE_CONTENT,
        CUSTOM,
    }
)


def agui_envelope(kind: str) -> dict[str, Any]:
    """Return the AG-UI envelope fragment for a legacy ``kind``.

    The return value is meant to be **spread into** the existing
    legacy envelope (``{"seq": ..., "kind": ..., **agui_envelope(kind)}``)
    so AG-UI fields sit alongside the legacy ones. Returning a
    fresh dict on every call (rather than a shared one) keeps callers
    from accidentally mutating the mapping table.

    Args:
        kind: The playground event kind (e.g. ``"tool.called"``).

    Returns:
        A new ``dict`` with at least a ``type`` key. May also include
        ``step_name``, ``source``, ``name``, or ``cancelled``
        depending on the kind. Unknown kinds get
        ``{"type": "CUSTOM", "name": <kind>}`` rather than raising —
        the stream is a feedback surface, not a validator, and
        forcing a synchronous failure here would silently drop
        events during a live run.
    """
    mapping = _KIND_TO_AGUI.get(kind)
    if mapping is None:
        return {"type": CUSTOM, "name": kind}
    agui_type, extra = mapping
    return {"type": agui_type, **extra}


__all__ = ["CUSTOM",
    "RUN_ERROR",
    "RUN_FINISHED",
    "RUN_STARTED",
    "STEP_FINISHED",
    "STEP_STARTED",
    "TEXT_MESSAGE_CONTENT",
    "TOOL_CALL_END",
    "TOOL_CALL_START",
    "agui_envelope",]
