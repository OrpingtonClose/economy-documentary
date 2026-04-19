"""ARCH-G2 — Preview trigger points (diagram 9).

Four fixed trigger points that invoke :func:`server.previews.builder.
build_preview`. Each trigger is a pure state predicate — **no LLM
calls** — and each is idempotent: once a trigger has fired for a
given milestone, re-running the callback on the same state is a
no-op.

Trigger points:

1. **Pre-production audio-only preview.**  Fires once, immediately
   after the ARCH-E2 narration-reconciliation stage completes (audio
   phase finished with ``_narration_reconciliation_passed = True``)
   and before video production begins.  Shape: every video slot on
   ``V1_Video`` is still a gap, so the preview is 100 % black-card
   placeholders with real narration on ``A1_Narration``.  This is the
   canonical "pacing preview" — the reviewer hears the full
   narration + runtime with honest missing-video cards.

2. **Scene complete.**  Fires each time a scene's video + audio
   tracks all reach a terminal status (``delivered`` or ``failed``).
   Uses a per-scene ledger to fire at most once per scene_num per
   run.

3. **Act complete.**  Fires each time every scene in an act reaches
   a terminal status.  Uses a per-act ledger.

4. **Halfway milestone.**  Fires exactly once when the cumulative
   duration of completed (delivered + failed) scenes crosses 50 % of
   the scripted total duration.

None of these triggers advances any stage, mutates any artifact tag,
or clears any approval-gate state.  They read the blackboard, call
the builder, record the resulting preview on the blackboard, and
emit a dashboard SSE event via the ARCH-G3 consumer lanes.

ADK idioms (meta #122):

- Every trigger is an :func:`after_agent_callback` — same pattern as
  ``narration_reconciliation_after_agent_callback`` and the Timeline
  Guardian.  No new agent hierarchy; the triggers compose into the
  existing pipeline via the existing ``after_agent_callback`` slot.
- Cross-stage state flows through the blackboard via
  :data:`PREVIEW_LEDGER_KEY` (what fired), :data:`LATEST_PREVIEW_KEY`
  (the most recent preview path), and :data:`PREVIEW_HISTORY_KEY`
  (every preview produced by this run).
- On builder failure the trigger does **not** raise into the
  pipeline — previews are QA artifacts, not deliverables; a failed
  preview is a dashboard issue, not a reason to halt the run.  The
  exception is :class:`PreviewInconsistencyError` which IS raised:
  an inconsistent OTIO is a real pipeline invariant violation.

Spec references:

- Issue #154 (ARCH-G2 Trigger points)
- Parent #129 (ARCH-G), meta #122 (ARCH-2026)
- ``docs/ARCHITECTURE_DIAGRAMS.md`` diagram 9
"""

from __future__ import annotations

import logging
from typing import Any, MutableMapping, Optional

