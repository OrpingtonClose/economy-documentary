"""ARCH-G1 — Preview builder (deterministic, non-LLM).

Produces a single renderable ``preview_<hash>.mp4`` from the current
OTIO (authoritative or draft) plus whatever clips are already on disk,
with **honest placeholders** for missing / failed / in-progress slots:

- Missing video → black card + ETA text overlay (read from
  ``fleet_state`` / ``worker_state`` on the blackboard).
- Missing audio → silence + caption track showing the scripted line
  with a ``[pending]`` tag.
- Failed slots → red card + failure reason.
- In-progress slots → amber card + current rung label.

Every slot's rendered duration equals its declared OTIO source-range
duration — the builder never stretches, squashes, or substitutes
neighbouring media.

Invariants (enforced by this module):

1. **Previews never advance the pipeline.** This builder is a pure
   reader over state + a writer of a new file under
   ``PREVIEW_OUTPUT_DIR``.  It does **not** mutate the OTIO timeline,
   the blackboard keys that gate stage transitions, artifact tags, or
   approval-gate state.
2. **Idempotent.** Running twice on the same state produces byte
   identical output and an identical manifest.  This is guaranteed by
   hashing the slot plan (deterministic JSON) and naming the output
   ``preview_<hash>.mp4`` / ``preview_<hash>.manifest.json``.
3. **Honest placeholders only.** There is no path that substitutes a
   neighbouring clip, holds a frame, or silently pads audio.  A slot
   with no delivered media **always** renders as a card that says so.
4. **Cheap re-run.** Existing ``preview_<hash>.*`` is returned
   untouched — no ffmpeg work is repeated.
5. **Fail loud.** Gaps with non-positive duration, overlapping
   clip/gap ranges, or inconsistent track structure raise
   :class:`PreviewInconsistencyError`; they do not silently render a
   broken preview.

Spec references:

- Issue #153 (ARCH-G1 Preview builder)
- Parent #129 (ARCH-G Preview loop), meta #122 (ARCH-2026)
- ``docs/ARCHITECTURE_DIAGRAMS.md`` diagram 9
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants — blackboard keys + artifact identifiers
# ---------------------------------------------------------------------------

#: Blackboard key for caller-supplied per-slot overrides.  Shape::
#:
#:     {"<slot_key>": {"status": "failed"|"in_progress",
#:                      "reason": "...",
#:                      "rung": "L2 CREATIVE — trying alt provider"}}
#:
#: ``slot_key`` is the OTIO item ``name`` (e.g. ``scene_003_V1`` for the
#: V1 gap of scene 3; ``scene_003_phrase_002`` for a video clip;
#: ``scene_003_V1_RU`` for a narration block).  Overrides take
#: precedence over classification inferred from the OTIO item itself.
PREVIEW_SLOT_OVERRIDES_KEY = "_preview_slot_statuses"

#: Blackboard key where :func:`build_preview` stores the path of the
#: most recent preview.  Never read by pipeline-advancing code; only
#: read by the preview consumer lanes (ARCH-G3).
LATEST_PREVIEW_KEY = "_latest_preview_path"

#: Blackboard key under which the preview history is appended.  Entry
#: is the full :class:`PreviewManifest.to_dict` of every preview built
#: in this run.
PREVIEW_HISTORY_KEY = "_preview_history"

#: Artifact ``kind`` tag — marks the preview as a QA artifact,
#: **not** a deliverable.  Downstream stage gates (approval_gate,
#: otio_state) key on deliverable kinds and MUST NOT key on this.
PREVIEW_ARTIFACT_KIND = "preview_assembly"

#: Default output directory for preview assemblies.  Overridable by
#: ``PREVIEW_OUTPUT_DIR`` env var — tests pin this to a tmp_path.
DEFAULT_PREVIEW_DIR = os.environ.get(
    "PREVIEW_OUTPUT_DIR",
    os.path.join(
        os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline"),
        "previews",
    ),
)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class PreviewInconsistencyError(RuntimeError):
    """Raised when the input OTIO cannot be rendered honestly.

    Examples:

    - Gap or Clip with duration <= 0.
    - Overlap between consecutive items on the same track.
    - Missing required track (V1_Video or A1_Narration).

    The builder refuses to render a preview when any of these holds —
    "fail loud" is an architectural invariant (meta #122).
    """


class PreviewRenderError(RuntimeError):
    """Raised when ffmpeg fails to produce the preview mp4."""


# ---------------------------------------------------------------------------
# Slot taxonomy
# ---------------------------------------------------------------------------


class SlotKind(str, Enum):
    """What kind of slot this is in the preview plan."""

    VIDEO = "video"
    NARRATION = "narration"
    MUSIC = "music"


class SlotStatus(str, Enum):
    """Per-slot classification for the preview renderer."""

    #: Real delivered media that the renderer will mux verbatim.
    DELIVERED = "delivered"

    #: Scripted block with no delivered media yet.  Rendered as the
    #: "missing" placeholder (black card with ETA / silence with
    #: caption marked ``[pending]``).
    MISSING = "missing"

    #: Slot currently being worked on by the content/infra ladder.
    #: Rendered as an amber card with the current rung label.
    IN_PROGRESS = "in_progress"

    #: Slot that the content ladder has given up on.  Rendered as a
    #: red card with the failure reason.
    FAILED = "failed"

    #: Intentional silence (inter-voice gap, inter-scene gap) on the
    #: narration track.  Rendered as real silence — no caption needed
    #: because it is not a missing block.
    INTENTIONAL_SILENCE = "intentional_silence"


@dataclass(frozen=True)
class SlotPlan:
    """One time slot in the preview assembly.

    The plan is deterministic — two runs on the same inputs produce
    equal ``SlotPlan`` instances and therefore identical output bytes
    after ffmpeg rendering with the same binary version.
    """

    track: str                  # e.g. "V1_Video" or "A1_Narration"
    kind: SlotKind
    index: int                  # order within its track
    slot_key: str               # OTIO item name (or synthetic id)
    status: SlotStatus
    duration_sec: float
    media_path: Optional[str] = None  # only set when status=DELIVERED

    # Contextual data for placeholder rendering (text drawn on card,
    # caption on silence, ETA, failure reason, ladder rung).
    scene_num: Optional[int] = None
    scripted_text: Optional[str] = None
    eta_text: Optional[str] = None
    rung_text: Optional[str] = None
    failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["status"] = self.status.value
        return d


@dataclass(frozen=True)
class PreviewManifest:
    """Manifest emitted alongside the preview mp4.

    Consumed by the agent + human lanes (ARCH-G3).  Stamped with
    :data:`PREVIEW_ARTIFACT_KIND` so downstream code can distinguish a
    preview from a deliverable unambiguously.
    """

    kind: str                   # always PREVIEW_ARTIFACT_KIND
    preview_path: str
    manifest_path: str
    input_hash: str             # SHA-256 of the canonical plan
    trigger_reason: str
    timeline_path: str
    otio_state: str             # "draft" or "authoritative"
    built_at: float
    total_duration_sec: float
    slots: tuple                # tuple of SlotPlan
    counts: dict                # {"delivered": N, "missing": M, ...}

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "preview_path": self.preview_path,
            "manifest_path": self.manifest_path,
            "input_hash": self.input_hash,
            "trigger_reason": self.trigger_reason,
            "timeline_path": self.timeline_path,
            "otio_state": self.otio_state,
            "built_at": self.built_at,
            "total_duration_sec": self.total_duration_sec,
            "slots": [s.to_dict() for s in self.slots],
            "counts": dict(self.counts),
        }


# ---------------------------------------------------------------------------
# Helpers: state lookups (all tolerant of missing keys — preview must
# render something useful even when blackboard is sparsely populated)
# ---------------------------------------------------------------------------


def _get_overrides(state: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    val = state.get(PREVIEW_SLOT_OVERRIDES_KEY, {})
    return val if isinstance(val, Mapping) else {}


def _get_fleet_eta_text(
    state: Mapping[str, Any], slot_key: str
) -> Optional[str]:
    """Return a short ETA label for ``slot_key`` from fleet state.

    Order of preference:

    1. ``fleet_state["eta_by_slot"][slot_key]`` (caller-provided map).
    2. ``fleet_state["eta_sec"]`` (fleet-wide ETA; applied to every
       pending slot as a fallback).
    3. ``worker_state["<any worker>"]["current_job"] == slot_key`` →
       use that worker's ``eta_sec``.
    4. ``None`` — caller should render "ETA: unknown".
    """
    fleet = state.get("fleet_state") or {}
    workers = state.get("worker_state") or {}

    if isinstance(fleet, Mapping):
        per_slot = fleet.get("eta_by_slot")
        if isinstance(per_slot, Mapping):
            v = per_slot.get(slot_key)
            if v:
                return str(v)

    if isinstance(workers, Mapping):
        for w in workers.values():
            if not isinstance(w, Mapping):
                continue
            if w.get("current_job") == slot_key and w.get("eta_sec") is not None:
                try:
                    secs = float(w["eta_sec"])
                except (TypeError, ValueError):
                    continue
                return _format_eta_seconds(secs)

    if isinstance(fleet, Mapping) and fleet.get("eta_sec") is not None:
        try:
            return _format_eta_seconds(float(fleet["eta_sec"]))
        except (TypeError, ValueError):
            return None

    return None


def _format_eta_seconds(secs: float) -> str:
    if secs < 60:
        return f"ETA: {int(secs)}s"
    mins = secs / 60.0
    if mins < 60:
        return f"ETA: {mins:.1f} min"
    hrs = mins / 60.0
    return f"ETA: {hrs:.1f} h"


# ---------------------------------------------------------------------------
# Planning — walks the OTIO timeline and classifies every slot
# ---------------------------------------------------------------------------


# Internal helper: keep the OTIO import lazy so unit tests that feed a
# ``SlotPlan`` list directly (bypassing planning) do not require the
# ``opentimelineio`` wheel.

_TRACK_V1 = "V1_Video"
_TRACK_A1 = "A1_Narration"
_TRACK_A2 = "A2_Music"


def _load_timeline(timeline_path: str):
    import opentimelineio as otio  # type: ignore
    from tools.otio_tools import _otio_lock  # type: ignore

    with _otio_lock:
        return otio.adapters.read_from_file(timeline_path)


def _duration_sec(item) -> float:
    sr = getattr(item, "source_range", None)
    if sr is None or getattr(sr, "duration", None) is None:
        return 0.0
    try:
        return float(sr.duration.to_seconds())
    except Exception:  # noqa: BLE001 — tolerate odd OTIO objects
        try:
            rate = float(sr.duration.rate) or 24.0
            return float(sr.duration.value) / rate
        except Exception:
            return 0.0


def _doc_meta(item) -> Mapping[str, Any]:
    meta = getattr(item, "metadata", None) or {}
    doc = meta.get("documentary") if isinstance(meta, Mapping) else None
    return doc if isinstance(doc, Mapping) else {}


def _external_path(clip) -> Optional[str]:
    """Return the on-disk path for a clip's media reference or ``None``."""
    import opentimelineio as otio  # type: ignore

    mr = getattr(clip, "media_reference", None)
    if not isinstance(mr, otio.schema.ExternalReference):
        return None
    url = getattr(mr, "target_url", None) or ""
    if url.startswith("file://"):
        url = url[len("file://") :]
    return url or None


def _classify_item(
    item,
    track_name: str,
    index: int,
    state: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, Any]],
) -> SlotPlan:
    """Classify one OTIO item into a :class:`SlotPlan`.

    The classification follows the precedence:

    1. Caller-supplied override in :data:`PREVIEW_SLOT_OVERRIDES_KEY`.
    2. OTIO ``metadata.documentary.status`` — ``"failed"`` /
       ``"in_progress"`` are honoured when present.
    3. Structural: ``Gap`` → MISSING (or INTENTIONAL_SILENCE if
       ``type == "silence"``); ``Clip`` with existing media file →
       DELIVERED; ``Clip`` with missing file → MISSING.
    """
    import opentimelineio as otio  # type: ignore

    name = getattr(item, "name", "") or f"{track_name}_item_{index:03d}"
    doc = _doc_meta(item)
    duration = _duration_sec(item)
    if duration <= 0:
        raise PreviewInconsistencyError(
            f"slot {name!r} on track {track_name!r} has non-positive "
            f"duration={duration}; refusing to render inconsistent OTIO."
        )

    kind = _kind_for_track(track_name)
    scene_num = doc.get("scene_num") if isinstance(doc.get("scene_num"), int) else None
    scripted_text = doc.get("scripted_text") or doc.get("text")

    over = overrides.get(name) or {}
    override_status = over.get("status")

    # Resolve status.
    status: SlotStatus
    reason: Optional[str] = None
    rung: Optional[str] = None
    eta_text: Optional[str] = None
    media_path: Optional[str] = None

    if override_status in ("failed",):
        status = SlotStatus.FAILED
        reason = over.get("reason") or doc.get("failure_reason")
    elif override_status in ("in_progress", "in-progress", "pending"):
        status = SlotStatus.IN_PROGRESS
        rung = over.get("rung") or doc.get("rung")
        eta_text = over.get("eta_text") or _get_fleet_eta_text(state, name)
    elif override_status in ("delivered",):
        status = SlotStatus.DELIVERED
        media_path = over.get("media_path") or (
            _external_path(item) if isinstance(item, otio.schema.Clip) else None
        )
    elif isinstance(item, otio.schema.Gap):
        gap_kind = str(doc.get("type") or doc.get("gap_type") or "").lower()
        if kind == SlotKind.NARRATION and (
            gap_kind in ("silence", "inter_voice", "inter_scene")
        ):
            status = SlotStatus.INTENTIONAL_SILENCE
        else:
            # Video placeholder gap OR narration "empty" placeholder.
            doc_status = str(doc.get("status") or "").lower()
            if doc_status == "failed":
                status = SlotStatus.FAILED
                reason = doc.get("failure_reason") or "failed"
            elif doc_status in ("in_progress", "in-progress", "pending"):
                status = SlotStatus.IN_PROGRESS
                rung = doc.get("rung")
                eta_text = _get_fleet_eta_text(state, name)
            else:
                status = SlotStatus.MISSING
                eta_text = _get_fleet_eta_text(state, name)
    elif isinstance(item, otio.schema.Clip):
        path = _external_path(item)
        doc_status = str(doc.get("status") or "").lower()
        if doc_status == "failed":
            status = SlotStatus.FAILED
            reason = doc.get("failure_reason") or "failed"
        elif doc_status in ("in_progress", "in-progress", "pending"):
            status = SlotStatus.IN_PROGRESS
            rung = doc.get("rung")
            eta_text = _get_fleet_eta_text(state, name)
        elif path and os.path.exists(path):
            status = SlotStatus.DELIVERED
            media_path = path
        else:
            status = SlotStatus.MISSING
            eta_text = _get_fleet_eta_text(state, name)
    else:
        raise PreviewInconsistencyError(
            f"slot {name!r} on track {track_name!r} is neither a Clip "
            f"nor a Gap (got {type(item).__name__!r})."
        )

    return SlotPlan(
        track=track_name,
        kind=kind,
        index=index,
        slot_key=name,
        status=status,
        duration_sec=round(duration, 6),
        media_path=media_path,
        scene_num=scene_num,
        scripted_text=scripted_text,
        eta_text=eta_text,
        rung_text=rung,
        failure_reason=reason,
    )


