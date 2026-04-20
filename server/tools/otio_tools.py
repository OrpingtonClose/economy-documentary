"""
OTIO timeline tools -- ADK FunctionTool wrappers for OpenTimelineIO operations.

All timeline mutations go through these tools. They enforce idempotency
(check for existing clips before appending) and maintain the canonical
track structure: V1_Video, A1_Narration, A2_Music.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

import opentimelineio as otio

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

# Canonical track names
TRACK_V1 = "V1_Video"
TRACK_A1 = "A1_Narration"
TRACK_A2 = "A2_Music"

_TIMELINE_DIR = os.environ.get("TIMELINE_DIR", "/tmp/documentary-pipeline/timelines")
_SCENE_ASSEMBLY_DIR = os.environ.get(
    "SCENE_ASSEMBLY_DIR", "/tmp/documentary-pipeline/scene_assemblies"
)

# Module-level lock to protect OTIO file read-modify-write cycles against
# concurrent tool calls (parallel_tool_calls=True is the default).
_otio_lock = threading.Lock()


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _find_scene_from_state(state: dict, scene_num: int) -> Optional[dict]:
    """Look up the scene dict matching ``scene_num`` from ``state['scenes']``.

    Returns ``None`` if scenes aren't populated yet (which is valid
    during the very first scenario pass).  Used by the per-moment
    validators in :mod:`tools.otio_moments` so they don't need to thread
    the scene dict through every call site.
    """
    try:
        from callbacks.deterministic_steps import extract_json_array
    except Exception:  # noqa: BLE001 — keep otio_tools importable standalone
        extract_json_array = None  # type: ignore[assignment]

    raw = state.get("scenes", "[]") if state else "[]"
    scenes = None
    if isinstance(raw, list):
        # Already a list on state -- mirror whisperx_oracle_callback's
        # handling so per-moment audio validation keeps working when the
        # caller stores scenes as a native list instead of a JSON string.
        scenes = raw
    elif extract_json_array is not None:
        scenes = extract_json_array(str(raw))
    if scenes is None:
        try:
            scenes = json.loads(str(raw))
        except Exception:  # noqa: BLE001
            return None

    if not isinstance(scenes, list):
        return None
    for s in scenes:
        if not isinstance(s, dict):
            continue
        try:
            if int(s.get("scene_num", 0) or 0) == int(scene_num):
                return s
        except (TypeError, ValueError):
            continue
    return None


def _primary_narration_duration_for_phrase(
    timeline,
    scene_num: int,
    phrase_idx: int,
) -> Optional[float]:
    """Narration duration for scene N, phrase K (primary language only).

    Phrase_idx maps to the (phrase_idx)-th narration clip of the scene
    after filtering out alternate-language clips (voice ends with ``_EN``).
    Returns ``None`` when the scene has no narration yet (audio stage
    pending) or when phrase_idx exceeds the scene's narration count.
    """
    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break
    if narration_track is None:
        return None

    durations: list[float] = []
    for item in narration_track:
        if not isinstance(item, otio.schema.Clip) or not item.source_range:
            continue
        meta = item.metadata.get("documentary", {})
        if meta.get("scene_num") != scene_num:
            continue
        voice = meta.get("voice", "")
        if voice.endswith("_EN"):
            continue
        durations.append(item.source_range.duration.to_seconds())

    if not durations or phrase_idx < 0 or phrase_idx >= len(durations):
        return None
    return durations[phrase_idx]


def _scene_has_empty_placeholder_gaps(timeline, scene_num: int) -> bool:
    """Return True when V1_Video still has an ``status=empty`` gap for this scene."""
    for track in timeline.tracks:
        if track.name != TRACK_V1:
            continue
        for item in track:
            if not isinstance(item, otio.schema.Gap):
                continue
            meta = item.metadata.get("documentary", {})
            if meta.get("scene_num") != scene_num:
                continue
            if meta.get("status") == "empty":
                return True
    return False


def _count_primary_narration_clips(timeline, scene_num: int) -> int:
    """Count narration clips for a scene on A1_Narration (primary language only).

    ``*_EN`` voices are alternate-language siblings and do not have their
    own video phrase, so they are excluded.
    """
    count = 0
    for track in timeline.tracks:
        if track.name != TRACK_A1:
            continue
        for item in track:
            if not isinstance(item, otio.schema.Clip):
                continue
            meta = item.metadata.get("documentary", {})
            if meta.get("scene_num") != scene_num:
                continue
            voice = str(meta.get("voice", ""))
            if voice.endswith("_EN"):
                continue
            count += 1
    return count


def _count_video_clips(timeline, scene_num: int) -> int:
    """Count video clips on V1_Video for a scene, ignoring extension sub-clips.

    Extension / sub-clips (``sub_idx`` set) are generated on top of an
    existing phrase to lengthen it and do not introduce new phrase
    indices, so they must not inflate the completeness count.
    """
    count = 0
    for track in timeline.tracks:
        if track.name != TRACK_V1:
            continue
        for item in track:
            if not isinstance(item, otio.schema.Clip):
                continue
            meta = item.metadata.get("documentary", {})
            if meta.get("scene_num") != scene_num:
                continue
            if meta.get("sub_idx") is not None:
                # Extension clip — decorates an existing phrase.
                continue
            count += 1
    return count


def _scene_is_video_complete(timeline, scene_num: int) -> bool:
    """Return True when every narration phrase in the scene has a video clip.

    The naive "no empty placeholder gap remaining" check is insufficient
    because ``create_timeline`` only creates ONE placeholder gap per
    scene regardless of how many phrases (voices) will be generated.
    After ``add_video_clip`` replaces that lone gap on phrase_idx=0, a
    multi-voice scene would falsely look complete (see PR #115 review).

    A scene is complete when:

    * no ``status=empty`` placeholder gap remains for the scene, AND
    * at least one narration clip and one video clip exist, AND
    * video clip count >= primary-language narration clip count.
    """
    if _scene_has_empty_placeholder_gaps(timeline, scene_num):
        return False
    narration_count = _count_primary_narration_clips(timeline, scene_num)
    video_count = _count_video_clips(timeline, scene_num)
    if narration_count == 0 or video_count == 0:
        return False
    return video_count >= narration_count


def _timeline_path(topic: str) -> str:
    os.makedirs(_TIMELINE_DIR, exist_ok=True)
    # Sanitise all characters that are problematic in file paths
    safe_topic = topic
    for ch in " /:\\?*\"<>|'":
        safe_topic = safe_topic.replace(ch, "_")
    safe_topic = safe_topic[:50]
    return os.path.join(_TIMELINE_DIR, f"{safe_topic}.otio")


def create_timeline(
    topic: str,
    num_scenes: int,
    tool_context=None,
) -> str:
    """Create a new OTIO timeline with the canonical track structure.

    Idempotent: if ``_timeline_path`` already exists in the session state
    the call is a no-op and returns the existing path.  This prevents the
    scenario generator from overwriting the timeline on evaluator-loop
    re-runs inside the LoopAgent.

    Args:
        topic: Documentary topic name.
        num_scenes: Number of scenes to create placeholder gaps for.

    Returns:
        JSON string with timeline path and structure summary.
    """
    # Guard: skip re-creation if timeline already exists in session state
    if tool_context:
        existing_path = tool_context.state.get("_timeline_path", "")
        if existing_path and os.path.exists(existing_path):
            logger.info(
                "Timeline already exists at %s — skipping re-creation",
                existing_path,
            )
            return json.dumps(
                {
                    "timeline_path": existing_path,
                    "tracks": [TRACK_V1, TRACK_A1, TRACK_A2],
                    "num_scenes": num_scenes,
                    "status": "already_exists",
                }
            )

    with _otio_lock:
        timeline = otio.schema.Timeline(name=f"Documentary: {topic}")

        # Create video track with gaps for each scene
        video_track = otio.schema.Track(name=TRACK_V1, kind=otio.schema.TrackKind.Video)
        for i in range(1, num_scenes + 1):
            gap = otio.schema.Gap(
                name=f"scene_{i:03d}_video",
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(0, 24),
                ),
            )
            gap.metadata["documentary"] = {"scene_num": i, "status": "empty"}
            video_track.append(gap)

        # Create narration track
        narration_track = otio.schema.Track(
            name=TRACK_A1, kind=otio.schema.TrackKind.Audio
        )

        # Create music track
        music_track = otio.schema.Track(
            name=TRACK_A2, kind=otio.schema.TrackKind.Audio
        )

        timeline.tracks.append(video_track)
        timeline.tracks.append(narration_track)
        timeline.tracks.append(music_track)

        # ARCH-E1: every newly-minted timeline starts its lifecycle in
        # the ``draft`` state.  The crystallisation to ``authoritative``
        # happens at the end of the audio stage once narration
        # reconciliation locks pacing (see
        # ``callbacks.otio_state.authoritative_transition_callback``).
        timeline.metadata.setdefault("documentary", {})
        timeline.metadata["documentary"]["state"] = "draft"
        timeline.metadata["documentary"]["state_reason"] = "timeline_created"

        path = _timeline_path(topic)
        _ensure_dir(path)
        otio.adapters.write_to_file(timeline, path)

    # Store path in tool_context state if available
    if tool_context:
        tool_context.state["_timeline_path"] = path
        # Mirror onto the blackboard for in-process enforcement.
        try:
            from callbacks.otio_state import mark_timeline_draft

            mark_timeline_draft(tool_context.state, timeline_path=path)
        except Exception as exc:  # noqa: BLE001 — on-disk stamp is the SSOT
            logger.warning(
                "create_timeline: failed to mark blackboard otio_state=draft: %r",
                exc,
            )

    logger.info("Created OTIO timeline: %s (%d scenes)", path, num_scenes)

    return json.dumps(
        {
            "timeline_path": path,
            "tracks": [TRACK_V1, TRACK_A1, TRACK_A2],
            "num_scenes": num_scenes,
            "status": "created",
        }
    )


def clear_narration_track(timeline_path: str, tool_context=None) -> int:
    """Remove all clips and gaps from the A1_Narration track.

    Called at the start of a timing loop re-iteration to prevent
    duplicate narration clips from accumulating across iterations.

    ARCH-E1 mutation guard: clearing narration is a mutation of the
    authoritative baseline.  Once the timeline has crystallised
    (state == ``authoritative``) this operation is forbidden unless a
    REPLACE/EXTEND escalation is open on the blackboard.  Callers that
    legitimately need to re-derive audio (dual-axis escalation) must
    open the escalation window via
    :func:`callbacks.otio_state.begin_escalation` (or reset the state
    to draft via :func:`callbacks.otio_state.reset_to_draft`) before
    calling this function.

    Args:
        timeline_path: Path to the OTIO timeline file.
        tool_context: Optional ADK ``ToolContext`` carrying blackboard
            state.  When provided, the mutation guard runs against
            ``tool_context.state``.  When absent (legacy callers), the
            guard is skipped — callers in that path are responsible for
            running during the draft phase only.

    Returns:
        Number of items removed.
    """
    if tool_context is not None:
        from callbacks.otio_state import guard_authoritative_mutation

        guard_authoritative_mutation(
            tool_context.state, operation="clear_narration_track"
        )

    if not timeline_path or not os.path.exists(timeline_path):
        return 0

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        narration_track = None
        for track in timeline.tracks:
            if track.name == TRACK_A1:
                narration_track = track
                break

        if narration_track is None:
            return 0

        count = len(narration_track)
        if count > 0:
            # Clear all items (clips + gaps) from the narration track
            del narration_track[:]
            otio.adapters.write_to_file(timeline, timeline_path)
            logger.info(
                "track=<%s>, items_removed=<%d> | cleared narration track "
                "for timing loop re-iteration",
                TRACK_A1, count,
            )
        return count


def add_narration_clip(
    scene_num: int,
    voice: str,
    wav_path: str,
    duration: float,
    tool_context=None,
) -> str:
    """Add a narration clip to A1_Narration track.

    Idempotent: checks for existing clip with same scene_num + voice
    before appending.

    Args:
        scene_num: Scene number (1-based).
        voice: Voice role identifier (e.g., "V1", "V2", "V3").
        wav_path: Path to the generated WAV file.
        duration: Duration in seconds.

    Returns:
        JSON string with clip details.
    """
    state = tool_context.state if tool_context else {}

    # ARCH-E1 mutation guard: adding a narration clip mutates the
    # authoritative baseline (the narration track IS the timing law).
    # Forbidden once the timeline has crystallised unless a REPLACE/EXTEND
    # escalation is open.
    if tool_context is not None:
        from callbacks.otio_state import guard_authoritative_mutation

        guard_authoritative_mutation(state, operation="add_narration_clip")

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found. Create one first."})

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        narration_track = None
        for track in timeline.tracks:
            if track.name == TRACK_A1:
                narration_track = track
                break

        if narration_track is None:
            return json.dumps({"error": "A1_Narration track not found"})

        clip_name = f"scene_{scene_num:03d}_{voice}"

        # Idempotency check: don't add duplicates
        for item in narration_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_name:
                return json.dumps(
                    {
                        "status": "already_exists",
                        "clip_name": clip_name,
                        "message": "Clip already exists, skipping duplicate",
                    }
                )

        clip = otio.schema.Clip(
            name=clip_name,
            media_reference=otio.schema.ExternalReference(target_url=wav_path),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(duration * 24, 24),
            ),
        )
        clip.metadata["documentary"] = {
            "scene_num": scene_num,
            "voice": voice,
            "type": "narration",
        }

        narration_track.append(clip)
        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Added narration clip: %s (%.2fs)", clip_name, duration)

    # ── Per-moment OTIO compliance (#84) ──────────────────────────────
    # Validate the freshly-persisted clip against its scene target
    # IMMEDIATELY instead of batching until post-stage.  Fail loud if
    # the clip exceeds the per-voice tolerance — this is the hook that
    # catches phrase-2-is-13.84s-but-scene-target-is-10s drift the
    # moment it happens, not after 21 wasted GPU clips.
    _run_per_moment_audio_check(
        state=state,
        scene_num=scene_num,
        voice=voice,
        actual_duration_sec=duration,
    )

    return json.dumps(
        {
            "status": "added",
            "clip_name": clip_name,
            "duration": duration,
            "wav_path": wav_path,
        }
    )


def _run_per_moment_audio_check(
    state: dict,
    scene_num: int,
    voice: str,
    actual_duration_sec: float,
) -> None:
    """Run the per-moment audio validator and escalate on failure.

    Kept as a free function (not an inline block) so ``add_narration_clip``
    stays focused on OTIO mutation.  Any validation error is routed
    through :func:`recovery.escalate_pipeline_error` which respects
    auto-approve mode for test runs.
    """
    from tools.otio_moments import validate_audio_duration_vs_scene_target

    scene = _find_scene_from_state(state, scene_num)
    if scene is None:
        # Scenes not yet in state (e.g. early in scenario phase); the
        # stage-boundary timeline_guardian will still run at end of phase.
        return

    err = validate_audio_duration_vs_scene_target(
        scene_num=scene_num,
        voice=voice,
        actual_duration_sec=actual_duration_sec,
        scene=scene,
    )
    if err is None:
        return

    logger.error("PER-MOMENT AUDIO VIOLATION: %s", err)
    state["otio_violation"] = f"per_moment_audio: {err}"
    try:
        from recovery import escalate_pipeline_error
    except Exception:  # noqa: BLE001 — keep module importable in tests
        raise RuntimeError(f"per-moment audio violation: {err}") from None

    response = escalate_pipeline_error(
        operation_name=f"per_moment_audio_scene_{scene_num}_{voice}",
        error_msg=err,
        severity="critical",
        default_action="skip",
        diagnosis_hint=(
            "Narration clip exceeds its per-voice duration budget. "
            "The scene's duration_sec target cannot accommodate it. "
            "The trim loop already accepted the clip as-is after exhausting retries."
        ),
        agent_policy_type="otio",
    )
    action = response.get("action", "abort")
    if action in ("skip", "retry_with_fix"):
        logger.warning(
            "PER-MOMENT AUDIO: recovery decided '%s' for scene %d %s "
            "(%.2fs over budget) — continuing pipeline",
            action, scene_num, voice, actual_duration_sec,
        )
        return
    if action == "abort":
        raise RuntimeError(f"per-moment audio violation (abort): {err}")


def add_narration_gap(
    scene_num: int,
    duration: float,
    gap_type: str,
    gap_index: int = 0,
    tool_context=None,
) -> str:
    """Add an intentional silence Gap to the A1_Narration track.

    These gaps are PLANNED pauses — breathing room between voice segments
    or transitions between scenes.  They are part of the immutable OTIO
    contract and must be rendered faithfully by the assembler.

    Args:
        scene_num: Scene number this gap belongs to (for metadata).
        duration: Duration of the silence in seconds.
        gap_type: One of "inter_voice" (pause between V1→V2→V3 within a
                  scene) or "inter_scene" (transition between scenes).
        gap_index: Unique index for this gap within the scene (for
                   idempotency — prevents duplicates on pipeline restart).

    Returns:
        JSON string with gap details.

    Raises:
        OtioStateViolation: If the timeline is ``authoritative`` and no
            REPLACE/EXTEND escalation window is open.  Gaps on the
            narration track are part of the timing law; once the timeline
            has crystallised, downstream stages must not insert silence.
    """
    state = tool_context.state if tool_context else {}

    if tool_context is not None:
        from callbacks.otio_state import guard_authoritative_mutation
        guard_authoritative_mutation(state, operation="add_narration_gap")

    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found. Create one first."})

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        narration_track = None
        for track in timeline.tracks:
            if track.name == TRACK_A1:
                narration_track = track
                break

        if narration_track is None:
            return json.dumps({"error": "A1_Narration track not found"})

        gap_name = f"scene_{scene_num:03d}_{gap_type}_{gap_index:03d}"

        # Idempotency check: don't add duplicates on pipeline restart
        for item in narration_track:
            if isinstance(item, otio.schema.Gap) and item.name == gap_name:
                return json.dumps({
                    "status": "already_exists",
                    "gap_name": gap_name,
                    "message": "Gap already exists, skipping duplicate",
                })

        gap = otio.schema.Gap(
            name=gap_name,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(duration * 24, 24),
            ),
        )
        gap.metadata["documentary"] = {
            "scene_num": scene_num,
            "gap_type": gap_type,
            "type": "silence",
        }
        narration_track.append(gap)
        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Added narration gap: %s (%.2fs, type=%s)", gap_name, duration, gap_type)
    return json.dumps({
        "status": "added",
        "gap_name": gap_name,
        "duration": duration,
        "gap_type": gap_type,
    })


def add_video_gap(
    scene_num: int,
    duration: float,
    gap_type: str,
    gap_index: int = 0,
    tool_context=None,
) -> str:
    """REMOVED: V1_Video may not contain Gaps.

    The V1_Video track has NO intentional gaps by architectural
    invariant (ARCH-F3 / strict_assembler.ensure_item_is_not_gap).
    Production sizes each video clip to cover the narration PLUS
    the following silence gap on A1_Narration — see
    :func:`get_video_slot_durations`.  Any Gap on V1_Video at render
    time raises :class:`UnpluggedGapError`; no freeze-frame, silent-fill
    or black-frame fabrication is permitted.

    Inter-voice / inter-scene silence belongs on A1_Narration and is
    added by :func:`add_narration_gap`; the assembler renders those
    narration Gaps as real silent WAV, which is faithful to the OTIO
    contract (the declared content IS silence).

    This function previously inserted Gaps onto V1_Video as a planned
    "black frame" pause.  That was always a latent violation of the
    no-Gap invariant on V1_Video and of the Media Immutability
    Invariant (fabricating filler media at render time).  It is now
    a hard error so callers fail loud rather than silently producing
    an unassemblable timeline.

    Raises:
        NotImplementedError: always.
    """
    raise NotImplementedError(
        "add_video_gap is forbidden. V1_Video must not contain Gaps "
        "(ARCH-F3 / strict_assembler.ensure_item_is_not_gap). Video clips "
        "must be sized to cover narration + following silence via "
        "get_video_slot_durations; inter-voice / inter-scene silence lives "
        "on A1_Narration via add_narration_gap."
    )


def get_narration_durations_by_scene(tool_context=None) -> dict:
    """Read the OTIO timeline and return narration durations per scene.

    Returns a dict mapping scene_num -> list of (voice, duration_sec) tuples,
    ordered as they appear on the A1_Narration track.  Only includes Clip
    items — Gap items (inter-voice/inter-scene pauses) are excluded.

    This is the AUTHORITATIVE source for how long video clips must be.
    Video concepts MUST be sized to match these durations.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return {}

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break

    if narration_track is None:
        return {}

    result: dict[int, list[tuple[str, float]]] = {}
    for item in narration_track:
        if isinstance(item, otio.schema.Clip) and item.source_range:
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            voice = meta.get("voice", "")
            dur = item.source_range.duration.to_seconds()
            # Skip alternate language clips (e.g. V1_EN in dual mode)
            if sn > 0 and dur > 0 and not voice.endswith("_EN"):
                result.setdefault(sn, []).append((voice, dur))

    return result


def get_video_slot_durations(tool_context=None) -> dict:
    """Read the OTIO narration track and return VIDEO durations per scene.

    Unlike get_narration_durations_by_scene (which returns narration-only
    durations), this returns each voice's FULL TIME SLOT: the narration
    clip duration PLUS any Gap that follows it on the narration track.

    This is the AUTHORITATIVE source for how long each video clip must be.
    The video track has NO gaps — each video clip must cover the narration
    plus the following silence so the viewer sees continuous footage while
    the narrator pauses.

    Returns a dict mapping scene_num -> list of (voice, slot_duration_sec)
    tuples, ordered as they appear on the A1_Narration track.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return {}

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break

    if narration_track is None:
        return {}

    # Walk the narration track: for each Clip, accumulate its duration
    # plus any immediately following Gap(s) to get the full time slot.
    items = list(narration_track)
    result: dict[int, list[tuple[str, float]]] = {}

    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, otio.schema.Clip) and item.source_range:
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            voice = meta.get("voice", "")
            clip_dur = item.source_range.duration.to_seconds()

            # Skip alternate language clips (e.g. V1_EN in dual mode)
            if sn > 0 and clip_dur > 0 and not voice.endswith("_EN"):
                # Accumulate following Gap duration(s)
                gap_dur = 0.0
                j = i + 1
                while j < len(items) and isinstance(items[j], otio.schema.Gap):
                    gap_item = items[j]
                    if gap_item.source_range:
                        gap_dur += gap_item.source_range.duration.to_seconds()
                    j += 1

                slot_dur = clip_dur + gap_dur
                result.setdefault(sn, []).append((voice, round(slot_dur, 3)))
        i += 1

    return result


def _gatekeeper_check_video_clip(
    timeline,
    scene_num: int,
    phrase_idx: int,
    source_range: float,
) -> Optional[str]:
    """OTIO Gatekeeper: cross-validate a video clip against narration timing.

    AUDIT-ONLY: this check logs warnings but does NOT block the OTIO write.
    The real gatekeeper validation runs as a batch AFTER all artifacts are
    uploaded to B2 (see deterministic_production_callback).  This function
    exists for early warning in the logs only.

    Checks:
    1. source_range > 0
    2. source_range <= available narration duration for this scene
    3. phrase_idx corresponds to a real narration phrase (not phantom)

    Returns None if valid, or a warning string describing the issue.
    """
    if source_range <= 0:
        return (
            f"source_range={source_range:.3f}s for scene {scene_num} "
            f"phrase {phrase_idx} — must be > 0"
        )

    # Cross-check against narration track
    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break

    if narration_track is None:
        # No narration yet — cannot cross-validate.  This is acceptable
        # only if audio stage hasn't run (production should not run before
        # audio, but we don't block here — the contract validator handles
        # stage ordering).
        return None

    # Collect narration clips for this scene (exclude alternate language
    # suffixes — match primary language only for video sizing).
    scene_narrations: list[tuple[str, float]] = []
    for item in narration_track:
        if isinstance(item, otio.schema.Clip) and item.source_range:
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            voice = meta.get("voice", "")
            # Skip alternate language clips (e.g. V1_EN in dual mode)
            if sn == scene_num and not voice.endswith("_EN"):
                dur = item.source_range.duration.to_seconds()
                scene_narrations.append((voice, dur))

    if not scene_narrations:
        # No narration for this scene — unusual but not gatekeeper's job
        # to enforce stage ordering.
        return None

    # Check phrase_idx is within bounds
    if phrase_idx >= len(scene_narrations):
        return (
            f"phrase_idx={phrase_idx} for scene {scene_num} exceeds "
            f"narration phrase count ({len(scene_narrations)}). "
            f"Video must have exactly one clip per narration phrase."
        )

    # Check source_range matches corresponding narration duration (1s tolerance).
    # Account for LTX-2.3 10s cap: when narration exceeds 10s, video at 10s
    # is the best the model can do — don't reject in that case.
    _LTX_CAP = 10.0
    expected_dur = scene_narrations[phrase_idx][1]
    drift = abs(source_range - expected_dur)
    cap_explained = (
        expected_dur > _LTX_CAP
        and source_range >= _LTX_CAP - 0.5
    )
    if drift > 1.0 and not cap_explained:
        return (
            f"scene {scene_num} phrase {phrase_idx}: video source_range "
            f"({source_range:.2f}s) does not match narration duration "
            f"({expected_dur:.2f}s) — drift {drift:.2f}s > 1s. "
            f"Video clips must be sized to match narration."
        )

    return None


def add_video_clip(
    scene_num: int,
    phrase_idx: int,
    mp4_path: str,
    duration: float,
    source_range: float,
    available_range: float,
    lora_id: str,
    sub_idx: int | None = None,
    tool_context=None,
) -> str:
    """Add a video clip to V1_Video track.

    GATEKEEPER (audit-only): Before adding, cross-validates the clip's
    source_range against the narration track and logs warnings.  Does NOT
    block the write — the batch gatekeeper in the production callback
    validates after all artifacts are uploaded to B2 (audit trail).

    Idempotent: checks for existing clip with same scene_num + phrase_idx + sub_idx.

    Args:
        scene_num: Scene number (1-based).
        phrase_idx: Visual phrase index within the scene.
        mp4_path: Path to the generated MP4 file.
        duration: Target duration in seconds.
        source_range: Source range duration in seconds.
        available_range: Available range duration in seconds.
        lora_id: LoRA identifier used for generation.
        sub_idx: Optional sub-clip index for split concepts (0-based).

    Returns:
        JSON string with clip details.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found. Create one first."})

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        video_track = None
        for track in timeline.tracks:
            if track.name == TRACK_V1:
                video_track = track
                break

        if video_track is None:
            return json.dumps({"error": "V1_Video track not found"})

        # GATEKEEPER (audit-only): cross-validate against narration.
        # This does NOT block the write — the batch gatekeeper in
        # deterministic_production_callback runs after B2 upload and
        # handles rejects.  We log here for early visibility only.
        gate_warning = _gatekeeper_check_video_clip(
            timeline, scene_num, phrase_idx, source_range,
        )
        if gate_warning:
            logger.warning(
                "OTIO GATEKEEPER WARNING (audit-only): video clip scene %d phrase %d: %s",
                scene_num, phrase_idx, gate_warning,
            )

        suffix = f"_sub{sub_idx:02d}" if sub_idx is not None else ""
        clip_name = f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}{suffix}"

        # Idempotency check
        for item in video_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_name:
                return json.dumps(
                    {
                        "status": "already_exists",
                        "clip_name": clip_name,
                        "message": "Clip already exists, skipping duplicate",
                    }
                )

        clip = otio.schema.Clip(
            name=clip_name,
            media_reference=otio.schema.ExternalReference(
                target_url=mp4_path,
                available_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(available_range * 24, 24),
                ),
            ),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(source_range * 24, 24),
            ),
        )
        clip.metadata["documentary"] = {
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "lora_id": lora_id,
            "available_range": available_range,
            "type": "video",
            **(({"sub_idx": sub_idx}) if sub_idx is not None else {}),
        }

        # Insert clip in correct sorted position by (scene_num, phrase_idx).
        # First, try replacing the scene's placeholder gap (only for phrase 0).
        # IMPORTANT: only replace gaps with status="empty" (placeholders from
        # create_timeline), NOT inter-voice/inter-scene structural gaps.
        replaced = False
        if phrase_idx == 0:
            for i, item in enumerate(video_track):
                if isinstance(item, otio.schema.Gap):
                    gap_meta = item.metadata.get("documentary", {})
                    if (gap_meta.get("scene_num") == scene_num
                            and gap_meta.get("status") == "empty"):
                        video_track[i] = clip
                        replaced = True
                        break

        if not replaced:
            # Find correct insertion position based on (scene_num, phrase_idx).
            insert_pos = len(video_track)  # default: append at end
            for i, item in enumerate(video_track):
                meta = item.metadata.get("documentary", {})
                item_scene = meta.get("scene_num", 0)
                item_phrase = meta.get("phrase_idx", 0)
                item_sub = meta.get("sub_idx", -1)
                cur_sub = sub_idx if sub_idx is not None else -1
                if (item_scene, item_phrase, item_sub) > (scene_num, phrase_idx, cur_sub):
                    insert_pos = i
                    break

            video_track.insert(insert_pos, clip)

        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info(
        "Added video clip: %s (source_range=%.2fs, avail=%.2fs)",
        clip_name, source_range, available_range,
    )

    # ── Per-moment OTIO compliance (#84, #85) ─────────────────────────
    # Validate the freshly-persisted video clip against its narration
    # duration IMMEDIATELY instead of waiting for post-production batch.
    # On shortfall (video < audio), fire the extension-clip escalation
    # to supervisor_escalate (W3) so the LLM can choose to generate an
    # extension clip rather than us having to discover the shortfall
    # after the fact.
    _run_per_moment_video_check(
        state=state,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
        video_source_range_sec=source_range,
    )

    # ── Per-scene OTIO assembly check (#84) ───────────────────────────
    # If this clip just closed out the scene (all placeholder gaps are
    # now filled), persist a standalone scene-assembly artifact and run
    # its own compliance check before the next scene starts work.
    _maybe_run_scene_assembly_check(
        state=state,
        scene_num=scene_num,
    )

    return json.dumps(
        {
            "status": "added",
            "clip_name": clip_name,
            "duration": duration,
            "source_range": source_range,
            "available_range": available_range,
            "lora_id": lora_id,
        }
    )


