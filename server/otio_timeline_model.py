"""
OTIO timeline centrepiece read models (ARCH-H1 / ARCH-H2 / ARCH-H3).

The dashboard now treats the OTIO timeline as the single source of truth for
everything the human sees: three horizontal tracks (``V1_Video``,
``A1_Narration``, ``A2_Music``) drawn to scale against real time, with each
slot showing its lifecycle state (pending / in-progress / delivered / failed)
and, while the OTIO is still ``draft``, a reconciliation overlay comparing
scripted vs WhisperX-measured durations per narration block.

This module is a *pure read model*.  It does not mutate any blackboard state,
does not write files, does not call into the orchestrator.  It only:

* Loads the latest OTIO file under ``PIPELINE_OUTPUT_DIR``.
* Assembles per-slot records for the three canonical tracks.
* Overlays artifact status from the AG-UI feedback store and on-disk
  ``_status.json`` files produced by the video orchestrator.
* Overlays narration reconciliation metrics (scripted vs measured) read
  from the audio stage's reconciliation report on disk.

The HTTP surface lives in :mod:`server.agui`.  Tests live under
``server/tests/test_otio_timeline_read_model.py`` and
``server/tests/test_slot_detail_read_model.py``.

Invariants enforced by the read model:

1. Returned slot windows are *scale-accurate*: ``start_sec`` and
   ``duration_sec`` come straight from the OTIO clip ranges, never
   estimated.
2. No mutation — the read functions never write to disk or state.
3. Slot IDs are stable across revisions: ``{track}:{scene_num}:{phrase_idx}``.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Track + slot identity
# ---------------------------------------------------------------------------

TRACK_V1_VIDEO = "V1_Video"
TRACK_A1_NARRATION = "A1_Narration"
TRACK_A2_MUSIC = "A2_Music"

CANONICAL_TRACKS = (TRACK_V1_VIDEO, TRACK_A1_NARRATION, TRACK_A2_MUSIC)

# Short aliases used in slot IDs (keep IDs compact for URLs + logs).
_TRACK_SHORT = {
    TRACK_V1_VIDEO: "V1",
    TRACK_A1_NARRATION: "A1",
    TRACK_A2_MUSIC: "A2",
}
_SHORT_TRACK = {v: k for k, v in _TRACK_SHORT.items()}


def make_slot_id(track: str, scene_num: int, phrase_idx: int) -> str:
    """Return the canonical slot identifier for a track / scene / phrase."""
    short = _TRACK_SHORT.get(track, track)
    return f"{short}:{int(scene_num)}:{int(phrase_idx)}"


def parse_slot_id(slot_id: str) -> tuple[str, int, int]:
    """Inverse of :func:`make_slot_id` — returns ``(track, scene, phrase)``.

    Raises ``ValueError`` on malformed input rather than silently returning
    a best-effort guess.  Slot IDs flow through URLs so accepting garbage
    would let clients request arbitrary read paths.
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
    track = _SHORT_TRACK.get(short)
    if track is None:
        raise ValueError(f"unknown track {short!r} in slot id {slot_id!r}")
    return track, scene, phrase


# ---------------------------------------------------------------------------
# Output records
# ---------------------------------------------------------------------------


@dataclass
class SlotView:
    """A single scale-accurate slot on one of the three canonical tracks."""

    slot_id: str
    track: str
    scene_num: int
    phrase_idx: int
    start_sec: float
    duration_sec: float
    #: "pending" | "in_progress" | "delivered" | "failed" | "gap"
    status: str = "pending"
    #: Free-form label shown inline before delivery
    #: (e.g. ``"scene 3 phrase 2 — scripted 4.2s"``).
    label: str = ""
    #: Real artifact URL once delivered (video file, wav, etc.).
    preview_url: str = ""
    #: For delivered video clips — a thumbnail URL (first frame). Best-effort.
    thumbnail_url: str = ""
    #: For narration / music — a URL to the waveform data.
    waveform_url: str = ""
    #: Failure reason (truncated) when ``status == "failed"``.
    failure_reason: str = ""
    #: Current content-ladder rung when ``status == "in_progress"``.
    rung: str = ""
    #: Scripted duration emitted by the scenario director (seconds).
    scripted_duration_sec: Optional[float] = None
    #: Measured duration (from WhisperX alignment) if available.
    measured_duration_sec: Optional[float] = None
    #: Extra artifact-type-specific metadata.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrackView:
    name: str
    kind: str
    slots: list[SlotView] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "slots": [s.to_dict() for s in self.slots],
            "total_slots": len(self.slots),
        }