def _kind_for_track(track_name: str) -> SlotKind:
    if track_name == _TRACK_V1:
        return SlotKind.VIDEO
    if track_name == _TRACK_A1:
        return SlotKind.NARRATION
    if track_name == _TRACK_A2:
        return SlotKind.MUSIC
    # Unknown tracks are classified as music (audio bed) so they do
    # not accidentally render as video placeholders.
    return SlotKind.MUSIC


def plan_preview(
    state: Mapping[str, Any],
    timeline_path: Optional[str] = None,
) -> list[SlotPlan]:
    """Walk the OTIO timeline and return a deterministic list of slots.

    Args:
        state: Blackboard / session state.  Read only.
        timeline_path: Override the path (tests).  Defaults to
            ``state["_timeline_path"]``.

    Raises:
        PreviewInconsistencyError: on negative duration, overlap, or
            missing V1/A1 tracks.
    """

    tl_path = timeline_path or state.get("_timeline_path") or ""
    if not tl_path or not os.path.exists(tl_path):
        raise PreviewInconsistencyError(
            f"preview builder: OTIO timeline not found at {tl_path!r}"
        )

    timeline = _load_timeline(tl_path)
    overrides = _get_overrides(state)
    plans: list[SlotPlan] = []
    seen_tracks: set[str] = set()

    for track in timeline.tracks:
        seen_tracks.add(track.name)
        # Overlap / negative-duration guard done per-item by
        # _classify_item.  Order on a track is positional (OTIO tracks
        # are sequential) so two consecutive items never overlap by
        # definition — but validate durations sum to something finite.
        running = 0.0
        for idx, item in enumerate(track):
            plan = _classify_item(
                item,
                track_name=track.name,
                index=idx,
                state=state,
                overrides=overrides,
            )
            if plan.duration_sec <= 0:
                raise PreviewInconsistencyError(
                    f"preview builder: track {track.name!r} slot "
                    f"{plan.slot_key!r} has non-positive duration "
                    f"{plan.duration_sec}"
                )
            running += plan.duration_sec
            plans.append(plan)

    # Require V1 + A1 to exist — the preview must show both video and
    # narration tracks even if they are empty placeholders.
    required = {_TRACK_V1, _TRACK_A1}
    missing_tracks = required - seen_tracks
    if missing_tracks:
        raise PreviewInconsistencyError(
            f"preview builder: timeline missing required tracks "
            f"{sorted(missing_tracks)}"
        )

    return plans