def _run_per_moment_video_check(
    state: dict,
    scene_num: int,
    phrase_idx: int,
    video_source_range_sec: float,
) -> None:
    """Run the per-moment video validator + extension escalation.

    Reads the narration duration for this scene+phrase from OTIO and
    compares against ``video_source_range_sec``.  On shortfall, fires
    :func:`tools.otio_moments.fire_extension_escalation` so the
    supervisor can choose ``generate_extension_clip``.

    The escalation itself is non-raising (it routes through W3) — the
    loop-until-pass behaviour is implemented by re-calling
    ``add_video_clip`` after the extension clip lands.
    """
    from tools.otio_moments import (
        fire_extension_escalation,
        validate_video_duration_vs_audio,
    )

    timeline_path = state.get("_timeline_path", "") if state else ""
    if not timeline_path or not os.path.exists(timeline_path):
        return

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    audio_dur = _primary_narration_duration_for_phrase(
        timeline, scene_num, phrase_idx,
    )
    if audio_dur is None:
        # Narration for this phrase isn't on the timeline yet — e.g.
        # production running ahead of audio (shouldn't happen, but
        # don't block the OTIO write).  The stage-boundary guardian
        # will catch the ordering violation.
        return

    err = validate_video_duration_vs_audio(
        scene_num=scene_num,
        phrase_idx=phrase_idx,
        video_duration_sec=video_source_range_sec,
        audio_duration_sec=audio_dur,
    )
    if err is None:
        return

    logger.error("PER-MOMENT VIDEO VIOLATION: %s", err)
    state["otio_violation"] = f"per_moment_video: {err}"
    fire_extension_escalation(
        state=state,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
        video_duration_sec=video_source_range_sec,
        audio_duration_sec=audio_dur,
    )


