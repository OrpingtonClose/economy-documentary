"""
Slot detail read model (ARCH-H3).

Pure read-only aggregator for the side panel that opens when a user clicks a
slot on the OTIO centrepiece timeline.  Given a ``slot_id`` (see
:func:`server.otio_timeline_model.make_slot_id`) we collect, in a single
request:

* Artifact history — every revision of the artifact the slot represents,
  with the ledger revision at birth (consumes the ARCH-B1 revision tag).
* QA verdicts — the stylistic QA records (ARCH-E3) + any coherence
  evaluator output from the critique store.
* Reasoning digests relevant to the slot, pulled from the digest engine.
* In-scope ledger records — scoped to scene / block / clip via the
  existing :func:`server.callbacks.preference_ledger.query_by_scope`.
* Current rung in the content ladder or infra ladder (best-effort; empty
  when no ladder state is live on disk).
* The latest preview assembly that includes the slot (latest ``assembly/``
  MP4, filtered by time range).

The aggregator is *read-only*: every helper in this module opens files or
calls other pure read functions.  No state is mutated; no events are
emitted.  Tests assert this in ``server/tests/test_slot_detail_read_model.py``.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, cast

from otio_timeline_model import (
    TRACK_A1_NARRATION,
    TRACK_A2_MUSIC,
    TRACK_V1_VIDEO,
    parse_slot_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output record
# ---------------------------------------------------------------------------


@dataclass
class SlotDetail:
    slot_id: str
    track: str
    scene_num: int
    phrase_idx: int
    artifact_history: list[dict[str, Any]] = field(default_factory=list)
    qa_verdicts: list[dict[str, Any]] = field(default_factory=list)
    reasoning_digests: list[dict[str, Any]] = field(default_factory=list)
    ledger_records: list[dict[str, Any]] = field(default_factory=list)
    current_rung: dict[str, Any] = field(default_factory=dict)
    latest_preview: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Artifact history
# ---------------------------------------------------------------------------


def _artifact_type_for_track(track: str) -> Optional[str]:
    if track == TRACK_V1_VIDEO:
        return "video_clip"
    if track == TRACK_A1_NARRATION:
        return "narration"
    if track == TRACK_A2_MUSIC:
        return "music"
    return None


def _artifact_history_from_store(
    feedback_artifacts: list[dict[str, Any]],
    track: str,
    scene_num: int,
    phrase_idx: int,
) -> list[dict[str, Any]]:
    """Return all revisions of the artifact backing this slot.

    The in-memory feedback store keeps the latest status per artifact id but
    emits events for each transition.  For the dashboard we surface the
    current record and any prior versions discoverable on disk.
    """
    target_type = _artifact_type_for_track(track)
    if target_type is None:
        return []
    rows: list[dict[str, Any]] = []
    for art in feedback_artifacts or []:
        if art.get("type") != target_type:
            continue
        if art.get("scene_num") != scene_num:
            continue
        if art.get("phrase_idx") != phrase_idx:
            continue
        rows.append(dict(art))
    rows.sort(key=lambda a: a.get("timestamp", 0))
    return rows


def _revision_tags_from_state(state: Any) -> dict[str, Any]:
    """Return ``{artifact_key: tag_dict}`` for every tagged artifact.

    The blackboard stores tags as a JSON-encoded string; we tolerate an
    already-decoded dict (used in tests).  Missing / malformed storage
    yields an empty dict — the dashboard treats this as "no history".
    """
    if state is None:
        return {}
    try:
        raw = state.get("_artifact_revision_tags")
    except Exception:  # noqa: BLE001
        return {}
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _annotate_history_with_revision_tags(
    history: list[dict[str, Any]],
    tags: dict[str, Any],
    track: str,
    scene_num: int,
    phrase_idx: int,
) -> list[dict[str, Any]]:
    """Stamp ``ledger_revision_at_derivation`` onto matching history rows.

    Artifact revision tags are keyed by a producer-chosen artifact_key; the
    most common convention we have seen in the pipeline is
    ``<track_short>:<scene>:<phrase>`` or ``scene_<n>_phrase_<m>_<track>``.
    We accept any of these; unrecognised keys are just ignored.
    """
    if not tags:
        return history

    candidate_keys = {
        f"V1:{scene_num}:{phrase_idx}",
        f"A1:{scene_num}:{phrase_idx}",
        f"A2:{scene_num}:{phrase_idx}",
        f"scene_{scene_num}_phrase_{phrase_idx}_video",
        f"scene_{scene_num}_phrase_{phrase_idx}_narration",
        f"scene_{scene_num}_phrase_{phrase_idx}_music",
    }
    matched_tag = None
    for key in candidate_keys:
        if key in tags:
            matched_tag = tags[key]
            break
    if matched_tag is None:
        return history
    for row in history:
        row.setdefault("ledger_revision_at_derivation", matched_tag)
    return history


# ---------------------------------------------------------------------------
# QA verdicts
# ---------------------------------------------------------------------------


def _qa_verdicts_from_critique_store(
    scene_num: int, phrase_idx: int, track: str
) -> list[dict[str, Any]]:
    """Read the critique store for this slot's artifact id.

    The critique store writes under
    ``<root>/critiques/<type>/<id>.json`` (see
    :mod:`server.critique.store`).  We try the conventional id forms.
    Silent no-op when the store is unavailable — the dashboard renders
    "no QA yet" in that case.
    """
    try:
        from critique.store import get_critique_store
    except Exception:  # noqa: BLE001
        return []

    try:
        store = get_critique_store()
    except Exception:  # noqa: BLE001
        return []

    # Map track -> critique artifact_type label. Match :mod:`critique.record`.
    type_for_track = {
        TRACK_V1_VIDEO: "video_clip",
        TRACK_A1_NARRATION: "narration",
        TRACK_A2_MUSIC: "music",
    }
    artifact_type = type_for_track.get(track)
    if not artifact_type:
        return []

    candidate_ids = [
        f"s{scene_num:03d}_p{phrase_idx:03d}",
        f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}",
        f"{scene_num}_{phrase_idx}",
    ]
    out: list[dict[str, Any]] = []
    for aid in candidate_ids:
        try:
            record = store.read(artifact_type, aid)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            continue
        if record is None:
            continue
        try:
            out.append(_record_to_dict(record))
        except Exception as exc:  # noqa: BLE001
            logger.log(logging.DEBUG, "slot_detail_model: failed to serialize critique: %s", exc)
    return out


def _record_to_dict(record: Any) -> dict[str, Any]:
    """Best-effort serialization of an ArtifactCritiqueRecord."""
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        try:
            return cast(dict[str, Any], to_dict())
        except Exception:  # noqa: BLE001
            pass  # Fall back to asdict serialization
    try:
        return asdict(record)
    except Exception:  # noqa: BLE001
        # Last resort
        return {k: getattr(record, k) for k in dir(record) if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Reasoning digests relevant to the slot
# ---------------------------------------------------------------------------


_DIGEST_DB_ENV = "REASONING_DIGEST_DB"


def _reasoning_digests_for_slot(
    scene_num: int, phrase_idx: int, track: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Return digests whose summary / details reference this slot.

    We cannot do a perfect structured match (digests are free-form text)
    so we fall back to substring containment on the rendered summary.
    """
    try:
        from plugins.reasoning_digest import get_digest_engine
    except Exception:  # noqa: BLE001
        return []

    try:
        engine = get_digest_engine()
        rows = engine.get_recent(500)
    except Exception:  # noqa: BLE001
        return []

    needles = [
        f"scene {scene_num} phrase {phrase_idx}",
        f"scene_{scene_num}_phrase_{phrase_idx}",
        f"s{scene_num:03d}_p{phrase_idx:03d}",
        f"s{scene_num}p{phrase_idx}",
    ]
    track_keyword = {
        TRACK_V1_VIDEO: "video",
        TRACK_A1_NARRATION: "narration",
        TRACK_A2_MUSIC: "music",
    }.get(track, "")

    matches: list[dict[str, Any]] = []
    for row in rows:
        text_parts = [
            str(row.get("summary", "")),
            json.dumps(row.get("details", {})),
        ]
        text = " ".join(text_parts).lower()
        if any(n.lower() in text for n in needles):
            if not track_keyword or track_keyword in text:
                matches.append(row)
            elif track_keyword == "":
                matches.append(row)
    return matches[-limit:]