# ---------------------------------------------------------------------------
# Hashing — content-addressed output for idempotency
# ---------------------------------------------------------------------------


def _canonical_plan_dict(
    plans: Sequence[SlotPlan],
    timeline_path: str,
    otio_state_value: str,
) -> dict:
    return {
        "timeline_path": os.path.abspath(timeline_path),
        "otio_state": otio_state_value,
        "slots": [p.to_dict() for p in plans],
    }


def compute_input_hash(
    plans: Sequence[SlotPlan],
    timeline_path: str,
    otio_state_value: str = "draft",
) -> str:
    """SHA-256 of the canonical plan — drives idempotent output naming."""
    payload = _canonical_plan_dict(plans, timeline_path, otio_state_value)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Rendering — ffmpeg wrappers for each placeholder kind
# ---------------------------------------------------------------------------

# Profile defaults for the preview.  Pinned to 512x320 @ 24fps /
# 48 kHz stereo so every placeholder / delivered clip concats without
# codec negotiation.  This matches the PREVIEW_512P master profile.

_PV_WIDTH = 512
_PV_HEIGHT = 320
_PV_FPS = 24
_PV_SAR = 48000
_PV_CHANNELS = 2
_PV_PIX_FMT = "yuv420p"
_PV_VCODEC = "libx264"
_PV_ACODEC = "aac"
_PV_ABITRATE = "128k"
_PV_PRESET = "veryfast"
_PV_CRF = "23"

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/DejaVuSans-Bold.ttf",
)