@dataclass
class FinishedFilm:
    """Describes the assembled documentary when it is ready to watch.

    Populated once ``deterministic_assembly_callback`` has written the
    ``final_documentary*.mp4`` file(s) under ``output_dir``.  The UI
    renders a "▶ Watch your film" card when any of these are non-empty.
    """

    url: str = ""  # HTTP URL served by /api/final_film/<name>
    duration_sec: float = 0.0
    language: str = ""  # "" for single-language, "ru"/"en" in dual
    alternates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "duration_sec": round(self.duration_sec, 3),
            "language": self.language,
            "alternates": list(self.alternates),
        }


@dataclass
class OtioTimelineView:
    """Full dashboard-facing OTIO view.

    The three canonical tracks are always present (empty if no clips yet)
    so the frontend never has to special-case absence.  ``state`` is the
    authoritative OTIO lifecycle state (``draft`` or ``authoritative``) —
    the overlay component keys on this to show the reconciliation diff.
    """

    state: str = "draft"  # "draft" | "authoritative"
    total_duration_sec: float = 0.0
    tracks: list[TrackView] = field(default_factory=list)
    reconciliation: list[dict[str, Any]] = field(default_factory=list)
    source_file: str = ""
    finished_film: Optional[FinishedFilm] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "total_duration_sec": round(self.total_duration_sec, 3),
            "tracks": [t.to_dict() for t in self.tracks],
            "reconciliation": self.reconciliation,
            "source_file": self.source_file,
            "finished_film": self.finished_film.to_dict() if self.finished_film else None,
        }


# ---------------------------------------------------------------------------
# OTIO file discovery + parsing
# ---------------------------------------------------------------------------


def _latest_otio_path(output_dir: str) -> str:
    """Return the most recent non-backup OTIO file under ``output_dir``.

    Mirrors :func:`server.agui.get_timeline`'s selection rule so the two
    endpoints never disagree about which timeline is "current".
    """
    pattern = os.path.join(output_dir, "timelines", "*.otio")
    candidates = [
        f for f in sorted(glob.glob(pattern))
        if not os.path.basename(f).startswith("_")
    ]
    return candidates[-1] if candidates else ""


