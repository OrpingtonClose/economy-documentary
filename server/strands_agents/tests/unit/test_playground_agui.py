"""AG-UI wire-format contract tests for the playground event bus.

Every event serialised by :meth:`Event.to_dict` must carry an AG-UI
``type`` alongside the legacy ``kind``. These tests pin:

* The mapping table covers every kind documented in the ``Event.kind``
  docstring — a new kind that lands without a mapping entry is a
  test failure (not a silent ``CUSTOM`` fallback).
* ``Event.to_dict`` always includes an AG-UI ``type`` and keeps the
  legacy ``kind`` field intact (so existing consumers don't break).
* Known kinds map to the documented AG-UI types (``STEP_STARTED``
  for ``task.start``, ``TOOL_CALL_START`` for ``tool.called``, etc.).
* Unknown / future kinds fall back to ``CUSTOM`` with the legacy
  kind carried in ``name``, rather than raising.
* Extra AG-UI fields (``step_name`` / ``source`` / ``cancelled``)
  land on the serialised envelope when the mapping declares them.
"""

from __future__ import annotations

import pytest

from strands_agents.playground import events as events_module
from strands_agents.playground.agui import (
    AGUI_TYPES,
    CUSTOM,
    KNOWN_KINDS,
    RUN_ERROR,
    RUN_FINISHED,
    RUN_STARTED,
    STEP_FINISHED,
    STEP_STARTED,
    TEXT_MESSAGE_CONTENT,
    TOOL_CALL_END,
    TOOL_CALL_START,
    agui_envelope,
)
from strands_agents.playground.events import Event


# --------------------------------------------------------------------------
# Mapping coverage


#: Every kind documented in ``events.Event.kind``. The docstring is
#: the spec; if a new kind lands in emitters but the docstring
#: isn't updated, this list will drift — the test below re-parses
#: the docstring so the two can't disagree silently.
_DOCUMENTED_KINDS = frozenset(
    {
        "run.dispatched",
        "probe.start",
        "probe.done",
        "task.pick_model",
        "task.start",
        "task.done",
        "tool.called",
        "tool.returned",
        "evaluate.start",
        "evaluate.scored",
        "narrate",
        "interpret",
        "run.ok",
        "run.error",
        "run.cancelled",
    }
)


def test_every_documented_kind_has_an_agui_mapping() -> None:
    """Every kind emitted by the playground has a known AG-UI type.

    The legacy kind vocabulary is stable (see ``Event.kind`` docstring
    in ``events.py``). Any addition lands here first — adding a new
    kind without a matching entry in ``_KIND_TO_AGUI`` would drop
    it through the ``CUSTOM`` fallback, which is legal but not what
    we want for a kind we know about. The test catches the omission.
    """
    missing = _DOCUMENTED_KINDS - KNOWN_KINDS
    assert not missing, f"documented kinds without AG-UI mapping: {sorted(missing)}"


def test_documented_and_mapped_kinds_agree() -> None:
    """Docstring vocabulary and the mapping table agree.

    A mapping entry for a kind nobody documents is dead code at
    best, lying metadata at worst. Flipping-around of
    :func:`test_every_documented_kind_has_an_agui_mapping`.
    """
    extra = KNOWN_KINDS - _DOCUMENTED_KINDS
    assert not extra, f"mapped kinds not listed in Event docstring: {sorted(extra)}"


@pytest.mark.parametrize(
    ("kind", "expected_type", "expected_extras"),
    [
        ("run.dispatched", RUN_STARTED, {}),
        ("run.ok", RUN_FINISHED, {}),
        ("run.error", RUN_ERROR, {}),
        ("run.cancelled", RUN_FINISHED, {"cancelled": True}),
        ("probe.start", STEP_STARTED, {"step_name": "probe"}),
        ("probe.done", STEP_FINISHED, {"step_name": "probe"}),
        ("task.start", STEP_STARTED, {"step_name": "task"}),
        ("task.done", STEP_FINISHED, {"step_name": "task"}),
        ("evaluate.start", STEP_STARTED, {"step_name": "evaluate"}),
        ("evaluate.scored", STEP_FINISHED, {"step_name": "evaluate"}),
        ("tool.called", TOOL_CALL_START, {}),
        ("tool.returned", TOOL_CALL_END, {}),
        ("narrate", TEXT_MESSAGE_CONTENT, {"source": "narrator"}),
        ("interpret", TEXT_MESSAGE_CONTENT, {"source": "interpreter"}),
        ("task.pick_model", CUSTOM, {"name": "task.pick_model"}),
    ],
)
def test_agui_envelope_mapping(
    kind: str, expected_type: str, expected_extras: dict[str, object]
) -> None:
    """Each documented kind maps to the expected AG-UI envelope."""
    envelope = agui_envelope(kind)
    assert envelope["type"] == expected_type
    for key, value in expected_extras.items():
        assert envelope[key] == value, (
            f"kind={kind!r} missing expected {key!r}={value!r}; got {envelope!r}"
        )
    # Envelope keys are a subset of the AG-UI-spec field names we
    # emit — any stray key would be a silent schema expansion.
    allowed_keys = {"type", "step_name", "source", "name", "cancelled"}
    assert set(envelope.keys()) <= allowed_keys, (
        f"kind={kind!r} produced non-AG-UI keys: "
        f"{sorted(set(envelope.keys()) - allowed_keys)}"
    )