def _maybe_run_scene_assembly_check(state: dict, scene_num: int) -> None:
    """Run the scene-level OTIO assembly check when the scene completes.

    A scene is "complete" when ``_scene_is_video_complete`` returns True —
    that is, every primary-language narration clip in the scene has a
    corresponding video clip on V1_Video (not just the single placeholder
    gap — multi-voice scenes only have one placeholder that's consumed
    by phrase_idx=0, so relying on the placeholder alone would fire the
    assembly check prematurely on phrase 0 of any multi-voice scene).

    When complete, we persist a standalone ``scene_NNN_assembly.otio``
    artifact and validate it.
    """
    from tools.otio_moments import (
        persist_scene_assembly_artifact,
        validate_scene_assembly,
    )

    timeline_path = state.get("_timeline_path", "") if state else ""
    if not timeline_path or not os.path.exists(timeline_path):
        return

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    if not _scene_is_video_complete(timeline, scene_num):
        # Scene still has pending phrases; defer the assembly check
        # until every narration clip has a matching video clip.
        return

    # Persist standalone scene artifact (paper trail for #70 — every
    # scene assembly becomes its own addressable OTIO file with a
    # compliance verdict).
    try:
        os.makedirs(_SCENE_ASSEMBLY_DIR, exist_ok=True)
        artifact_path = persist_scene_assembly_artifact(
            timeline, scene_num, _SCENE_ASSEMBLY_DIR,
        )
    except Exception as e:  # noqa: BLE001 — never block the main write
        logger.warning("Scene %d assembly artifact write failed: %s", scene_num, e)
        artifact_path = ""

    err = validate_scene_assembly(timeline, scene_num)
    if err is None:
        logger.info(
            "Scene %d per-moment assembly check PASS (artifact=%s)",
            scene_num, artifact_path or "<no-artifact>",
        )
        return

    logger.error("SCENE %d ASSEMBLY VIOLATION: %s", scene_num, err)
    state["otio_violation"] = f"scene_assembly_{scene_num}: {err}"
    try:
        from recovery import escalate_pipeline_error
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"scene {scene_num} assembly violation: {err}") from None

    response = escalate_pipeline_error(
        operation_name=f"scene_assembly_{scene_num}",
        error_msg=err,
        severity="critical",
        default_action="skip",
        diagnosis_hint=(
            f"Per-scene OTIO assembly check failed for scene {scene_num}. "
            f"This runs immediately after all of the scene's video clips "
            f"are persisted — the scene's assembly cannot be trusted."
        ),
        agent_policy_type="otio",
    )
    action = response.get("action", "abort")
    if action in ("skip", "retry_with_fix"):
        logger.warning(
            "SCENE %d ASSEMBLY: recovery decided '%s' — continuing pipeline",
            scene_num, action,
        )
        return
    if action == "abort":
        raise RuntimeError(f"scene {scene_num} assembly violation (abort): {err}")


