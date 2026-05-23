"""
AG-UI OTIO slot-state bridge and drilldown layer.

Extracted from the monolithic agui.py into a focused module.  This
module owns:

* **ARCH-H1 slot-state bridge** — translates ``ArtifactEvent`` transitions
  into ``slot_state`` SSE events so the OTIO centrepiece dashboard can
  update its three canonical tracks (V1_Video / A1_Narration / A2_Music)
  without polling.

* **ARCH-H2 OTIO authoritative flip** — emits the ``otio_authoritative``
  event when the timeline crystallises.

* **OTIO state endpoint** — ``GET /agui/otio/state`` returns the full
  centrepiece timeline view.

* **Slot detail endpoints** — thumbnail, waveform, and detail for a
  single slot.

* **UI-04 slot drilldown** — ``GET /api/slots/{slot_id}/full`` and
  ``GET /api/reasoning/raw`` back the right-rail slot panel.

* **DESIGN-07 / DESIGN-08** — cost preview (``POST /agui/estimate_directive``)
  and rewind-to-stage (``POST /agui/rewind_to_stage``).

This module does NOT contain any SSE event bus code or approval gate
code — those live in their own modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agui_events import (
    emit_agui_event,
    get_feedback_store,
)
import agui_events  # for runtime agui_events._OUTPUT_DIR access (monkeypatching compat)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agui", tags=["agui"])
api_router = APIRouter(prefix="/api", tags=["slot-bridge"])


# ---------------------------------------------------------------------------
# ARCH-H1 slot-state bridge
# ---------------------------------------------------------------------------
#
# The centrepiece OTIO dashboard models each slot as
# ``{track}:{scene_num}:{phrase_idx}``.  Artifacts flowing through the
# existing FeedbackStore carry scene/phrase metadata; we translate every
# ``ArtifactEvent`` into a ``slot_state`` SSE event so the frontend never
# has to poll to learn that a slot changed state.
_ARTIFACT_TYPE_TO_TRACK = {
    "video_clip": "V1_Video",
    "narration": "A1_Narration",
    "music": "A2_Music",
}

_STATUS_TO_SLOT_STATE = {
    "generating": "in_progress",
    "regenerating": "in_progress",
    "pending_review": "delivered",
    "approved": "delivered",
    "rejected": "failed",
}


# _emit_slot_state_from_artifact is imported from agui_events
# (canonical home — FeedbackStore.register_artifact calls it directly)
# emit_otio_authoritative is also imported from agui_events.


# ---------------------------------------------------------------------------
# ARCH-H1 / ARCH-H2 / ARCH-H3 — OTIO centrepiece timeline endpoints
# ---------------------------------------------------------------------------
#
# These endpoints back the new dashboard centrepiece: an authoritative
# (or draft-with-reconciliation-overlay) OTIO timeline rendered on three
# canonical tracks.  All endpoints are pure read models — no mutation —
# and a dedicated SSE stream delivers slot-state transitions so the UI
# never polls.


@router.get("/otio/state")
async def get_otio_state_view():
    """Return the centrepiece OTIO view.

    Response shape::

        {
          "state": "draft"|"authoritative",
          "total_duration_sec": float,
          "tracks": [
            {"name": "V1_Video", "kind": "video", "slots": [...], "total_slots": N},
            {"name": "A1_Narration", "kind": "audio", "slots": [...], ...},
            {"name": "A2_Music", "kind": "audio", "slots": [...], ...}
          ],
          "reconciliation": [...],  // empty when state=="authoritative"
          "source_file": "/tmp/documentary-pipeline/timelines/<file>.otio"
        }
    """
    from otio_timeline_model import build_timeline_view

    _store = get_feedback_store()
    artifacts = _store.get_all_artifacts()
    view = build_timeline_view(agui_events._OUTPUT_DIR, feedback_artifacts=artifacts)
    return JSONResponse(view.to_dict())


@router.get("/slots/{slot_id}/detail")
async def get_slot_detail(slot_id: str):
    """Aggregate artifact history, QA, reasoning, ledger, rung, preview.

    Pure read-only; never mutates state.  See
    :mod:`server.slot_detail_model` for the underlying builder.
    """
    from otio_timeline_model import parse_slot_id
    from slot_detail_model import build_slot_detail

    try:
        parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _store = get_feedback_store()
    artifacts = _store.get_all_artifacts()
    detail = build_slot_detail(
        slot_id,
        agui_events._OUTPUT_DIR,
        feedback_artifacts=artifacts,
        state=None,
    )
    return JSONResponse(detail.to_dict())


@router.get("/slots/{slot_id}/thumbnail")
async def get_slot_thumbnail(slot_id: str):
    """Return the first-frame thumbnail for a delivered video slot.

    Best-effort.  If ``ffmpeg`` is on PATH and a delivered MP4 exists for
    the slot we extract a single frame on-demand (cached on disk next to
    the MP4).  Otherwise we return 404.
    """
    from fastapi.responses import FileResponse
    from otio_timeline_model import (
        TRACK_V1_VIDEO,
        parse_slot_id,
    )

    try:
        track, scene, phrase = parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if track != TRACK_V1_VIDEO:
        return JSONResponse({"error": "not a video slot"}, status_code=400)

    mp4_path = os.path.join(
        agui_events._OUTPUT_DIR,
        "video",
        f"scene_{scene:03d}_phrase_{phrase:03d}.mp4",
    )
    if not os.path.exists(mp4_path):
        return JSONResponse({"error": "no delivered clip"}, status_code=404)
    thumb_path = mp4_path.replace(".mp4", "_thumb.jpg")
    if not os.path.exists(thumb_path):
        try:
            import subprocess
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", "0.1", "-i", mp4_path,
                    "-frames:v", "1", "-q:v", "4", thumb_path,
                ],
                check=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("thumbnail extraction failed for %s: %s", mp4_path, exc)
            return JSONResponse({"error": "thumbnail unavailable"}, status_code=404)
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.get("/slots/{slot_id}/waveform")
async def get_slot_waveform(slot_id: str, samples: int = 240):
    """Return a downsampled RMS envelope for the WAV backing this slot."""
    from otio_timeline_model import (
        TRACK_A1_NARRATION,
        TRACK_A2_MUSIC,
        parse_slot_id,
    )

    try:
        track, scene, phrase = parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if track == TRACK_A1_NARRATION:
        wav_path = os.path.join(
            agui_events._OUTPUT_DIR, "audio", f"scene_{scene:03d}_phrase_{phrase:03d}.wav"
        )
    elif track == TRACK_A2_MUSIC:
        wav_path = os.path.join(
            agui_events._OUTPUT_DIR, "music", f"scene_{scene:03d}_phrase_{phrase:03d}.wav"
        )
    else:
        return JSONResponse({"error": "not an audio slot"}, status_code=400)

    if not os.path.exists(wav_path):
        return JSONResponse({"error": "no delivered audio"}, status_code=404)
    samples = max(16, min(samples, 2000))

    try:
        import wave
        with wave.open(wav_path, "rb") as wf:
            n_frames = wf.getnframes()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            raw = wf.readframes(n_frames)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"wav read failed: {exc}"}, status_code=500)

    # Cheap RMS downsample without numpy dependency (ARCH-H1 doesn't need it).
    import struct
    if sampwidth == 2:
        fmt = "<" + "h" * (len(raw) // 2)
    elif sampwidth == 4:
        fmt = "<" + "i" * (len(raw) // 4)
    else:
        return JSONResponse({"error": "unsupported sample width"}, status_code=415)
    try:
        all_samples = struct.unpack(fmt, raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"decode failed: {exc}"}, status_code=500)
    # Fold channels down to mono by averaging interleaved samples.
    if n_channels > 1:
        mono = [
            sum(all_samples[i : i + n_channels]) / n_channels
            for i in range(0, len(all_samples), n_channels)
        ]
    else:
        mono = list(all_samples)

    if not mono:
        return JSONResponse({"samples": [], "duration_sec": 0})

    bucket = max(1, len(mono) // samples)
    peak = float(1 << (8 * sampwidth - 1))
    envelope = []
    for i in range(0, len(mono), bucket):
        chunk = mono[i : i + bucket]
        if not chunk:
            continue
        mx = max(abs(v) for v in chunk)
        envelope.append(round(mx / peak, 4))
    return JSONResponse({
        "samples": envelope[:samples],
        "duration_sec": n_frames / framerate if framerate else 0,
    })


# ---------------------------------------------------------------------------
# UI-04 — full slot drilldown (#189 / #201 / #204)
# ---------------------------------------------------------------------------
#
# Two endpoints back the rebuilt right-rail slot panel:
#
# * ``GET /api/slots/{slot_id}/full``  — single aggregation call that
#   returns slot + takes + critiques + QA + artifacts + ledger records +
#   reasoning trace preview. Uses :func:`slot_detail_model.build_slot_detail`
#   as the foundation and enriches with per-artifact critique store reads,
#   scope-resolved ledger records (``records_applying_to_slot``), and a
#   preview of the latest reasoning digests scoped to the slot.
#
# * ``GET /api/reasoning/raw?slot_id=...`` — raw reasoning events filtered
#   to a single slot, for the advanced-mode virtualised subsection. The
#   slot-qualifier substring scan mirrors
#   :func:`slot_detail_model._reasoning_digests_for_slot`.


# Long track name -> short ``_TRACK_SHORT`` form, kept local so the
# aggregator does not import the OTIO model just for this.
_SLOT_TRACK_SHORT: dict[str, str] = {
    "V1_Video": "V1",
    "A1_Narration": "A1",
    "A2_Music": "A2",
}


# Track -> critique store ``artifact_type``. The critique layer uses a
# different taxonomy than the feedback store (see ``critique/record.py``):
# ``clip`` / ``audio`` / ``music`` instead of ``video_clip`` / ``narration``
# / ``music``. We probe both so we pick up records regardless of which
# producer wrote them.
_SLOT_CRITIQUE_TYPES: dict[str, tuple[str, ...]] = {
    "V1_Video": ("clip", "video_clip"),
    "A1_Narration": ("audio", "narration"),
    "A2_Music": ("audio", "music"),
}


def _slot_reasoning_match_terms(
    short_track: str, scene_num: int, phrase_idx: int
) -> tuple[list[str], str]:
    """Return (substring needles, track keyword) for slot-aware matching.

    The reasoning store is free-form text — we do a case-insensitive
    substring scan against ``content`` and ``metadata`` JSON using the
    same conventions the digest engine tags with (see
    :func:`slot_detail_model._reasoning_digests_for_slot`).
    """
    needles = [
        f"{short_track}:{scene_num}:{phrase_idx}",
        f"scene {scene_num} phrase {phrase_idx}",
        f"scene_{scene_num}_phrase_{phrase_idx}",
        f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}",
        f"s{scene_num:03d}_p{phrase_idx:03d}",
        f"s{scene_num}p{phrase_idx}",
    ]
    track_keyword = {
        "V1": "video",
        "A1": "narration",
        "A2": "music",
    }.get(short_track, "")
    return needles, track_keyword


def _load_dashboard_blackboard_state() -> dict:
    """Best-effort snapshot of the shared dashboard blackboard.

    The slot-full endpoint is read-only; we never mutate the blackboard.
    If the file is missing or unreadable we return an empty dict — the
    downstream helpers (``records_applying_to_slot``) handle that
    gracefully by returning an empty record list.
    """
    try:
        from dashboard_directives import _load_blackboard
        return dict(_load_blackboard())
    except Exception as exc:  # noqa: BLE001
        logger.debug("slot/full: blackboard unavailable: %s", exc)
        return {}


def _slot_view_for(slot_id: str) -> dict:
    """Return the canonical ``SlotView`` dict for ``slot_id``.

    Pulls from the same ``build_timeline_view`` used by
    ``/agui/otio/state`` so the panel header matches the timeline
    exactly. When the slot is not yet on the timeline we synthesise a
    minimal stub so the panel can still render (header + sections that
    exist).
    """
    from otio_timeline_model import build_timeline_view, parse_slot_id

    track, scene, phrase = parse_slot_id(slot_id)
    _store = get_feedback_store()
    try:
        artifacts = _store.get_all_artifacts()
        view = build_timeline_view(agui_events._OUTPUT_DIR, feedback_artifacts=artifacts)
    except Exception as exc:  # noqa: BLE001
        logger.debug("slot/full: timeline view unavailable: %s", exc)
        view = None

    if view is not None:
        for track_view in view.tracks:
            if track_view.name != track:
                continue
            for slot in track_view.slots:
                if slot.slot_id == slot_id:
                    return slot.to_dict()

    # Fallback: synthesise a minimal stub.
    return {
        "slot_id": slot_id,
        "track": track,
        "scene_num": scene,
        "phrase_idx": phrase,
        "start_sec": 0.0,
        "duration_sec": 0.0,
        "status": "pending",
        "label": "",
        "preview_url": "",
        "thumbnail_url": "",
        "waveform_url": "",
        "failure_reason": "",
        "rung": "",
        "scripted_duration_sec": None,
        "measured_duration_sec": None,
        "metadata": {},
    }


def _critique_record_for_slot(track: str, scene_num: int, phrase_idx: int) -> dict:
    """Read the critique-store record for this slot, across id conventions.

    Returns the record's ``to_dict()`` form plus the ``(artifact_type,
    artifact_id)`` pair we matched, so the caller can surface which
    taxonomy hit. Empty dict when no record is found.
    """
    try:
        from critique.store import get_critique_store
    except Exception:  # noqa: BLE001
        return {}
    try:
        store = get_critique_store()
    except Exception:  # noqa: BLE001
        return {}

    candidate_ids = (
        f"s{scene_num:03d}_p{phrase_idx:03d}",
        f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}",
        f"{scene_num}_{phrase_idx}",
    )
    for artifact_type in _SLOT_CRITIQUE_TYPES.get(track, ()):
        for aid in candidate_ids:
            try:
                record = store.read(artifact_type, aid)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                continue
            if record is None:
                continue
            try:
                payload = record.to_dict()
            except Exception:  # noqa: BLE001
                continue
            payload["_matched_artifact_type"] = artifact_type
            payload["_matched_artifact_id"] = aid
            return payload
    return {}


def _takes_from_detail(
    detail_dict: dict, slot_view: dict
) -> list[dict]:
    """Normalise artifact history into the ``takes`` response shape.

    Each take carries ``revision``, ``outcome`` (approved / rejected /
    pending / failed / generating), the preview url, and — when the
    pipeline has stamped one — the ledger revision at derivation.
    """
    _OUTCOME = {
        "approved": "accepted",
        "rejected": "rejected",
        "pending_review": "pending",
        "regenerating": "regenerating",
        "generating": "generating",
        "failed": "failed",
    }
    history = list(detail_dict.get("artifact_history") or [])
    out: list[dict] = []
    for idx, art in enumerate(history):
        status = str(art.get("status", ""))
        out.append(
            {
                "revision": idx,
                "artifact_id": art.get("id", ""),
                "status": status,
                "outcome": _OUTCOME.get(status, status or "unknown"),
                "timestamp": art.get("timestamp"),
                "preview_url": art.get("preview_url", ""),
                "b2_url": (art.get("metadata") or {}).get("b2_url", "") or art.get("preview_url", ""),
                "qa_scores": art.get("qa_scores", {}),
                "ledger_revision_at_derivation": art.get(
                    "ledger_revision_at_derivation"
                ),
            }
        )
    if not out and slot_view.get("preview_url"):
        out.append(
            {
                "revision": 0,
                "artifact_id": "",
                "status": slot_view.get("status", ""),
                "outcome": _OUTCOME.get(
                    slot_view.get("status", ""),
                    slot_view.get("status", "unknown"),
                ),
                "timestamp": None,
                "preview_url": slot_view.get("preview_url", ""),
                "b2_url": slot_view.get("preview_url", ""),
                "qa_scores": {},
                "ledger_revision_at_derivation": None,
            }
        )
    return out


def _artifacts_from_slot(slot_view: dict, takes: list[dict]) -> list[dict]:
    """Normalised list of media artifacts + thumbnails + waveforms."""
    entries: list[dict] = []
    if slot_view.get("preview_url"):
        entries.append(
            {
                "kind": "preview",
                "url": slot_view["preview_url"],
                "label": "Current preview",
            }
        )
    if slot_view.get("thumbnail_url"):
        entries.append(
            {
                "kind": "thumbnail",
                "url": slot_view["thumbnail_url"],
                "label": "Thumbnail",
            }
        )
    if slot_view.get("waveform_url"):
        entries.append(
            {
                "kind": "waveform",
                "url": slot_view["waveform_url"],
                "label": "Waveform",
            }
        )
    for take in takes:
        url = take.get("b2_url") or take.get("preview_url")
        if not url:
            continue
        entries.append(
            {
                "kind": "take",
                "url": url,
                "label": f"Revision {take.get('revision')}",
                "revision": take.get("revision"),
                "outcome": take.get("outcome"),
            }
        )
    return entries


@api_router.get("/slots/{slot_id}/full")
async def get_slot_full(slot_id: str):
    """Aggregation endpoint backing the UI-04 slot drilldown panel (#201).

    Single request returns everything the right-rail panel needs:

    * ``slot`` — header info (scene/phrase/duration/status/label).
    * ``takes`` — ordered artifact revisions with outcome and B2 url.
    * ``critiques`` — per-critic rationale from the critique store.
    * ``qa_results`` — numerical QA verdicts (LUFS, motion stability, ...).
    * ``artifacts`` — flat list of media URLs (preview / waveform / takes).
    * ``ledger_records`` — resolved via
      :func:`callbacks.preference_ledger.records_applying_to_slot`.
    * ``reasoning_trace_preview`` — last 20 digests that mention the slot.

    Pure read-only. Never mutates any store or state.
    """
    from otio_timeline_model import parse_slot_id
    from slot_detail_model import build_slot_detail

    try:
        track, scene_num, phrase_idx = parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Base slot view (header info).
    slot_view = _slot_view_for(slot_id)

    # Per-artifact detail (history + digests + ledger scope probe + rung).
    state = _load_dashboard_blackboard_state()
    _store = get_feedback_store()
    try:
        artifacts_store = _store.get_all_artifacts()
    except Exception:  # noqa: BLE001
        artifacts_store = []

    try:
        detail = build_slot_detail(
            slot_id,
            agui_events._OUTPUT_DIR,
            feedback_artifacts=artifacts_store,
            state=state or None,
        )
        detail_dict = detail.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot/full: build_slot_detail failed for %s: %s", slot_id, exc)
        detail_dict = {
            "artifact_history": [],
            "qa_verdicts": [],
            "reasoning_digests": [],
            "ledger_records": [],
            "current_rung": {},
            "latest_preview": {},
        }

    # Takes + artifacts from the detail + slot view.
    takes = _takes_from_detail(detail_dict, slot_view)
    artifacts = _artifacts_from_slot(slot_view, takes)

    # Critique-store record — one per (type, id). We surface the per-critic
    # ``critiques`` list and the deterministic ``qa_results`` list separately.
    critique_record = _critique_record_for_slot(track, scene_num, phrase_idx)
    critiques = list(critique_record.get("critiques") or [])
    qa_results = list(critique_record.get("qa_results") or [])
    # Fallback: if the critique store is empty, surface any QA verdicts the
    # stylistic QA pipeline emitted directly (these are already in detail).
    if not qa_results:
        qa_results = list(detail_dict.get("qa_verdicts") or [])

    # Ledger records scoped to the slot via #202's helper.
    try:
        from callbacks.preference_ledger import records_applying_to_slot
        resolved = records_applying_to_slot(state, slot_id)
        ledger_records = [r.to_dict() for r in resolved]
    except Exception as exc:  # noqa: BLE001
        logger.debug("slot/full: ledger resolution failed for %s: %s", slot_id, exc)
        ledger_records = []

    # Reasoning trace preview — last 20 digest entries scoped to slot.
    reasoning_trace_preview = list(detail_dict.get("reasoning_digests") or [])[-20:]

    payload = {
        "slot": slot_view,
        "takes": takes,
        "critiques": critiques,
        "qa_results": qa_results,
        "artifacts": artifacts,
        "ledger_records": ledger_records,
        "reasoning_trace_preview": reasoning_trace_preview,
        # Supplementary context the panel may render even when sections
        # above are empty (current rung, latest assembly preview).
        "current_rung": detail_dict.get("current_rung") or {},
        "latest_preview": detail_dict.get("latest_preview") or {},
    }
    return JSONResponse(payload)


@api_router.get("/reasoning/raw")
async def get_reasoning_raw_for_slot(
    slot_id: str | None = None,
    limit: int = 100,
    since: float | None = None,
):
    """Raw reasoning entries, optionally filtered to a single slot (#204).

    Query params:

    * ``slot_id`` — when set, only entries whose content or metadata
      contains a recognised slot-qualifier substring are returned.
    * ``limit`` — max rows (default 100, clamped to 1000). Designed so
      the frontend's virtualised list can request up to 1000 entries in
      one fetch without chunking.
    * ``since`` — only rows after this Unix timestamp (for polling).

    The response shape matches ``/agui/reasoning/raw`` so the frontend
    can share a single ``RawTrace`` type: ``{traces: [...], count: N}``.
    Each trace carries ``id``, ``timestamp``, ``event_type``,
    ``agent_name``, ``model``, ``content``, ``tokens_in``, ``tokens_out``,
    ``metadata``.
    """
    limit = max(1, min(int(limit or 100), 1000))

    filter_terms: list[str] = []
    track_keyword = ""
    if slot_id:
        try:
            from otio_timeline_model import parse_slot_id
            track, scene_num, phrase_idx = parse_slot_id(slot_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        short = _SLOT_TRACK_SHORT.get(track, track[:2])
        filter_terms, track_keyword = _slot_reasoning_match_terms(
            short, scene_num, phrase_idx
        )

    # reasoning_trace plugin removed (ADK pipeline deleted)
    return JSONResponse(
        {"traces": [], "count": 0, "error": "reasoning_trace plugin removed"},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# DESIGN-07 (#259) / DESIGN-08 (#260): cost preview + rewind-to-stage
# ---------------------------------------------------------------------------
#
# These endpoints back the shadcn ``Dialog`` cost preview and the
# ``DropdownMenu``-based rewind affordance in the dashboard.  They are
# deliberately small wrappers over existing pipeline state so the UI
# flows are usable today; the per-stage cost math is a placeholder
# (documented below) and the rewind handler delegates to the existing
# halt + preference-ledger plumbing rather than introducing a parallel
# rollback path.

# Plain-English stage labels surfaced by DESIGN-08.  Keep the backend
# identifiers aligned with ``server/dashboard/sse.py::KNOWN_PIPELINE_STAGES``
# -- DESIGN-08 forbids introducing new stage names.
_REWIND_STAGE_LABELS: dict[str, str] = {
    "scenario": "scenario",
    "visual_director": "visuals",
    "audio": "narration",
    "video": "production",
    "assembly": "final touches",
}

# TODO(DESIGN-07 backend): the cost numbers below are a conservative
# client-side-equivalent placeholder.  Replace with a proper estimate
# that reads the current blackboard (scene count, worker availability,
# historical runtime) once the accounting hooks land.  Until then the
# UI shows the same rough numbers the client-side fallback would show.
_PER_STAGE_MINUTES: dict[str, float] = {
    "scenario": 1.0,
    "visual_director": 3.0,
    "audio": 5.0,
    "video": 8.0,
    "assembly": 2.0,
}
_PER_STAGE_DOLLARS: dict[str, float] = {
    "scenario": 0.05,
    "visual_director": 0.2,
    "audio": 0.4,
    "video": 1.2,
    "assembly": 0.1,
}
_FALLBACK_STAGE_MINUTES = 7.0
_FALLBACK_STAGE_DOLLARS = 0.7


def _estimate_for(stage: Optional[str], *, scene_scoped: bool) -> dict:
    """Compute a placeholder cost estimate for the directive endpoint."""
    stages = 1 if scene_scoped else 3
    minutes_per_stage = (
        _PER_STAGE_MINUTES.get(stage, _FALLBACK_STAGE_MINUTES)
        if stage
        else _FALLBACK_STAGE_MINUTES
    )
    dollars_per_stage = (
        _PER_STAGE_DOLLARS.get(stage, _FALLBACK_STAGE_DOLLARS)
        if stage
        else _FALLBACK_STAGE_DOLLARS
    )
    eta_minutes = max(1, round(stages * minutes_per_stage))
    dollars = round(stages * dollars_per_stage, 2)
    stage_label = "scene"
    unit = stage_label if stages == 1 else f"{stage_label}s"
    summary = (
        f"This will rerun {stages} {unit}, "
        f"add about {eta_minutes} minutes, "
        f"and cost about ${dollars:.2f}."
    )
    return {
        "stages": stages,
        "stage_label": stage_label,
        "eta_minutes": eta_minutes,
        "dollars": dollars,
        "summary": summary,
        "note": "Backend estimate is a placeholder (DESIGN-07 TODO).",
    }


@router.post("/estimate_directive")
async def estimate_directive(request: Request) -> JSONResponse:
    """Return a plain-English cost estimate for a directive-style action.

    Accepts the same shape the intervention bar / rewind dropdown already
    builds: ``{"directive": str?, "slot_context": dict?, "stage": str?,
    "action": str?}``.  Everything is optional -- when nothing is
    supplied we assume a pipeline-wide change (three stages).

    This is a deliberate placeholder.  The numbers come from a static
    per-stage table (see ``_PER_STAGE_MINUTES`` / ``_PER_STAGE_DOLLARS``
    above) rather than from live blackboard state; the TODO comment on
    that table is the spec for the real implementation.  The shape of
    the response (``stages``, ``stage_label``, ``eta_minutes``,
    ``dollars``, ``summary``) matches :class:`CostEstimate` on the
    frontend so the dialog can render without any transformation.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- empty/malformed bodies are fine
        body = {}
    if not isinstance(body, dict):
        body = {}

    slot_context = body.get("slot_context")
    scene_scoped = False
    if isinstance(slot_context, dict):
        scene_scoped = any(
            slot_context.get(k) is not None
            for k in ("scene_num", "scene_id", "clip_id", "voice_block_id")
        )

    stage = body.get("stage")
    if not isinstance(stage, str) or not stage:
        stage = None

    return JSONResponse(_estimate_for(stage, scene_scoped=scene_scoped))


@router.post("/rewind_to_stage")
async def rewind_to_stage(request: Request) -> JSONResponse:
    """Rewind the pipeline to an earlier stage.

    Body: ``{"stage": str, "reviewer": str?, "reason": str?}``.

    The endpoint is a thin wrapper that defers to the existing halt +
    preference-ledger plumbing rather than introducing a parallel
    rollback path:

    1. Engage the halt flag via :func:`set_halt_requested` with a
       plain-English reason ("Rewind to narration") so the approval-gate
       poll loop pauses the pipeline at the next safe checkpoint.
    2. Append a synthetic rewind directive via
       :func:`_append_rewind_directive` scoped to the target stage so
       downstream consistency-checker / A6 re-manifestation treats it
       as drift on that stage.
    3. Emit a ``rewind_requested`` AG-UI event so the dashboard can
       surface a toast.

    Returns ``{"status": "accepted", "stage": str, "halt_state": {...}}``
    on success, HTTP 400 on an unknown stage, HTTP 500 on a ledger /
    halt-state write failure.  On either failure the halt flag stays
    unchanged so the reviewer can retry.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"error": "request body must be JSON"}, status_code=400
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "request body must be a JSON object"}, status_code=400
        )

    stage = body.get("stage")
    if not isinstance(stage, str) or not stage:
        return JSONResponse(
            {"error": "'stage' must be a non-empty string"}, status_code=400
        )
    if stage not in _REWIND_STAGE_LABELS:
        return JSONResponse(
            {
                "error": (
                    f"Unknown stage {stage!r}. Must be one of "
                    f"{sorted(_REWIND_STAGE_LABELS)}."
                )
            },
            status_code=400,
        )

    reviewer = body.get("reviewer") or "dashboard-user"
    reason = body.get("reason") or (
        f"Rewind to {_REWIND_STAGE_LABELS[stage]}"
    )

    try:
        # Deferred import keeps ``agui`` importable during tests that
        # stub out ``dashboard_directives``' disk-backed state.
        from dashboard_directives import (  # type: ignore
            _append_rewind_directive,
            _read_halt_state,
            set_halt_requested,
        )
    except Exception as exc:  # noqa: BLE001 -- surface wiring failures loud
        logger.exception("rewind_to_stage: dashboard_directives import failed")
        return JSONResponse(
            {"error": f"rewind wiring unavailable: {exc}"}, status_code=500,
        )

    try:
        set_halt_requested(
            reviewer=str(reviewer),
            reason=str(reason),
            at_stage=stage,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rewind_to_stage: halt flag write failed")
        return JSONResponse(
            {"error": f"halt flag write failed: {exc}"}, status_code=500,
        )

    try:
        await asyncio.to_thread(_append_rewind_directive, stage)
    except Exception:  # noqa: BLE001 -- directive append is best-effort
        logger.exception(
            "rewind_to_stage: synthetic rewind directive append failed "
            "(halt flag is still engaged; reviewer can retry)"
        )

    try:
        halt_state = _read_halt_state()
    except Exception:  # noqa: BLE001
        halt_state = {}

    emit_agui_event(
        "rewind_requested",
        {
            "stage": stage,
            "stage_label": _REWIND_STAGE_LABELS[stage],
            "reviewer": reviewer,
            "reason": reason,
        },
    )

    return JSONResponse(
        {
            "status": "accepted",
            "stage": stage,
            "stage_label": _REWIND_STAGE_LABELS[stage],
            "halt_state": halt_state,
        }
    )