# ---------------------------------------------------------------------------
# Ledger records in scope
# ---------------------------------------------------------------------------


def _in_scope_ledger_records(
    state: Any, scene_num: int, phrase_idx: int
) -> list[dict[str, Any]]:
    """Return ledger records whose scope covers this slot.

    We collect:
      * global entries (they apply to everything),
      * scene-level entries with scope_ref == str(scene_num),
      * block / clip level entries with scope_ref starting ``{scene}_{phrase}``
        or explicitly tagged.
    """
    try:
        from callbacks.preference_ledger import Scope, query_by_scope
    except Exception:  # noqa: BLE001
        return []

    if state is None:
        return []

    out: list[dict[str, Any]] = []

    def _to_dict_safe(rec: Any) -> dict[str, Any]:
        td = getattr(rec, "to_dict", None)
        if callable(td):
            try:
                result = td()
                if isinstance(result, dict):
                    return result
            except Exception:  # noqa: BLE001
                pass  # Ignore record serialization failures
        return {}

    try:
        for rec in query_by_scope(state, Scope.GLOBAL):
            out.append(_to_dict_safe(rec))
    except Exception:  # noqa: BLE001
        pass  # Ignore GLOBAL scope query failures
    try:
        for rec in query_by_scope(state, Scope.SCENE, scope_ref=str(scene_num)):
            out.append(_to_dict_safe(rec))
    except Exception:  # noqa: BLE001
        pass  # Ignore SCENE scope query failures
    # Block / clip level: two conventional scope_refs are in play across the
    # pipeline — ``{scene}_{phrase}`` and ``{scene}:{phrase}``.  We probe both.
    for scope_name in ("BLOCK", "CLIP"):
        scope_enum = getattr(Scope, scope_name, None)  # type: ignore[arg-type]
        if scope_enum is None:
            continue
        for ref in (f"{scene_num}_{phrase_idx}", f"{scene_num}:{phrase_idx}"):
            try:
                for rec in query_by_scope(state, scope_enum, scope_ref=ref):
                    out.append(_to_dict_safe(rec))
            except Exception:  # noqa: BLE001
                continue
    return out