def _seconds(rt: Any) -> float:
    """Convert an OTIO RationalTime-like dict to seconds."""
    if not isinstance(rt, dict):
        return 0.0
    value = rt.get("value") or 0
    rate = rt.get("rate") or 1
    try:
        return float(value) / float(rate) if rate else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _extract_scene_phrase(name: str, metadata: dict) -> tuple[int, int]:
    """Best-effort extraction of (scene_num, phrase_idx) from a clip.

    OTIO clip metadata emitted by the scenario director carries scene/phrase
    keys directly; older files only have it embedded in the clip name
    (e.g. ``scene_003_phrase_002``).  We accept both.
    """
    doc_meta = (metadata or {}).get("documentary") or {}
    if doc_meta:
        s = doc_meta.get("scene_num")
        if s is None:
            s = doc_meta.get("scene")
        p = doc_meta.get("phrase_idx")
        if p is None:
            p = doc_meta.get("phrase")
        if isinstance(s, int) and isinstance(p, int):
            return s, p
    # Direct keys on metadata root
    s = metadata.get("scene_num") if isinstance(metadata, dict) else None
    p = metadata.get("phrase_idx") if isinstance(metadata, dict) else None
    if isinstance(s, int) and isinstance(p, int):
        return s, p
    # Fall back to name parsing
    m = re.search(r"scene[_\s]*(\d+)[^\d]*phrase[_\s]*(\d+)", name or "", re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"s(\d+)_p(\d+)", name or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def _canonical_track_name(raw: str, kind: str) -> Optional[str]:
    """Normalise a track name to one of the three canonical tracks.

    Returns ``None`` for anything we don't know how to render (the
    dashboard is opinionated: only V1/A1/A2 are shown; other tracks
    are ignored by design so the centrepiece never becomes a soup).
    """
    if not raw:
        return None
    lowered = raw.strip().lower()
    kind_lower = (kind or "").lower()
    if "video" in lowered or "v1" in lowered or kind_lower == "video":
        return TRACK_V1_VIDEO
    if "narration" in lowered or "a1" in lowered:
        return TRACK_A1_NARRATION
    if "music" in lowered or "a2" in lowered:
        return TRACK_A2_MUSIC
    # Anything else (SFX, captions, …) is not rendered on the centrepiece.
    return None


# ---------------------------------------------------------------------------
# Delivery overlays (status files, feedback store, reconciliation report)
# ---------------------------------------------------------------------------


_STATUS_BASENAME_RE = re.compile(r"scene_(\d+)_phrase_(\d+)_status\.json")


def _load_video_status(output_dir: str) -> dict[tuple[int, int], dict[str, Any]]:
    """Load all video ``_status.json`` files, keyed by (scene, phrase)."""
    result: dict[tuple[int, int], dict[str, Any]] = {}
    pattern = os.path.join(output_dir, "video", "*_status.json")
    for path in glob.glob(pattern):
        m = _STATUS_BASENAME_RE.match(os.path.basename(path))
        if not m:
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.debug("otio_timeline_model: failed to read %s: %s", path, exc)
            continue
        key = (int(m.group(1)), int(m.group(2)))
        data["_status_path"] = path
        result[key] = data
    return result


def _load_reconciliation_report(output_dir: str) -> list[dict[str, Any]]:
    """Return the narration reconciliation report rows, or []."""
    candidates = [
        os.path.join(output_dir, "audio", "_narration_reconciliation.json"),
        os.path.join(output_dir, "timelines", "_narration_reconciliation.json"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("blocks"), list):
            return data["blocks"]
    return []


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_timeline_view(
    output_dir: str,
    *,
    feedback_artifacts: Optional[list[dict[str, Any]]] = None,
    ladder_rungs: Optional[dict[str, str]] = None,
) -> OtioTimelineView:
    """Build the centrepiece view from disk + in-memory overlays.

    Args:
        output_dir: Pipeline output root (``/tmp/documentary-pipeline`` by
            default in production).
        feedback_artifacts: Output of
            :meth:`server.agui.FeedbackStore.get_all_artifacts`.  When
            supplied, delivered/in-progress/rejected status flows through
            here instead of (or in addition to) on-disk status.
        ladder_rungs: Optional mapping ``slot_id -> rung_label`` for
            in-flight escalations.  The orchestrator owns rung bookkeeping;
            we just decorate.

    Returns an :class:`OtioTimelineView` with the three canonical tracks
    always populated (possibly with zero slots).  Never raises — on
    parse errors we log and return an empty view.
    """

    otio_path = _latest_otio_path(output_dir)
    tracks = {
        TRACK_V1_VIDEO: TrackView(name=TRACK_V1_VIDEO, kind="video"),
        TRACK_A1_NARRATION: TrackView(name=TRACK_A1_NARRATION, kind="audio"),
        TRACK_A2_MUSIC: TrackView(name=TRACK_A2_MUSIC, kind="audio"),
    }
    total_duration = 0.0
    state = "draft"

    if not otio_path:
        logger.debug("otio_timeline_model: no OTIO file found under %s", output_dir)
        return OtioTimelineView(
            state=state,
            tracks=list(tracks.values()),
            finished_film=_detect_finished_film(output_dir),
        )

    try:
        with open(otio_path) as f:
            otio = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("otio_timeline_model: failed to parse %s: %s", otio_path, exc)
        return OtioTimelineView(
            state=state,
            tracks=list(tracks.values()),
            finished_film=_detect_finished_film(output_dir),
        )

    # Root-level state: either persisted on the OTIO file itself (ARCH-E1
    # stamps documentary.state) or absent (treat as draft).
    root_meta = otio.get("metadata") or {}
    doc_root = root_meta.get("documentary") or {}
    maybe_state = doc_root.get("state")
    if isinstance(maybe_state, str) and maybe_state in ("draft", "authoritative"):
        state = maybe_state

    # Overlay sources
    feedback_by_slot: dict[str, dict[str, Any]] = {}
    for art in feedback_artifacts or []:
        at = art.get("type", "")
        scene = art.get("scene_num", 0)
        phrase = art.get("phrase_idx", 0)
        # Artifacts from different types map to different tracks
        if at == "video_clip":
            key = make_slot_id(TRACK_V1_VIDEO, scene, phrase)
        elif at == "narration":
            key = make_slot_id(TRACK_A1_NARRATION, scene, phrase)
        else:
            continue
        feedback_by_slot[key] = art

    status_by_sp = _load_video_status(output_dir)
    recon_rows = _load_reconciliation_report(output_dir)
    recon_by_sp: dict[tuple[int, int], dict[str, Any]] = {}
    for row in recon_rows:
        s = row.get("scene_num") or row.get("scene") or 0
        p = row.get("phrase_idx") or row.get("phrase") or 0
        try:
            recon_by_sp[(int(s), int(p))] = row
        except Exception:  # noqa: BLE001
            continue

    # Walk OTIO tracks
    for raw_track in otio.get("tracks", {}).get("children", []) or []:
        track_name = _canonical_track_name(
            raw_track.get("name", ""), raw_track.get("kind", "")
        )
        if track_name is None:
            continue
        view = tracks[track_name]

        cursor = 0.0
        for child in raw_track.get("children", []) or []:
            schema = child.get("OTIO_SCHEMA", "")
            sr = child.get("source_range", {})
            duration_sec = _seconds(sr.get("duration"))
            name = child.get("name", "")
            metadata = child.get("metadata", {}) or {}

            if "Gap" in schema:
                # Gaps exist in the OTIO timeline for pacing; we surface them
                # as explicit "gap" slots so the frontend can render spacing
                # correctly rather than collapsing them.
                scene, phrase = _extract_scene_phrase(name, metadata)
                slot = SlotView(
                    slot_id=f"{_TRACK_SHORT[track_name]}:gap:{int(cursor*1000)}",
                    track=track_name,
                    scene_num=scene,
                    phrase_idx=phrase,
                    start_sec=round(cursor, 3),
                    duration_sec=round(duration_sec, 3),
                    status="gap",
                    label=name or "gap",
                )
                view.slots.append(slot)
                cursor += duration_sec
                continue

            scene, phrase = _extract_scene_phrase(name, metadata)
            slot_id = make_slot_id(track_name, scene, phrase)
            scripted_dur = duration_sec

            slot = SlotView(
                slot_id=slot_id,
                track=track_name,
                scene_num=scene,
                phrase_idx=phrase,
                start_sec=round(cursor, 3),
                duration_sec=round(duration_sec, 3),
                status="pending",
                label=(
                    name
                    or f"scene {scene} phrase {phrase} — scripted {scripted_dur:.1f}s"
                ),
                scripted_duration_sec=round(scripted_dur, 3),
                metadata=dict(metadata),
            )

            # Overlay in-memory artifact state
            fb = feedback_by_slot.get(slot_id)
            if fb is not None:
                fb_status = fb.get("status", "")
                if fb_status == "approved":
                    slot.status = "delivered"
                elif fb_status == "rejected":
                    slot.status = "failed"
                elif fb_status in ("generating", "regenerating"):
                    slot.status = "in_progress"
                elif fb_status == "pending_review":
                    slot.status = "delivered"
                if fb.get("preview_url"):
                    slot.preview_url = fb["preview_url"]
                if fb.get("metadata"):
                    slot.metadata.setdefault("artifact", fb["metadata"])

            # Overlay on-disk video status when we're on the video track.
            if track_name == TRACK_V1_VIDEO:
                disk = status_by_sp.get((scene, phrase))
                if disk is not None:
                    quality = disk.get("quality", "unknown")
                    attempts = disk.get("attempts", 0)
                    qa_reason = disk.get("qa_reason", "") or ""
                    has_mp4 = os.path.exists(
                        disk.get("_status_path", "").replace("_status.json", ".mp4")
                    )
                    if quality in ("acceptable", "excellent", "good") and has_mp4:
                        slot.status = "delivered"
                        mp4_path = disk.get("_status_path", "").replace(
                            "_status.json", ".mp4"
                        )
                        slot.preview_url = slot.preview_url or mp4_path
                        slot.thumbnail_url = slot.thumbnail_url or (
                            f"/agui/slots/{slot_id}/thumbnail"
                        )
                    elif quality in ("rejected", "bad") or attempts >= 3:
                        slot.status = "failed"
                        if qa_reason:
                            slot.failure_reason = qa_reason[:200]
                    elif quality == "unknown" and not has_mp4:
                        slot.status = "in_progress"
                    if slot.status == "in_progress" and ladder_rungs:
                        slot.rung = ladder_rungs.get(slot_id, "")

            # Overlay narration reconciliation on the narration track.
            if track_name == TRACK_A1_NARRATION:
                recon = recon_by_sp.get((scene, phrase))
                if recon is not None:
                    measured = recon.get("measured_duration_sec")
                    if measured is None:
                        measured = recon.get("measured_sec")
                    if measured is None:
                        measured = recon.get("measured")
                    scripted = recon.get("scripted_duration_sec")
                    if scripted is None:
                        scripted = recon.get("scripted_sec")
                    if scripted is None:
                        scripted = recon.get("scripted")
                    try:
                        if measured is not None:
                            slot.measured_duration_sec = round(float(measured), 3)
                        if scripted is not None:
                            slot.scripted_duration_sec = round(float(scripted), 3)
                    except Exception:  # noqa: BLE001
                        pass
                # Delivered narration has a WAV under audio/
                wav_guess = os.path.join(
                    output_dir, "audio", f"scene_{scene:03d}_phrase_{phrase:03d}.wav"
                )
                if os.path.exists(wav_guess):
                    slot.status = "delivered" if slot.status == "pending" else slot.status
                    slot.preview_url = slot.preview_url or wav_guess
                    slot.waveform_url = (
                        slot.waveform_url or f"/agui/slots/{slot_id}/waveform"
                    )

            view.slots.append(slot)
            cursor += duration_sec

        total_duration = max(total_duration, cursor)

    # Build reconciliation summary (scripted vs measured per narration block)
    reconciliation: list[dict[str, Any]] = []
    if state == "draft":
        for slot in tracks[TRACK_A1_NARRATION].slots:
            if slot.status == "gap":
                continue
            if slot.scripted_duration_sec is None:
                continue
            diff = None
            if slot.measured_duration_sec is not None:
                diff = round(slot.measured_duration_sec - slot.scripted_duration_sec, 3)
            reconciliation.append({
                "slot_id": slot.slot_id,
                "scene_num": slot.scene_num,
                "phrase_idx": slot.phrase_idx,
                "start_sec": slot.start_sec,
                "scripted_duration_sec": slot.scripted_duration_sec,
                "measured_duration_sec": slot.measured_duration_sec,
                "skew_sec": diff,
            })

    finished_film = _detect_finished_film(output_dir)

    return OtioTimelineView(
        state=state,
        total_duration_sec=round(total_duration, 3),
        tracks=[tracks[n] for n in CANONICAL_TRACKS],
        reconciliation=reconciliation,
        source_file=otio_path,
        finished_film=finished_film,
    )


def _detect_finished_film(output_dir: str) -> Optional[FinishedFilm]:
    """Return a FinishedFilm pointer when ``final_documentary*.mp4`` exists.

    Walks ``output_dir`` for the canonical filenames produced by
    :func:`deterministic_assembly_callback`.  Returns ``None`` when no
    final file exists yet — the UI then hides the "watch your film"
    card.  Durations are probed via ``probe_clip`` so the UI can show a
    precise runtime without re-opening the file itself.
    """
    if not output_dir:
        return None

    # Single-language (no suffix) takes priority; if both "_ru" and no
    # suffix exist we trust the single-language file.
    primary_name = "final_documentary.mp4"
    ru_name = "final_documentary_ru.mp4"
    en_name = "final_documentary_en.mp4"

    primary_path = os.path.join(output_dir, primary_name)
    ru_path = os.path.join(output_dir, ru_name)
    en_path = os.path.join(output_dir, en_name)

    def _probe_dur(path: str) -> float:
        try:
            from tools.video_tools import probe_clip  # type: ignore[attr-defined]  # local import
            return float(json.loads(probe_clip(mp4_path=path)).get("duration", 0.0))
        except Exception:  # noqa: BLE001
            return 0.0

    def _url_for(name: str) -> str:
        return f"/agui/final_film/{name}"

    if os.path.exists(primary_path):
        return FinishedFilm(
            url=_url_for(primary_name),
            duration_sec=_probe_dur(primary_path),
            language="",
            alternates=[],
        )

    if os.path.exists(ru_path):
        alternates: list[dict[str, Any]] = []
        if os.path.exists(en_path):
            alternates.append({
                "url": _url_for(en_name),
                "duration_sec": _probe_dur(en_path),
                "language": "en",
            })
        return FinishedFilm(
            url=_url_for(ru_name),
            duration_sec=_probe_dur(ru_path),
            language="ru",
            alternates=alternates,
        )

    if os.path.exists(en_path):
        return FinishedFilm(
            url=_url_for(en_name),
            duration_sec=_probe_dur(en_path),
            language="en",
            alternates=[],
        )

    return None


__all__ = [
    "TRACK_V1_VIDEO",
    "TRACK_A1_NARRATION",
    "TRACK_A2_MUSIC",
    "CANONICAL_TRACKS",
    "SlotView",
    "TrackView",
    "FinishedFilm",
    "OtioTimelineView",
    "make_slot_id",
    "parse_slot_id",
    "build_timeline_view",
]