def get_timeline_status(tool_context=None) -> str:
    """Get a summary of all tracks, clips, and gaps in the timeline.

    Returns:
        JSON string with timeline structure summary.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found."})

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    tracks_info = []
    for track in timeline.tracks:
        clips = []
        gaps = []
        for item in track:
            if isinstance(item, otio.schema.Clip):
                clips.append(
                    {
                        "name": item.name,
                        "duration": (
                            item.source_range.duration.to_seconds()
                            if item.source_range
                            else 0
                        ),
                        "metadata": dict(item.metadata.get("documentary", {})),
                    }
                )
            elif isinstance(item, otio.schema.Gap):
                gaps.append(
                    {
                        "name": item.name,
                        "metadata": dict(item.metadata.get("documentary", {})),
                    }
                )

        tracks_info.append(
            {
                "name": track.name,
                "kind": str(track.kind),
                "clips": clips,
                "gaps": gaps,
                "total_clips": len(clips),
                "total_gaps": len(gaps),
            }
        )

    return json.dumps(
        {"timeline_name": timeline.name, "tracks": tracks_info},
        indent=2,
    )


def validate_timeline(phase: str, tool_context=None) -> str:
    """Run phase-specific validation checks on the timeline.

    Args:
        phase: Pipeline phase name (scenario, audio, visual_direction,
               production, assembly).

    Returns:
        JSON string with validation results.
    """
    from callbacks.timeline_guardian import _VALIDATORS, _load_timeline

    state = tool_context.state if tool_context else {}
    # _load_timeline now acquires _otio_lock internally, so no outer lock needed.
    timeline = _load_timeline(state)

    if not timeline:
        return json.dumps({"valid": False, "error": "Timeline not found"})

    validator = _VALIDATORS.get(phase)
    if not validator:
        return json.dumps(
            {"valid": False, "error": f"Unknown phase: {phase}"}
        )

    error = validator(timeline, state)
    if error:
        return json.dumps({"valid": False, "phase": phase, "errors": error})

    return json.dumps({"valid": True, "phase": phase, "message": "All checks passed"})


# -- ADK FunctionTool wrappers -------------------------------------------------
create_timeline_tool = FunctionTool(create_timeline)
add_narration_clip_tool = FunctionTool(add_narration_clip)
add_video_clip_tool = FunctionTool(add_video_clip)
get_timeline_status_tool = FunctionTool(get_timeline_status)
validate_timeline_tool = FunctionTool(validate_timeline)

otio_tools = [
    create_timeline_tool,
    add_narration_clip_tool,
    add_video_clip_tool,
    get_timeline_status_tool,
    validate_timeline_tool,
]