def _resolve_font() -> Optional[str]:
    for cand in _FONT_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
    )


def _wrap_text(text: str, width: int = 48) -> str:
    words = (text or "").split()
    if not words:
        return ""
    lines: list[str] = []
    current: list[str] = []
    running = 0
    for w in words:
        if running + len(w) + 1 > width and current:
            lines.append(" ".join(current))
            current = [w]
            running = len(w)
        else:
            current.append(w)
            running += len(w) + 1
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines[:4])


def _drawtext_chain(
    font: Optional[str],
    title: str,
    subtitle: str = "",
    fg: str = "0xEAEAEA",
    accent: str = "0x7AA2F7",
) -> str:
    if not font:
        # No font available — ffmpeg drawtext requires fontfile for
        # reproducible rendering.  Rather than silently skipping text
        # we raise — preview would be dishonest without the label.
        raise PreviewRenderError(
            "no suitable drawtext font found; install ttf-dejavu."
        )
    title_size = max(24, _PV_HEIGHT // 10)
    subtitle_size = max(14, _PV_HEIGHT // 18)
    parts = [
        (
            f"drawtext=fontfile={font}"
            f":text='{_escape_drawtext(title)}'"
            f":fontcolor={fg}:fontsize={title_size}"
            f":x=(w-text_w)/2:y=(h/2)-(text_h*1.0)"
        ),
    ]
    if subtitle:
        parts.append(
            f"drawtext=fontfile={font}"
            f":text='{_escape_drawtext(_wrap_text(subtitle, 48))}'"
            f":fontcolor={accent}:fontsize={subtitle_size}"
            f":x=(w-text_w)/2:y=(h/2)+(text_h*0.5)"
            f":line_spacing=6"
        )
    return ",".join(parts)


def _run_ffmpeg(cmd: list[str], timeout: int = 120) -> None:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PreviewRenderError(
            "ffmpeg not found on PATH — install ffmpeg to render previews."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewRenderError(f"ffmpeg timed out after {timeout}s: {cmd}") from exc
    if proc.returncode != 0:
        raise PreviewRenderError(
            f"ffmpeg rc={proc.returncode}: {proc.stderr[-500:]}"
        )


def _render_placeholder_segment(
    plan: SlotPlan,
    output_path: str,
    font: Optional[str],
) -> None:
    """Render a single placeholder segment to ``output_path``.

    Each placeholder is a self-contained mp4 with matching codec /
    sample rate / channel count to the delivered clips so concat
    demuxer can stitch them without re-encoding surprises.
    """
    # Background colour by status.
    if plan.status == SlotStatus.FAILED:
        bg = "0x4d0000"           # deep red
        title = f"FAILED — {_short_slot_label(plan)}"
        subtitle = plan.failure_reason or "no reason recorded"
    elif plan.status == SlotStatus.IN_PROGRESS:
        bg = "0x443300"           # amber
        title = f"IN PROGRESS — {_short_slot_label(plan)}"
        subtitle = plan.rung_text or "ladder active"
    elif plan.status == SlotStatus.MISSING and plan.kind == SlotKind.VIDEO:
        bg = "0x000000"           # black
        title = f"MISSING VIDEO — {_short_slot_label(plan)}"
        subtitle = plan.eta_text or "ETA: unknown"
    elif plan.status == SlotStatus.MISSING and plan.kind == SlotKind.NARRATION:
        # Missing audio: render silence with caption marked [pending].
        _render_silent_caption_segment(plan, output_path, font)
        return
    elif plan.status == SlotStatus.INTENTIONAL_SILENCE:
        _render_silent_segment(plan, output_path)
        return
    else:
        bg = "0x111111"
        title = _short_slot_label(plan)
        subtitle = plan.scripted_text or ""

    if font is None:
        raise PreviewRenderError(
            "cannot render placeholder without drawtext font; install ttf-dejavu."
        )

    filt = _drawtext_chain(font, title, subtitle)

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i",
        f"color=c={bg}:s={_PV_WIDTH}x{_PV_HEIGHT}"
        f":r={_PV_FPS}:d={plan.duration_sec:.3f}",
        "-f", "lavfi",
        "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={_PV_SAR}",
        "-vf", filt,
        "-t", f"{plan.duration_sec:.3f}",
        "-c:v", _PV_VCODEC, "-preset", _PV_PRESET, "-crf", _PV_CRF,
        "-pix_fmt", _PV_PIX_FMT, "-r", str(_PV_FPS),
        "-c:a", _PV_ACODEC, "-b:a", _PV_ABITRATE, "-ar", str(_PV_SAR),
        "-ac", str(_PV_CHANNELS),
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)


def _render_silent_segment(plan: SlotPlan, output_path: str) -> None:
    """Intentional silence on the narration track — black video + silence.

    Present on the preview as real silence so a reviewer hears the
    actual pacing.  No caption because an intentional pause is not a
    missing block.
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i",
        f"color=c=0x000000:s={_PV_WIDTH}x{_PV_HEIGHT}"
        f":r={_PV_FPS}:d={plan.duration_sec:.3f}",
        "-f", "lavfi",
        "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={_PV_SAR}",
        "-t", f"{plan.duration_sec:.3f}",
        "-c:v", _PV_VCODEC, "-preset", _PV_PRESET, "-crf", _PV_CRF,
        "-pix_fmt", _PV_PIX_FMT, "-r", str(_PV_FPS),
        "-c:a", _PV_ACODEC, "-b:a", _PV_ABITRATE, "-ar", str(_PV_SAR),
        "-ac", str(_PV_CHANNELS),
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)


def _render_silent_caption_segment(
    plan: SlotPlan,
    output_path: str,
    font: Optional[str],
) -> None:
    """Missing narration block — render silence + the scripted line
    with a ``[pending]`` tag so the reviewer knows this is not the
    final voice."""
    if font is None:
        raise PreviewRenderError(
            "cannot render caption without drawtext font; install ttf-dejavu."
        )
    caption = (plan.scripted_text or "").strip()
    title = f"MISSING NARRATION — {_short_slot_label(plan)}"
    subtitle = f"{caption} [pending]" if caption else "[pending]"
    filt = _drawtext_chain(font, title, subtitle, fg="0xCCCCCC", accent="0xFFD27F")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i",
        f"color=c=0x000000:s={_PV_WIDTH}x{_PV_HEIGHT}"
        f":r={_PV_FPS}:d={plan.duration_sec:.3f}",
        "-f", "lavfi",
        "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={_PV_SAR}",
        "-vf", filt,
        "-t", f"{plan.duration_sec:.3f}",
        "-c:v", _PV_VCODEC, "-preset", _PV_PRESET, "-crf", _PV_CRF,
        "-pix_fmt", _PV_PIX_FMT, "-r", str(_PV_FPS),
        "-c:a", _PV_ACODEC, "-b:a", _PV_ABITRATE, "-ar", str(_PV_SAR),
        "-ac", str(_PV_CHANNELS),
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)


def _short_slot_label(plan: SlotPlan) -> str:
    if plan.scene_num is not None:
        return f"scene {plan.scene_num:03d} · {plan.slot_key}"
    return plan.slot_key


def _normalise_delivered_clip(src_path: str, duration_sec: float, output_path: str) -> None:
    """Re-encode a delivered clip to the preview codec profile.

    Guarantees every segment muxed into the concat demuxer uses
    identical codec / SAR / channel count, so the concat output is
    playable without surprises.  Not run through the master profile —
    previews are intentionally lossy.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", src_path,
        "-t", f"{duration_sec:.3f}",
        "-vf", f"scale={_PV_WIDTH}:{_PV_HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={_PV_WIDTH}:{_PV_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-r", str(_PV_FPS),
        "-c:v", _PV_VCODEC, "-preset", _PV_PRESET, "-crf", _PV_CRF,
        "-pix_fmt", _PV_PIX_FMT,
        "-c:a", _PV_ACODEC, "-b:a", _PV_ABITRATE, "-ar", str(_PV_SAR),
        "-ac", str(_PV_CHANNELS),
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)


def _concat_segments(segment_paths: Sequence[str], output_path: str) -> None:
    fd, listfile = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            for p in segment_paths:
                f.write(f"file '{p}'\n")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", listfile,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        _run_ffmpeg(cmd)
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


def build_preview(
    state: Mapping[str, Any],
    trigger_reason: str,
    output_dir: Optional[str] = None,
) -> PreviewManifest:
    """Build (or re-use) a preview assembly and return its manifest.

    Args:
        state: Blackboard / session state.  Read-only — the builder
            does not mutate this mapping.  Callers that want to record
            "latest preview path" on the blackboard should do so
            AFTER this function returns (see :mod:`server.callbacks.
            preview_triggers`).
        trigger_reason: Short label for why this preview was built
            (``"pre_production"``, ``"scene_003_complete"``, ...).
            Recorded in the manifest; does not affect byte output.
        output_dir: Override the preview output directory.

    Returns:
        :class:`PreviewManifest` describing the assembly.  If a
        byte-identical preview already exists on disk it is returned
        without any ffmpeg work.
    """
    tl_path = state.get("_timeline_path") or ""
    otio_state_value = _read_otio_state(state)
    plans = plan_preview(state, timeline_path=tl_path)

    input_hash = compute_input_hash(plans, tl_path, otio_state_value)
    out_dir = output_dir or DEFAULT_PREVIEW_DIR
    os.makedirs(out_dir, exist_ok=True)
    preview_path = os.path.join(out_dir, f"preview_{input_hash}.mp4")
    manifest_path = os.path.join(out_dir, f"preview_{input_hash}.manifest.json")

    total = round(sum(p.duration_sec for p in plans), 6)
    counts: dict[str, int] = {s.value: 0 for s in SlotStatus}
    for p in plans:
        counts[p.status.value] += 1

    # Idempotency short-circuit — same plan ⇒ same hash ⇒ reuse.
    if os.path.exists(preview_path) and os.path.exists(manifest_path):
        try:
            with open(manifest_path) as fh:
                existing = json.load(fh)
            if existing.get("input_hash") == input_hash:
                logger.info(
                    "preview: reusing existing %s (hash=%s)",
                    preview_path, input_hash,
                )
                return _manifest_from_dict(existing, plans)
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "preview: stale manifest at %s, rebuilding", manifest_path
            )

    font = _resolve_font()
    segs: list[str] = []
    tmp_dir = tempfile.mkdtemp(prefix=f"preview_{input_hash[:8]}_")
    try:
        for i, plan in enumerate(plans):
            seg_out = os.path.join(tmp_dir, f"seg_{i:04d}.mp4")
            if plan.status == SlotStatus.DELIVERED and plan.media_path:
                _normalise_delivered_clip(
                    plan.media_path, plan.duration_sec, seg_out
                )
            else:
                _render_placeholder_segment(plan, seg_out, font)
            segs.append(seg_out)

        if not segs:
            raise PreviewInconsistencyError(
                "preview builder: empty timeline — refusing to render."
            )

        _concat_segments(segs, preview_path)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    manifest = PreviewManifest(
        kind=PREVIEW_ARTIFACT_KIND,
        preview_path=preview_path,
        manifest_path=manifest_path,
        input_hash=input_hash,
        trigger_reason=trigger_reason,
        timeline_path=tl_path,
        otio_state=otio_state_value,
        built_at=time.time(),
        total_duration_sec=total,
        slots=tuple(plans),
        counts=counts,
    )

    # Strip ``built_at`` from the on-disk manifest so a re-run on the
    # same plan still produces byte-identical output — the mp4 is
    # deterministic (ffmpeg output with fixed seed + fixed encoder
    # settings is reproducible modulo encoder version) and the
    # manifest should be too.
    persist = manifest.to_dict()
    persist["built_at"] = None
    with open(manifest_path, "w") as fh:
        json.dump(persist, fh, indent=2, sort_keys=True, ensure_ascii=False)

    logger.info(
        "preview: built %s (%d slots, %.2fs, hash=%s)",
        preview_path, len(plans), total, input_hash,
    )
    return manifest


