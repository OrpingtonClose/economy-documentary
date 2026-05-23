"""
Consistency Checker -- ARCH-A5 (issue #135) under parent ARCH-A #123 / meta
ARCH-2026 #122.

Fires at every stage boundary, every gate, and every tool call. Compares the
ledger revision a stage was derived against (the *derivation revision*, set
by ARCH-B1, not yet built -- stubbed here) to the current revision of the
Preference Ledger (ARCH-A1, :mod:`callbacks.preference_ledger`). On drift,
emits a structured :class:`LedgerDrift` signal into the blackboard for the
re-manifestation executor (ARCH-A6, future ticket) to consume.

**This module only DETECTS and SIGNALS drift.** It does NOT plan, validate,
or execute re-manifestation -- that is ARCH-A6. Keeping the contract narrow
lets A5 land before B1 / A6 and mirrors the scope discipline of A1 (#131).

Design invariants (enforced by tests in
``server/tests/test_consistency_checker.py``):

1. **Blackboard-only access.** Reads the ledger through
   :data:`callbacks.preference_ledger.PREFERENCE_LEDGER_KEY`; writes drift
   signals under :data:`LEDGER_DRIFT_SIGNALS_KEY`. No direct cross-stage
   imports.
2. **Fail loud on invariant violations.**
   * Ledger state absent from the blackboard -> ``RuntimeError``.
   * Malformed ledger / derivation records -> propagate ``ValueError`` /
     ``TypeError`` from the ledger module.
   * Revision decrease (derivation revision > current revision, impossible
     for an append-only ledger) -> ``RuntimeError``.
3. **Warn (do not fail) for untagged artifacts.** ARCH-B1 has not yet
   populated :data:`STAGE_DERIVATIONS_KEY` for every stage; until it does,
   unknown stages log a single warning and return ``None`` (no drift
   signal).
4. **Drift is a signal, not an error.** ``derivation_rev < current_rev``
   emits a :class:`LedgerDrift` into the blackboard and returns it; the
   caller (agent, tool, gate) is free to continue.

Registration surface:

* ``after_agent_consistency_check`` -- ADK ``after_agent_callback`` for
  every stage-boundary check, mirroring :mod:`callbacks.timeline_guardian`.
* ``before_tool_consistency_check`` -- ADK ``before_tool_callback`` for
  every tool, mirroring :mod:`callbacks.before_tool`.
* ``check_consistency_at_gate`` -- plain function invokable from the
  gate-polling path (human L4 path in ARCH-H5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping, Optional

from callbacks.preference_ledger import (
    PREFERENCE_LEDGER_KEY,
    _load_raw,
    current_revision,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State keys
# ---------------------------------------------------------------------------

#: Blackboard key under which per-stage derivation tags are stored.
#: Shape: ``{stage_name: {"revision": int, "artifact_ids": [str, ...]}}``.
#: Stored either as a raw dict (convenient for tests) or as a JSON-encoded
#: string (the blackboard convention used elsewhere in ``server/callbacks``).
#:
#: ARCH-B1 (#137) will define the canonical tagging helper and populate this
#: map as artifacts are produced. Until then, this module treats absent
#: stages as "untagged" (warn, no drift) rather than failing.
STAGE_DERIVATIONS_KEY = "_stage_derivations"

#: Blackboard key under which :class:`LedgerDrift` signals are appended.
#: Value is a JSON-encoded list of :meth:`LedgerDrift.to_dict` outputs, in
#: insertion order. ARCH-A6 will drain this queue and produce a
#: re-manifestation plan.
LEDGER_DRIFT_SIGNALS_KEY = "ledger_drift_signals"


# ---------------------------------------------------------------------------
# Drift signal record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerDrift:
    """A structured signal that a stage's derivation is older than the ledger.

    Carries everything ARCH-A6 needs to plan minimal re-manifestation:

    * ``stage_name`` -- which stage drifted (matches the key in
      :data:`STAGE_DERIVATIONS_KEY` and the ADK agent name).
    * ``artifact_ids`` -- the stage's tagged artifacts at derivation time;
      may be empty if B1 tagged the stage with no artifact list.
    * ``from_rev`` -- the derivation revision the stage was built against.
    * ``to_rev`` -- the current ledger revision at the moment of drift
      detection.
    * ``new_records`` -- the ledger entries appended in ``(from_rev,
      to_rev]``, serialised via :meth:`PreferenceRecord.to_dict`. These are
      what the impact analyzer (A6) scope-matches against artifacts.
    """

    stage_name: str
    artifact_ids: tuple[str, ...]
    from_rev: int
    to_rev: int
    new_records: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage_name, str) or not self.stage_name:
            raise ValueError(
                f"LedgerDrift.stage_name must be a non-empty string, "
                f"got {self.stage_name!r}"
            )
        if not isinstance(self.from_rev, int) or isinstance(self.from_rev, bool):
            raise TypeError(
                f"LedgerDrift.from_rev must be int, got {type(self.from_rev).__name__}"
            )
        if not isinstance(self.to_rev, int) or isinstance(self.to_rev, bool):
            raise TypeError(
                f"LedgerDrift.to_rev must be int, got {type(self.to_rev).__name__}"
            )
        if self.from_rev < 0:
            raise ValueError(
                f"LedgerDrift.from_rev must be >= 0, got {self.from_rev}"
            )
        if self.to_rev <= self.from_rev:
            raise ValueError(
                f"LedgerDrift requires to_rev ({self.to_rev}) > "
                f"from_rev ({self.from_rev}); not a drift otherwise"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-friendly dict."""
        return {
            "stage_name": self.stage_name,
            "artifact_ids": list(self.artifact_ids),
            "from_rev": self.from_rev,
            "to_rev": self.to_rev,
            "new_records": [dict(r) for r in self.new_records],
        }