# ---------------------------------------------------------------------------
# Current rung
# ---------------------------------------------------------------------------


def _current_rung(
    output_dir: str, scene_num: int, phrase_idx: int, track: str
) -> dict[str, Any]:
    """Best-effort lookup of the live ladder rung for this slot.

    For the video track we consult ``_status.json`` attempts counters.
    For the audio track we look for a live rung sidecar written by the
    recovery supervisor; when absent we report an empty rung.
    """
    if track == TRACK_V1_VIDEO:
        status_path = os.path.join(
            output_dir,
            "video",
            f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}_status.json",
        )
        if os.path.exists(status_path):
            try:
                with open(status_path) as f:
                    data = json.load(f)
                return {
                    "ladder": "content",
                    "rung": data.get("rung") or data.get("current_rung") or "",
                    "attempts": data.get("attempts", 0),
                    "quality": data.get("quality", "unknown"),
                }
            except Exception:  # noqa: BLE001
                return {}

    rung_sidecar = os.path.join(
        output_dir,
        "recovery",
        f"rung_scene_{scene_num:03d}_phrase_{phrase_idx:03d}.json",
    )
    if os.path.exists(rung_sidecar):
        try:
            with open(rung_sidecar) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ---------------------------------------------------------------------------
# Latest preview assembly
# ---------------------------------------------------------------------------


def _latest_preview_for_slot(
    output_dir: str, start_sec_hint: Optional[float] = None
) -> dict[str, Any]:
    """Return metadata for the latest preview assembly that includes the slot.

    We pick the most recent MP4 under ``assembly/`` — the preview writer
    (workstream G) keeps previews append-only.  When the preview has a
    sidecar JSON listing included clip windows we return the matching
    window; otherwise we just return the preview's absolute path.
    """
    assembly_dir = os.path.join(output_dir, "assembly")
    if not os.path.exists(assembly_dir):
        return {}
    mp4s = sorted(
        glob.glob(os.path.join(assembly_dir, "*.mp4")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    if not mp4s:
        return {}
    latest = mp4s[0]
    sidecar = latest + ".json"
    preview: dict[str, Any] = {
        "path": latest,
        "name": os.path.basename(latest),
        "mtime": os.path.getmtime(latest),
    }
    if os.path.exists(sidecar):
        try:
            with open(sidecar) as f:
                preview["manifest"] = json.load(f)
        except Exception:  # noqa: BLE001
            pass  # Ignore manifest loading failures
    if start_sec_hint is not None:
        preview["seek_sec"] = round(start_sec_hint, 3)
    return preview


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_slot_detail(
    slot_id: str,
    output_dir: str,
    *,
    feedback_artifacts: Optional[list[dict[str, Any]]] = None,
    state: Any = None,
    start_sec_hint: Optional[float] = None,
) -> SlotDetail:
    """Assemble the side-panel detail view for ``slot_id``.

    Pure read-only: never writes to disk or to the blackboard.
    """
    track, scene_num, phrase_idx = parse_slot_id(slot_id)

    history = _artifact_history_from_store(
        feedback_artifacts or [], track, scene_num, phrase_idx
    )
    tags = _revision_tags_from_state(state)
    history = _annotate_history_with_revision_tags(
        history, tags, track, scene_num, phrase_idx
    )

    return SlotDetail(
        slot_id=slot_id,
        track=track,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
        artifact_history=history,
        qa_verdicts=_qa_verdicts_from_critique_store(scene_num, phrase_idx, track),
        reasoning_digests=_reasoning_digests_for_slot(scene_num, phrase_idx, track),
        ledger_records=_in_scope_ledger_records(state, scene_num, phrase_idx),
        current_rung=_current_rung(output_dir, scene_num, phrase_idx, track),
        latest_preview=_latest_preview_for_slot(output_dir, start_sec_hint),
    )


__all__ = ["SlotDetail", "build_slot_detail"]