def test_unknown_kind_falls_back_to_custom() -> None:
    """An unregistered kind becomes AG-UI ``CUSTOM`` with the kind as name.

    Doing anything louder (raising, logging, crashing) would risk
    dropping a live-run event — the stream is a feedback surface,
    not a validator. Falling back to ``CUSTOM`` keeps the envelope
    AG-UI-compliant while preserving the original kind for callers
    that still want to branch on it.
    """
    envelope = agui_envelope("wild.unseen.kind")
    assert envelope == {"type": CUSTOM, "name": "wild.unseen.kind"}


def test_agui_envelope_returns_fresh_dict_each_call() -> None:
    """Caller mutation of the envelope must not leak into the table."""
    first = agui_envelope("probe.start")
    first["step_name"] = "MUTATED"
    second = agui_envelope("probe.start")
    assert second["step_name"] == "probe"


# --------------------------------------------------------------------------
# Event.to_dict carries both the legacy kind and the AG-UI envelope


def test_to_dict_preserves_legacy_kind_and_adds_agui_type() -> None:
    """Event envelopes carry ``kind`` (legacy) + ``type`` (AG-UI) + all extras.

    Backwards-compat: every existing consumer branches on ``kind``,
    so the field must stay untouched. Forwards-compat: new clients
    branch on ``type``. Both sit on the same envelope.
    """
    event = Event(seq=7, ts=123.0, kind="tool.called", summary="step 1", detail={"tool": "x"})
    payload = event.to_dict()
    assert payload["kind"] == "tool.called"
    assert payload["type"] == TOOL_CALL_START
    assert payload["summary"] == "step 1"
    assert payload["detail"] == {"tool": "x"}
    assert payload["seq"] == 7


def test_to_dict_carries_step_name_for_step_events() -> None:
    event = Event(seq=1, ts=0.0, kind="probe.start", summary="go")
    payload = event.to_dict()
    assert payload["type"] == STEP_STARTED
    assert payload["step_name"] == "probe"


def test_to_dict_carries_source_for_text_events() -> None:
    event = Event(seq=1, ts=0.0, kind="narrate", summary="still probing")
    payload = event.to_dict()
    assert payload["type"] == TEXT_MESSAGE_CONTENT
    assert payload["source"] == "narrator"


def test_to_dict_carries_cancelled_flag_on_cancellation() -> None:
    """``run.cancelled`` has no first-class AG-UI event; flag must surface."""
    event = Event(seq=9, ts=0.0, kind="run.cancelled", summary="cancelled")
    payload = event.to_dict()
    assert payload["type"] == RUN_FINISHED
    assert payload["cancelled"] is True


def test_to_dict_type_is_always_a_known_agui_type() -> None:
    """Spot-check: even malformed events get a known AG-UI type."""
    event = Event(seq=1, ts=0.0, kind="mystery.kind", summary="?")
    payload = event.to_dict()
    assert payload["type"] in AGUI_TYPES


# --------------------------------------------------------------------------
# events.py module hygiene


def test_events_module_exposes_to_dict_with_agui_type() -> None:
    """Sanity check that the import wiring in events.py still works.

    Guards against a future refactor accidentally removing the
    ``from .agui import agui_envelope`` line — the module-level
    import is what ties the two files together, so a broken import
    would silently strip ``type`` from every envelope.
    """
    assert hasattr(events_module, "Event")
    event = events_module.Event(seq=1, ts=0.0, kind="run.ok", summary="done")
    assert event.to_dict().get("type") == RUN_FINISHED