# ---------------------------------------------------------------------------
# Stage-derivation stub (ARCH-B1 will own this surface)
# ---------------------------------------------------------------------------


def _load_stage_derivations(
    state: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return the stage-derivation map from ``state``.

    Accepts either a raw ``dict`` or a JSON-encoded string (mirroring the
    dual storage convention used by the ledger itself). An absent key is
    treated as an empty map.

    Raises ``ValueError`` / ``TypeError`` if the stored value is malformed,
    in line with the fail-loud invariant.
    """
    raw = state.get(STAGE_DERIVATIONS_KEY)
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{STAGE_DERIVATIONS_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                f"{STAGE_DERIVATIONS_KEY!r} must decode to a dict, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{STAGE_DERIVATIONS_KEY!r} must be a dict or JSON string, "
        f"got {type(raw).__name__}"
    )


def _validate_derivation_entry(
    stage_name: str, entry: Any
) -> tuple[int, tuple[str, ...]]:
    """Validate a single stage-derivation entry and return ``(revision, ids)``.

    Shape expected: ``{"revision": int, "artifact_ids": [str, ...]}``.
    ``artifact_ids`` is optional (defaults to empty tuple) to let B1 tag a
    stage as derived-at-revision without enumerating artifacts yet.
    """
    if not isinstance(entry, Mapping):
        raise TypeError(
            f"stage_derivations[{stage_name!r}] must be a mapping, "
            f"got {type(entry).__name__}"
        )
    if "revision" not in entry:
        raise ValueError(
            f"stage_derivations[{stage_name!r}] missing 'revision' field"
        )
    rev = entry["revision"]
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise TypeError(
            f"stage_derivations[{stage_name!r}].revision must be int, "
            f"got {type(rev).__name__}"
        )
    if rev < 0:
        raise ValueError(
            f"stage_derivations[{stage_name!r}].revision must be >= 0, "
            f"got {rev}"
        )

    artifact_ids_raw = entry.get("artifact_ids", ())
    if isinstance(artifact_ids_raw, str):
        # Guard against the easy mistake of passing a single id as a string;
        # strings are iterable but that is almost certainly a bug.
        raise TypeError(
            f"stage_derivations[{stage_name!r}].artifact_ids must be a list/tuple "
            f"of strings, got str (did you mean [id]?)"
        )
    if not isinstance(artifact_ids_raw, (list, tuple)):
        raise TypeError(
            f"stage_derivations[{stage_name!r}].artifact_ids must be a list/tuple, "
            f"got {type(artifact_ids_raw).__name__}"
        )
    artifact_ids: list[str] = []
    for i, aid in enumerate(artifact_ids_raw):
        if not isinstance(aid, str):
            raise TypeError(
                f"stage_derivations[{stage_name!r}].artifact_ids[{i}] must be str, "
                f"got {type(aid).__name__}"
            )
        artifact_ids.append(aid)
    return rev, tuple(artifact_ids)


# ---------------------------------------------------------------------------
# Drift-signal queue
# ---------------------------------------------------------------------------


def _load_drift_signals(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get(LEDGER_DRIFT_SIGNALS_KEY)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{LEDGER_DRIFT_SIGNALS_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError(
                f"{LEDGER_DRIFT_SIGNALS_KEY!r} must decode to a list, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{LEDGER_DRIFT_SIGNALS_KEY!r} must be a list or JSON string, "
        f"got {type(raw).__name__}"
    )


def _append_drift_signal(
    state: MutableMapping[str, Any], drift: LedgerDrift
) -> None:
    signals = _load_drift_signals(state)
    signals.append(drift.to_dict())
    state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps(signals, ensure_ascii=False)


def pending_drift_signals(state: Mapping[str, Any]) -> list[LedgerDrift]:
    """Return all drift signals currently queued, oldest first.

    Provided for ARCH-A6 wiring and tests. Does not mutate state.
    """
    out: list[LedgerDrift] = []
    for entry in _load_drift_signals(state):
        if not isinstance(entry, Mapping):
            raise TypeError(
                f"ledger_drift_signals entry must be mapping, "
                f"got {type(entry).__name__}"
            )
        out.append(
            LedgerDrift(
                stage_name=entry["stage_name"],
                artifact_ids=tuple(entry.get("artifact_ids", [])),
                from_rev=int(entry["from_rev"]),
                to_rev=int(entry["to_rev"]),
                new_records=tuple(
                    dict(r) for r in entry.get("new_records", [])
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def _records_in_range(
    state: Mapping[str, Any], *, lo_exclusive: int, hi_inclusive: int
) -> list[dict[str, Any]]:
    """Return ledger records with revision in ``(lo_exclusive, hi_inclusive]``.

    Preserves stored insertion order. Re-uses the ledger's raw loader so any
    malformation propagates loudly.
    """
    out: list[dict[str, Any]] = []
    for entry in _load_raw(state):
        if not isinstance(entry, Mapping) or "revision" not in entry:
            raise ValueError(
                "preference_ledger contains an entry without a revision "
                f"field: {entry!r}"
            )
        rev = entry["revision"]
        if not isinstance(rev, int) or isinstance(rev, bool):
            raise TypeError(
                f"preference_ledger entry has non-int revision: {rev!r}"
            )
        if lo_exclusive < rev <= hi_inclusive:
            out.append(dict(entry))
    return out


def check_consistency(
    state: MutableMapping[str, Any], stage_name: str
) -> Optional[LedgerDrift]:
    """Compare ``stage_name``'s derivation revision against the current ledger.

    Returns the :class:`LedgerDrift` signal if drift was detected (and
    appends it to :data:`LEDGER_DRIFT_SIGNALS_KEY`), or ``None`` if the
    stage is untagged (B1 stub) or up-to-date.

    Raises:
        RuntimeError: If the ledger is not initialised in state, or if the
            stored derivation revision exceeds the current ledger revision
            (impossible for an append-only ledger -> pipeline invariant
            violation).
        ValueError / TypeError: For any malformed ledger / derivation state,
            surfaced straight from the underlying loaders.
    """
    if not isinstance(stage_name, str) or not stage_name:
        raise ValueError(
            f"stage_name must be a non-empty string, got {stage_name!r}"
        )

    if PREFERENCE_LEDGER_KEY not in state:
        raise RuntimeError(
            "Consistency checker invariant violated: "
            f"{PREFERENCE_LEDGER_KEY!r} not present in session state. "
            "The Preference Ledger must be initialised (R0 seed, ARCH-A3) "
            "before the pipeline starts."
        )

    current_rev = current_revision(state)
    derivations = _load_stage_derivations(state)

    if stage_name not in derivations:
        # ARCH-B1 has not tagged this stage yet. Per issue #135, emit a
        # warning but do NOT fail or signal drift -- otherwise A5 would
        # block every pipeline run until B1 lands.
        logger.warning(
            "consistency_checker: stage %r is untagged "
            "(no ledger_revision_at_derivation) -- treating as no-op "
            "until ARCH-B1 lands",
            stage_name,
        )
        return None

    from_rev, artifact_ids = _validate_derivation_entry(
        stage_name, derivations[stage_name]
    )

    if from_rev > current_rev:
        raise RuntimeError(
            "Consistency checker invariant violated: "
            f"stage {stage_name!r} derivation revision ({from_rev}) exceeds "
            f"current ledger revision ({current_rev}). The Preference "
            "Ledger is append-only; revision cannot decrease."
        )

    if from_rev == current_rev:
        logger.debug(
            "consistency_checker: stage %s up-to-date at rev=%d",
            stage_name,
            current_rev,
        )
        return None

    new_records = _records_in_range(
        state, lo_exclusive=from_rev, hi_inclusive=current_rev
    )
    drift = LedgerDrift(
        stage_name=stage_name,
        artifact_ids=artifact_ids,
        from_rev=from_rev,
        to_rev=current_rev,
        new_records=tuple(new_records),
    )
    _append_drift_signal(state, drift)
    logger.info(
        "consistency_checker: LEDGER DRIFT stage=%s from_rev=%d to_rev=%d "
        "new_records=%d artifact_ids=%d",
        stage_name,
        from_rev,
        current_rev,
        len(new_records),
        len(artifact_ids),
    )
    return drift


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def _resolve_stage_name(context: Any, state: Mapping[str, Any]) -> str:
    """Resolve the stage name for callback/tool-context invocations.

    Order of preference (mirrors Timeline Guardian and existing callbacks):

    1. ``context.agent_name`` (ADK populates this on CallbackContext and
       ToolContext).
    2. ``state['pipeline_phase']`` (set by phase-routing callbacks).
    3. ``'pipeline'`` as a last-resort default.
    """
    name = getattr(context, "agent_name", None)
    if isinstance(name, str) and name:
        return name
    phase = state.get("pipeline_phase")
    if isinstance(phase, str) and phase:
        return phase
    return "pipeline"


def after_agent_consistency_check(callback_context: Any) -> None:
    """ADK ``after_agent_callback`` -- stage-boundary consistency check.

    Runs after every ADK agent completes. Detects drift, emits a signal,
    and returns ``None`` so the pipeline continues. Invariant violations
    (missing ledger, revision decrease) raise immediately.
    """
    state = callback_context.state
    stage_name = _resolve_stage_name(callback_context, state)
    check_consistency(state, stage_name)
    return None


def before_tool_consistency_check(
    tool: Any, args: Mapping[str, Any], tool_context: Any
) -> None:
    """ADK ``before_tool_callback`` -- pre-call consistency check.

    Runs before every tool invocation. Returns ``None`` so the tool call
    proceeds; drift is a signal, not an error.
    """
    state = tool_context.state
    stage_name = _resolve_stage_name(tool_context, state)
    check_consistency(state, stage_name)
    return None


def check_consistency_at_gate(
    state: MutableMapping[str, Any], stage_name: str
) -> Optional[LedgerDrift]:
    """Gate-polling entry point (human L4 path, ARCH-H5).

    Thin alias of :func:`check_consistency` kept as a distinct symbol so
    call sites read intentionally and so future gate-specific behaviour
    (e.g. rate-limited polling) can land here without touching callers.
    """
    return check_consistency(state, stage_name)


__all__ = [
    "STAGE_DERIVATIONS_KEY",
    "LEDGER_DRIFT_SIGNALS_KEY",
    "LedgerDrift",
        "pending_drift_signals",
    "check_consistency",
    "check_consistency_at_gate",
    "after_agent_consistency_check",
    "before_tool_consistency_check",
]