def _read_otio_state(state: Mapping[str, Any]) -> str:
    try:
        from callbacks.otio_state import (  # type: ignore
            OTIO_STATE_DRAFT,
            get_otio_state,
        )
    except ImportError:
        return "draft"
    try:
        return get_otio_state(state) or OTIO_STATE_DRAFT
    except Exception:  # noqa: BLE001 — never crash the preview for state lookup
        return OTIO_STATE_DRAFT


def _manifest_from_dict(d: Mapping[str, Any], plans: Sequence[SlotPlan]) -> PreviewManifest:
    return PreviewManifest(
        kind=str(d.get("kind") or PREVIEW_ARTIFACT_KIND),
        preview_path=str(d.get("preview_path") or ""),
        manifest_path=str(d.get("manifest_path") or ""),
        input_hash=str(d.get("input_hash") or ""),
        trigger_reason=str(d.get("trigger_reason") or ""),
        timeline_path=str(d.get("timeline_path") or ""),
        otio_state=str(d.get("otio_state") or "draft"),
        built_at=float(d.get("built_at") or 0.0),
        total_duration_sec=float(d.get("total_duration_sec") or 0.0),
        slots=tuple(plans),
        counts=dict(d.get("counts") or {}),
    )


__all__ = [
    "DEFAULT_PREVIEW_DIR",
    "LATEST_PREVIEW_KEY",
    "PREVIEW_ARTIFACT_KIND",
    "PREVIEW_HISTORY_KEY",
    "PREVIEW_SLOT_OVERRIDES_KEY",
    "PreviewInconsistencyError",
    "PreviewManifest",
    "PreviewRenderError",
    "SlotKind",
    "SlotPlan",
    "SlotStatus",
    "build_preview",
    "compute_input_hash",
    "plan_preview",
]