from previews.builder import (
    LATEST_PREVIEW_KEY,
    PREVIEW_HISTORY_KEY,
    PreviewInconsistencyError,
    PreviewManifest,
    PreviewRenderError,
    SlotKind,
    SlotPlan,
    SlotStatus,
    build_preview,
    plan_preview,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blackboard keys
# ---------------------------------------------------------------------------

#: Ledger of which triggers have already fired this run.  Shape::
#:
#:     {"pre_production": bool,
#:      "halfway": bool,
#:      "scenes": set[int],
#:      "acts": set[int]}
#:
#: A list is serialised on disk for ``scenes`` / ``acts`` since JSON
#: has no sets.  In-memory we use ``set`` for O(1) membership.
PREVIEW_LEDGER_KEY = "_preview_trigger_ledger"


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------


def _get_ledger(state: MutableMapping[str, Any]) -> dict:
    """Return the ledger, initialising if missing."""
    existing = state.get(PREVIEW_LEDGER_KEY)
    if not isinstance(existing, dict):
        existing = {}

    out = {
        "pre_production": bool(existing.get("pre_production", False)),
        "halfway": bool(existing.get("halfway", False)),
        "scenes": set(existing.get("scenes") or ()),
        "acts": set(existing.get("acts") or ()),
    }
    state[PREVIEW_LEDGER_KEY] = out
    return out


def _persist_manifest(
    state: MutableMapping[str, Any], manifest: PreviewManifest
) -> None:
    """Record ``manifest`` on the blackboard.

    Preview callers (dashboard, agent critic) read these two keys to
    find the latest preview.  Neither key is read by any
    pipeline-advancing gate — per ARCH-G1 invariant #1.
    """
    state[LATEST_PREVIEW_KEY] = manifest.preview_path
    history = state.get(PREVIEW_HISTORY_KEY)
    if not isinstance(history, list):
        history = []
    history.append(manifest.to_dict())
    state[PREVIEW_HISTORY_KEY] = history


def _run_builder_safely(
    state: MutableMapping[str, Any], trigger_reason: str
) -> Optional[PreviewManifest]:
    """Invoke :func:`build_preview` with the right error policy.

    - :class:`PreviewInconsistencyError` propagates — an inconsistent
      OTIO is a real invariant violation and the pipeline must hear
      about it (fail loud).
    - :class:`PreviewRenderError` is logged and swallowed — ffmpeg
      missing / font missing is a degraded dashboard experience,
      not a reason to halt the run.  The emission of the SSE event
      is skipped accordingly.
    """
    try:
        manifest = build_preview(state, trigger_reason=trigger_reason)
    except PreviewInconsistencyError:
        raise
    except PreviewRenderError as exc:
        logger.warning(
            "preview_triggers: render failed for %r — %s",
            trigger_reason, exc,
        )
        return None
    except Exception:  # noqa: BLE001 — never crash the pipeline for a preview
        logger.exception(
            "preview_triggers: unexpected builder error for %r",
            trigger_reason,
        )
        return None

    _persist_manifest(state, manifest)
    try:
        from previews.consumers import emit_preview_ready

        emit_preview_ready(manifest)
    except Exception:  # noqa: BLE001 — dashboard emit is best-effort
        logger.exception(
            "preview_triggers: emit_preview_ready failed for %r",
            trigger_reason,
        )
    return manifest


# ---------------------------------------------------------------------------
# Predicate helpers over the OTIO / blackboard
# ---------------------------------------------------------------------------


def _is_terminal(status: SlotStatus) -> bool:
    return status in (SlotStatus.DELIVERED, SlotStatus.FAILED)


def _scenes_completed(state: MutableMapping[str, Any]) -> set[int]:
    """Return the set of scene_nums whose video + narration slots are
    all terminal (delivered or failed).

    A scene is "complete" when every SlotPlan keyed by that
    ``scene_num`` on the V1_Video and A1_Narration tracks has status
    ``delivered`` or ``failed`` — matching the issue #154 definition
    ("video + audio + music tracks all reach ``status in ('delivered',
    'failed')``").  Music is optional: if the A2_Music track has a
    slot for the scene we require it terminal too; absent music
    tracks do not block completion.
    """
    try:
        plans = plan_preview(state)
    except PreviewInconsistencyError:
        raise
    except Exception:  # noqa: BLE001 — missing timeline → no scenes complete
        return set()

    by_scene_by_kind: dict[int, dict[SlotKind, list[SlotPlan]]] = {}
    for p in plans:
        if p.scene_num is None:
            continue
        by_scene_by_kind.setdefault(p.scene_num, {}).setdefault(
            p.kind, []
        ).append(p)

    complete: set[int] = set()
    for scene, by_kind in by_scene_by_kind.items():
        video = by_kind.get(SlotKind.VIDEO, [])
        narration = by_kind.get(SlotKind.NARRATION, [])
        music = by_kind.get(SlotKind.MUSIC, [])
        tracks_to_check = []
        if video:
            tracks_to_check.append(video)
        if narration:
            tracks_to_check.append(narration)
        if music:
            tracks_to_check.append(music)
        if not tracks_to_check:
            continue
        if all(
            all(_is_terminal(s.status) for s in track)
            for track in tracks_to_check
        ):
            complete.add(scene)
    return complete


def _scenes_to_acts(state: MutableMapping[str, Any]) -> dict[int, int]:
    """Return a mapping ``scene_num -> act_num``.

    Reads ``state["_scene_act_map"]`` if present.  Otherwise derives
    from ``state["_scenes"]`` / ``state["scenario"]`` if those
    expose ``"act"`` on each scene.  Falls back to ``{}`` when no
    structural information is available — without acts, the
    act-complete trigger simply never fires (intentionally silent
    rather than wrong).
    """
    m = state.get("_scene_act_map")
    if isinstance(m, dict):
        try:
            return {int(k): int(v) for k, v in m.items()}
        except (TypeError, ValueError):
            return {}
    scenes = state.get("_scenes") or state.get("scenes") or []
    out: dict[int, int] = {}
    if isinstance(scenes, list):
        for s in scenes:
            if not isinstance(s, dict):
                continue
            try:
                num = int(s.get("scene_num") or s.get("number") or 0)
                act = int(s.get("act") or s.get("act_num") or 0)
            except (TypeError, ValueError):
                continue
            if num and act:
                out[num] = act
    return out


def _scripted_total_and_cursor(
    state: MutableMapping[str, Any], completed: set[int]
) -> tuple[float, float]:
    """Return ``(total_sec, completed_sec)`` over the scripted plan.

    - ``total_sec`` is the sum of declared scene durations (read from
      ``state["_scripted_durations"]`` if present, else summed over
      the OTIO plan).
    - ``completed_sec`` is the sum over scenes in ``completed``.
    """
    # Preferred: caller-supplied scripted durations.
    scripted = state.get("_scripted_durations")
    if isinstance(scripted, dict) and scripted:
        total = 0.0
        done = 0.0
        for scene_str, dur_raw in scripted.items():
            try:
                scene = int(scene_str)
                dur = float(dur_raw)
            except (TypeError, ValueError):
                continue
            total += dur
            if scene in completed:
                done += dur
        return total, done

    # Fallback: derive from the OTIO plan.
    try:
        plans = plan_preview(state)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0

    by_scene: dict[int, float] = {}
    for p in plans:
        if p.scene_num is None:
            continue
        # Use narration duration as the scripted duration for the
        # scene; each scene has exactly one narration arc.
        if p.kind == SlotKind.NARRATION:
            by_scene[p.scene_num] = by_scene.get(p.scene_num, 0.0) + p.duration_sec
    total = sum(by_scene.values())
    done = sum(dur for scene, dur in by_scene.items() if scene in completed)
    return total, done


# ---------------------------------------------------------------------------
# Pure trigger predicates (public — used by tests)
# ---------------------------------------------------------------------------


def pre_production_predicate(state: MutableMapping[str, Any]) -> bool:
    """Return True iff the pre-production preview should fire now.

    Condition: the ARCH-E2 narration reconciliation gate is set to
    True (audio phase complete, pacing reconciled) AND this trigger
    has not fired yet this run.
    """
    if _get_ledger(state)["pre_production"]:
        return False
    return bool(state.get("_narration_reconciliation_passed", False))


def scene_complete_predicates(
    state: MutableMapping[str, Any],
) -> set[int]:
    """Return the set of scene_nums that are newly complete.

    Newly complete = every V1/A1 slot for the scene is terminal AND
    the scene_num is not already recorded in the ledger.
    """
    ledger = _get_ledger(state)
    return _scenes_completed(state) - ledger["scenes"]


def act_complete_predicates(
    state: MutableMapping[str, Any],
) -> set[int]:
    """Return the set of act_nums that are newly complete."""
    ledger = _get_ledger(state)
    completed = _scenes_completed(state)
    scenes_to_acts = _scenes_to_acts(state)
    if not scenes_to_acts:
        return set()

    by_act: dict[int, set[int]] = {}
    for scene, act in scenes_to_acts.items():
        by_act.setdefault(act, set()).add(scene)

    out: set[int] = set()
    for act, scenes in by_act.items():
        if act in ledger["acts"]:
            continue
        if scenes.issubset(completed):
            out.add(act)
    return out


def halfway_predicate(state: MutableMapping[str, Any]) -> bool:
    """Return True iff the halfway milestone has just been crossed.

    Crossing the 50 % boundary of scripted scene-duration is a
    one-shot event: once fired, the ledger suppresses re-firing even
    if scenes regress back below 50 %.
    """
    ledger = _get_ledger(state)
    if ledger["halfway"]:
        return False
    completed = _scenes_completed(state)
    total, done = _scripted_total_and_cursor(state, completed)
    if total <= 0:
        return False
    return done / total >= 0.5


# ---------------------------------------------------------------------------
# Public callback wiring — ADK after_agent_callback style
# ---------------------------------------------------------------------------


def _state_from_cbctx(callback_context) -> Optional[MutableMapping[str, Any]]:
    state = getattr(callback_context, "state", None)
    return state if isinstance(state, MutableMapping) else None


def pre_production_preview_after_agent_callback(callback_context) -> None:
    """Fire the pre-production (audio-only) preview at the narration
    reconciliation boundary.

    Runs only during the ``audio`` phase; same phase guard as
    ``narration_reconciliation_after_agent_callback``.  Calling this
    from any other phase is a no-op.
    """
    state = _state_from_cbctx(callback_context)
    if state is None:
        return None

    phase = state.get("pipeline_phase", "")
    if phase and phase != "audio":
        return None

    if pre_production_predicate(state):
        manifest = _run_builder_safely(state, trigger_reason="pre_production")
        if manifest is not None:
            _get_ledger(state)["pre_production"] = True
    return None


def scene_complete_preview_after_agent_callback(callback_context) -> None:
    """Fire a preview each time a scene reaches terminal status.

    Scope: runs during ``production`` and later phases.  Intentionally
    permissive — production agents can call this repeatedly as each
    scene's video track fills in.
    """
    state = _state_from_cbctx(callback_context)
    if state is None:
        return None
    _fire_scene_triggers(state)
    return None


def act_complete_preview_after_agent_callback(callback_context) -> None:
    state = _state_from_cbctx(callback_context)
    if state is None:
        return None
    _fire_act_triggers(state)
    return None


def halfway_preview_after_agent_callback(callback_context) -> None:
    state = _state_from_cbctx(callback_context)
    if state is None:
        return None
    _fire_halfway_trigger(state)
    return None


def preview_triggers_after_agent_callback(callback_context) -> None:
    """All-in-one callback — fires whichever triggers are ready.

    Composition-friendly: can be chained into any stage's existing
    ``after_agent_callback`` list.  The individual callbacks above
    remain available for callers that want a narrower binding.
    """
    state = _state_from_cbctx(callback_context)
    if state is None:
        return None

    # Pre-production only fires during / right after the audio phase.
    phase = state.get("pipeline_phase", "")
    if phase in ("", "audio") and pre_production_predicate(state):
        manifest = _run_builder_safely(state, trigger_reason="pre_production")
        if manifest is not None:
            _get_ledger(state)["pre_production"] = True

    _fire_scene_triggers(state)
    _fire_act_triggers(state)
    _fire_halfway_trigger(state)
    return None


def _fire_scene_triggers(state: MutableMapping[str, Any]) -> list[PreviewManifest]:
    manifests: list[PreviewManifest] = []
    for scene in sorted(scene_complete_predicates(state)):
        manifest = _run_builder_safely(
            state, trigger_reason=f"scene_{scene:03d}_complete"
        )
        if manifest is not None:
            _get_ledger(state)["scenes"].add(scene)
            manifests.append(manifest)
    return manifests


def _fire_act_triggers(state: MutableMapping[str, Any]) -> list[PreviewManifest]:
    manifests: list[PreviewManifest] = []
    for act in sorted(act_complete_predicates(state)):
        manifest = _run_builder_safely(
            state, trigger_reason=f"act_{act:03d}_complete"
        )
        if manifest is not None:
            _get_ledger(state)["acts"].add(act)
            manifests.append(manifest)
    return manifests


def _fire_halfway_trigger(
    state: MutableMapping[str, Any],
) -> Optional[PreviewManifest]:
    if not halfway_predicate(state):
        return None
    manifest = _run_builder_safely(state, trigger_reason="halfway_milestone")
    if manifest is not None:
        _get_ledger(state)["halfway"] = True
    return manifest


__all__ = [
    "PREVIEW_LEDGER_KEY",
    "act_complete_predicates",
    "act_complete_preview_after_agent_callback",
    "halfway_predicate",
    "halfway_preview_after_agent_callback",
    "pre_production_predicate",
    "pre_production_preview_after_agent_callback",
    "preview_triggers_after_agent_callback",
    "scene_complete_predicates",
    "scene_complete_preview_after_agent_callback",
]
