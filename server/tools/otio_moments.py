"""
Per-moment OTIO validation helpers + WhisperX duration oracle.

The pipeline used to check OTIO compliance only at stage boundaries
(post-audio, pre-assembly). Scene 5 phrase 2 narration was 13.84s but
video was capped at 10.00s by LTX — discovered AFTER all 21 clips were
generated, wasting 2.5h of GPU time and requiring 5 manual extension clips.

This module closes that gap with fine-grained validators that run
immediately after each artifact is persisted:

* ``validate_audio_duration_vs_scene_target``  — every narration clip
* ``validate_video_duration_vs_audio``          — every video clip
* ``validate_scene_assembly``                   — each assembled scene
  (standalone OTIO artifact + compliance check)

The ``WhisperXOracle`` uses WhisperX as ground truth for TTS durations
(Qwen3-TTS reports inaccurate ``duration`` values). It tracks measured
durations and produces a running projection of the final movie runtime.
If the projection falls below 80% of target, it fires a reflection event
and escalates to the production supervisor (W3).

Closes #70, #82, #84, #85, #86.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------

# Per-clip audio tolerance: spoken narration may run over/under the
# per-voice budget a little bit without breaking the pipeline. The scene
# total check (in callbacks.timeline_guardian) is the hard boundary.
#
# Previously 1.5s: far too tight when the per-voice budget is derived as
# ``scene.duration_sec / num_voices`` (equal split), while voices have
# very uneven word counts.  A voice with 11 words naturally lands at
# ~5s regardless of its 11.65s equal-share budget, so the check fired
# on every well-formed clip and drove the recovery ladder into
# pointless regeneration loops.  4.0s is roughly 35% of a typical
# 11-12s per-voice budget — wide enough to absorb TTS jitter and
# uneven word counts, narrow enough that a genuinely off clip still
# trips the gate.  The scene-total (hard) boundary in
# ``callbacks.timeline_guardian`` remains the real invariant.
AUDIO_CLIP_TOLERANCE_SEC = 4.0

# LTX-Video 2.3 hard cap per clip (seconds). When narration exceeds this
# the pipeline MUST generate an extension clip — we cannot rescale in place.
LTX_CAP_SEC = 10.0

# Video must cover at least (audio - VIDEO_UNDERFLOW_TOLERANCE) or we fire
# extension-clip escalation. 0.25s matches ffmpeg frame-boundary slop.
VIDEO_UNDERFLOW_TOLERANCE_SEC = 0.25

# Running projection alarm threshold. If projected_total < ratio * target_total
# we fire a reflection event.
PROJECTION_ALARM_RATIO = 0.8


# ---------------------------------------------------------------------------
# Per-moment validation helpers
# ---------------------------------------------------------------------------

def validate_audio_duration_vs_scene_target(
    scene_num: int,
    voice: str,
    actual_duration_sec: float,
    scene: dict,
    tolerance_sec: float = AUDIO_CLIP_TOLERANCE_SEC,
) -> Optional[str]:
    """Check a freshly-persisted narration clip against its scene target.

    Called **immediately** after ``add_narration_clip`` has written the
    clip to OTIO — not batched at end of stage.

    The clip is valid if ``actual_duration`` is within ``tolerance`` of the
    per-voice pro-rata budget derived from the scene's ``duration_sec``.
    The per-voice budget is ``duration_sec / num_active_voices`` which
    mirrors how ``deterministic_audio_callback`` allocates voice budgets.

    Args:
        scene_num: 1-based scene number.
        voice: Voice role (e.g. "V1", "V2_RU", "V3_EN").
        actual_duration_sec: Measured duration in seconds (authoritative —
            should come from WhisperX, not from the TTS engine's self-report).
        scene: Scene dict from ``state["scenes"]``; must contain
            ``duration_sec`` and ``voices`` list.
        tolerance_sec: Allowed drift either side of the per-voice budget.

    Returns:
        ``None`` if the clip is within tolerance, otherwise a human-readable
        error string naming the clip + expected vs actual durations.
    """
    if actual_duration_sec <= 0:
        return (
            f"scene {scene_num} {voice}: actual_duration "
            f"{actual_duration_sec:.3f}s must be > 0"
        )

    scene_target = float(scene.get("duration_sec", 0) or 0)
    if scene_target <= 0:
        return (
            f"scene {scene_num} has no duration_sec target "
            f"(got {scene.get('duration_sec')!r}) — cannot validate"
        )

    voices = [
        v for v in (scene.get("voices") or [])
        if (v.get("text") or "").strip()
    ]
    num_voices = max(1, len(voices))
    per_voice_budget = scene_target / num_voices

    drift = actual_duration_sec - per_voice_budget
    # Only flag OVERSHOOTS: if the audio clip is longer than the
    # per-voice budget by more than ``tolerance_sec`` we have a real
    # invariant problem (can't fit in its video slot, may breach LTX
    # cap, etc.).  Undershoots are normal — voices naturally have
    # uneven word counts so the equal-split budget is an upper bound,
    # not a target.  The scene-total (hard) boundary in
    # ``callbacks.timeline_guardian`` handles aggregate under-run.
    if drift <= tolerance_sec:
        return None

    return (
        f"scene {scene_num} {voice}: audio duration {actual_duration_sec:.2f}s "
        f"vs per-voice budget {per_voice_budget:.2f}s "
        f"(scene target {scene_target:.2f}s / {num_voices} voices) "
        f"— drift {drift:+.2f}s exceeds tolerance {tolerance_sec:.2f}s"
    )


def validate_video_duration_vs_audio(
    scene_num: int,
    phrase_idx: int,
    video_duration_sec: float,
    audio_duration_sec: float,
    tolerance_sec: float = VIDEO_UNDERFLOW_TOLERANCE_SEC,
) -> Optional[str]:
    """Check a freshly-persisted video clip covers its narration.

    Video is a failure when it is **shorter** than the narration it is
    supposed to cover — the assembler cannot paper over missing frames.
    Video longer than audio is fine (the extra gets trimmed on assembly).

    Args:
        scene_num: 1-based scene number.
        phrase_idx: Visual phrase index within the scene.
        video_duration_sec: Measured video duration in seconds.
        audio_duration_sec: Measured narration duration for the same slot.
        tolerance_sec: Allowed underflow (ffmpeg frame-boundary slop).

    Returns:
        ``None`` if the clip covers the narration, otherwise an error
        string describing the shortfall (which the caller should use to
        drive extension-clip escalation — see ``fire_extension_escalation``).
    """
    if video_duration_sec <= 0:
        return (
            f"scene {scene_num} phrase {phrase_idx}: "
            f"video_duration {video_duration_sec:.3f}s must be > 0"
        )
    if audio_duration_sec <= 0:
        return (
            f"scene {scene_num} phrase {phrase_idx}: "
            f"audio_duration {audio_duration_sec:.3f}s must be > 0 "
            f"— cannot validate video coverage without narration"
        )

    shortfall = audio_duration_sec - video_duration_sec
    if shortfall <= tolerance_sec:
        return None

    return (
        f"scene {scene_num} phrase {phrase_idx}: video {video_duration_sec:.2f}s "
        f"< audio {audio_duration_sec:.2f}s — shortfall {shortfall:.2f}s "
        f"(LTX cap {LTX_CAP_SEC:.1f}s)"
    )


def validate_scene_assembly(
    timeline: Any,
    scene_num: int,
    tolerance_sec: float = 1.0,
) -> Optional[str]:
    """Per-scene standalone OTIO compliance check.

    After all audio AND video clips for a single scene have been
    persisted, assemble a **scene-level** OTIO artifact (a shallow
    sub-timeline containing only clips/gaps tagged with this scene_num)
    and validate its compliance BEFORE proceeding to the next scene.

    This catches per-scene drift the moment it happens, instead of at the
    final Timeline Guardian pass.

    Checks (all must hold):

    1. At least one narration clip exists for the scene.
    2. At least one video clip exists for the scene.
    3. ``sum(video_clips.source_range)`` >= ``sum(narration_clips.duration) -
       tolerance`` for the scene's primary language.
    4. No unfilled ``status=empty`` placeholder gaps remain on V1.

    Args:
        timeline: Live OTIO Timeline object.
        scene_num: 1-based scene number to validate.
        tolerance_sec: Allowed underflow between video total and audio total.

    Returns:
        ``None`` on pass, otherwise an error string.
    """
    import opentimelineio as otio

    video_track = None
    narration_track = None
    for track in timeline.tracks:
        if track.name == "V1_Video":
            video_track = track
        elif track.name == "A1_Narration":
            narration_track = track

    if video_track is None:
        return f"scene {scene_num} assembly: V1_Video track missing"
    if narration_track is None:
        return f"scene {scene_num} assembly: A1_Narration track missing"

    narration_total = 0.0
    narration_count = 0
    for item in narration_track:
        if not isinstance(item, otio.schema.Clip):
            continue
        meta = item.metadata.get("documentary", {})
        if meta.get("scene_num") != scene_num:
            continue
        voice = meta.get("voice", "")
        # Primary language only — alternate language (_EN suffix in dual
        # mode) lives on the same track but video is generated once.
        if voice.endswith("_EN"):
            continue
        if item.source_range:
            narration_total += item.source_range.duration.to_seconds()
            narration_count += 1

    if narration_count == 0:
        return (
            f"scene {scene_num} assembly: no narration clips on A1_Narration "
            f"— audio stage incomplete for this scene"
        )

    video_total = 0.0
    video_count = 0
    empty_gaps = 0
    for item in video_track:
        meta = item.metadata.get("documentary", {})
        if meta.get("scene_num") != scene_num:
            continue
        if isinstance(item, otio.schema.Clip):
            if item.source_range:
                video_total += item.source_range.duration.to_seconds()
                video_count += 1
        elif isinstance(item, otio.schema.Gap):
            gap_type = meta.get("gap_type", "")
            if gap_type in ("inter_voice", "inter_scene"):
                continue  # structural gaps are expected
            if meta.get("status") == "empty":
                empty_gaps += 1

    if empty_gaps > 0:
        return (
            f"scene {scene_num} assembly: {empty_gaps} placeholder gap(s) "
            f"on V1_Video not replaced by clips"
        )

    if video_count == 0:
        return (
            f"scene {scene_num} assembly: no video clips on V1_Video "
            f"— production stage incomplete for this scene"
        )

    shortfall = narration_total - video_total
    if shortfall > tolerance_sec:
        return (
            f"scene {scene_num} assembly: video total {video_total:.2f}s "
            f"< narration total {narration_total:.2f}s "
            f"(shortfall {shortfall:.2f}s > tolerance {tolerance_sec:.2f}s)"
        )

    logger.info(
        "Scene %d assembly OK: narration=%.2fs (%d clips), video=%.2fs (%d clips)",
        scene_num, narration_total, narration_count, video_total, video_count,
    )
    return None


def persist_scene_assembly_artifact(
    timeline: Any,
    scene_num: int,
    out_dir: str,
) -> str:
    """Write a standalone OTIO artifact for a single scene (paper trail for #70).

    Extracts only the clips/gaps belonging to ``scene_num`` from the full
    timeline and writes them to ``<out_dir>/scene_NNN_assembly.otio``.
    Used by the per-moment assembly validator so each scene's compliance
    state is inspectable after the fact.

    Returns the path to the persisted OTIO file.
    """
    import opentimelineio as otio

    os.makedirs(out_dir, exist_ok=True)

    sub = otio.schema.Timeline(name=f"scene_{scene_num:03d}_assembly")

    for src_track in timeline.tracks:
        new_track = otio.schema.Track(name=src_track.name, kind=src_track.kind)
        for item in src_track:
            meta = item.metadata.get("documentary", {})
            if meta.get("scene_num") != scene_num:
                continue
            new_track.append(item.deepcopy())
        sub.tracks.append(new_track)

    path = os.path.join(out_dir, f"scene_{scene_num:03d}_assembly.otio")
    otio.adapters.write_to_file(sub, path)
    logger.info("Persisted scene %d assembly artifact: %s", scene_num, path)
    return path


# ---------------------------------------------------------------------------
# WhisperX duration oracle (#86)
# ---------------------------------------------------------------------------

@dataclass
class MeasuredClip:
    """A single narration clip measured via WhisperX."""

    scene_num: int
    voice: str
    claimed_sec: float      # scene.duration_sec share the TTS aimed for
    measured_sec: float     # WhisperX ground truth
    wav_path: str = ""

    @property
    def ratio(self) -> float:
        if self.claimed_sec <= 0:
            return 0.0
        return self.measured_sec / self.claimed_sec


@dataclass
class WhisperXOracle:
    """Running ground-truth duration tracker.

    After each TTS clip lands, call :meth:`record` with the WhisperX
    measurement. Use :meth:`project_total` to compute the projected final
    runtime given the remaining un-synthesised scene budgets, and
    :meth:`check_projection` to fire a reflection event when the
    projection falls below :data:`PROJECTION_ALARM_RATIO` of the target.

    The PAG reference run (see :mod:`tests/test_otio_moments`) produced
    a 72% ratio — measured 194s vs claimed 270s. This oracle would have
    caught it at scene 3 and escalated before burning GPU time on the
    remaining clips.
    """

    target_total_sec: float
    clips: list[MeasuredClip] = field(default_factory=list)
    scene_claimed: dict[int, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def register_scenes(self, scenes: list[dict]) -> None:
        """Snapshot each scene's claimed ``duration_sec`` for projection math."""
        with self._lock:
            for s in scenes:
                sn = int(s.get("scene_num", 0) or 0)
                if sn > 0:
                    self.scene_claimed[sn] = float(s.get("duration_sec", 0) or 0)

    def record(
        self,
        scene_num: int,
        voice: str,
        claimed_sec: float,
        measured_sec: float,
        wav_path: str = "",
    ) -> MeasuredClip:
        """Record a WhisperX measurement for a clip."""
        clip = MeasuredClip(
            scene_num=scene_num,
            voice=voice,
            claimed_sec=claimed_sec,
            measured_sec=measured_sec,
            wav_path=wav_path,
        )
        with self._lock:
            self.clips.append(clip)
        return clip

    def measured_total(self) -> float:
        """Sum of all measured durations so far."""
        with self._lock:
            return sum(c.measured_sec for c in self.clips)

    def measured_by_scene(self, scene_num: int) -> float:
        """Sum of measured durations for clips of a single scene."""
        with self._lock:
            return sum(c.measured_sec for c in self.clips if c.scene_num == scene_num)

    def completed_scene_nums(self) -> set[int]:
        """Set of scene_nums that have at least one measured clip."""
        with self._lock:
            return {c.scene_num for c in self.clips}

    def project_total(self) -> float:
        """Projected final runtime = measured_so_far + sum(remaining_targets).

        "Remaining" = scenes that have no measured clips yet. For scenes
        that are partially measured we trust the measurements as-is
        (don't extrapolate), matching how the pipeline actually behaves.
        """
        with self._lock:
            done_scenes = {c.scene_num for c in self.clips}
            measured = sum(c.measured_sec for c in self.clips)
            remaining = sum(
                target
                for sn, target in self.scene_claimed.items()
                if sn not in done_scenes
            )
            return measured + remaining

    def check_projection(
        self,
        alarm_ratio: float = PROJECTION_ALARM_RATIO,
    ) -> Optional[str]:
        """Return an alarm string if projection is below ``alarm_ratio`` of target.

        Returns ``None`` when projection is healthy.
        """
        if self.target_total_sec <= 0:
            return None
        projected = self.project_total()
        if projected >= alarm_ratio * self.target_total_sec:
            return None
        return (
            f"projected total {projected:.0f}s vs target "
            f"{self.target_total_sec:.0f}s = "
            f"{100 * projected / self.target_total_sec:.0f}%; "
            f"need more scenes or longer ones"
        )


# ---------------------------------------------------------------------------
# WhisperX integration — ground-truth duration measurement
# ---------------------------------------------------------------------------

def measure_actual_duration_with_whisperx(
    wav_path: str,
    text: str,
    language: str = "en",
) -> float:
    """Measure spoken duration via WhisperX (authoritative, not TTS-reported).

    Per SKILL.md rule 3, WhisperX is the duration oracle — the TTS
    engine's self-reported duration is not trusted because Qwen3-TTS has
    been observed to over-report by up to 30%.

    FAIL LOUD: if WhisperX is unavailable or the alignment errors, this
    raises :class:`RuntimeError`. There is no silent fallback to estimated
    duration — that was the bug #82 tracked.

    Returns:
        Measured spoken duration in seconds (end timestamp of the last word).

    Raises:
        RuntimeError: WhisperX unreachable / aligned 0 words / WAV missing.
    """
    # Import lazily so tests can stub ``tools.whisperx_tools.align_narration``.
    from tools.whisperx_tools import align_narration

    raw = align_narration(wav_path=wav_path, text=text, language=language)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"WhisperX returned non-JSON for {wav_path}: {e}"
        ) from e

    if not isinstance(data, dict):
        raise RuntimeError(f"WhisperX returned non-dict result: {type(data)!r}")

    if data.get("status") != "aligned":
        raise RuntimeError(
            f"WhisperX alignment failed for {wav_path}: "
            f"status={data.get('status')!r} error={data.get('error')!r}"
        )

    # WhisperX reports per-word end timestamps. Duration is the last word's
    # end time. ``total_duration`` is provided by align_narration but we
    # compute from words to be defensive.
    words = data.get("words") or []
    if not words:
        raise RuntimeError(
            f"WhisperX aligned 0 words for {wav_path} — "
            f"cannot trust duration (status={data.get('status')!r})"
        )

    total = float(data.get("total_duration") or 0.0)
    if total <= 0:
        total = max(float(w.get("end", 0) or 0) for w in words)

    if total <= 0:
        raise RuntimeError(
            f"WhisperX measured duration 0s for {wav_path} — alignment is broken"
        )

    return total


# ---------------------------------------------------------------------------
# Escalation helpers (#85, #86)
# ---------------------------------------------------------------------------

def fire_reflection_event(state: dict, context: str) -> None:
    """Record a reflection event in session state + escalate to supervisor.

    The event is a structured log entry the Reasoning Digest can surface
    in the dashboard. It is ALSO sent to ``supervisor_escalate`` from the
    production_supervisor agent (W3) so the supervisor's LLM can pick a
    remediation action from its menu (add scenes, lengthen existing
    scenes, accept the runtime as-is).

    If W3 has not landed yet the ``supervisor_escalate`` import fails —
    we log loudly and keep going so the rest of the pipeline continues
    (per the SKILL.md rule 8, problems must be reported immediately;
    they must not silently halt unrelated work).
    """
    import time

    events = state.get("_reflection_events")
    if not isinstance(events, list):
        events = []
    events.append({"at": time.time(), "context": context, "kind": "projection"})
    state["_reflection_events"] = events
    logger.warning("WhisperX oracle reflection: %s", context)

    _invoke_supervisor_escalate(
        kind="projection_shortfall",
        context=context,
        state=state,
        actions=["add_scenes", "lengthen_scenes", "accept_runtime"],
    )


def fire_extension_escalation(
    state: dict,
    scene_num: int,
    phrase_idx: int,
    video_duration_sec: float,
    audio_duration_sec: float,
) -> None:
    """Escalate a video<audio shortfall to the production supervisor.

    The supervisor's LLM picks from the action menu (which includes
    ``generate_extension_clip``); the executor generates the extension
    clip via existing ``video_tools.generate_video_clip`` and the
    ``validate_video_duration_vs_audio`` check re-runs after the new
    clip lands. Loop until pass OR supervisor picks ``abort``.
    """
    context = (
        f"scene {scene_num} phrase {phrase_idx}: video "
        f"{video_duration_sec:.2f}s < audio {audio_duration_sec:.2f}s "
        f"(shortfall {audio_duration_sec - video_duration_sec:.2f}s, "
        f"LTX cap {LTX_CAP_SEC:.1f}s)"
    )
    _invoke_supervisor_escalate(
        kind="video_shortfall",
        context=context,
        state=state,
        actions=["generate_extension_clip", "accept_shortfall", "abort"],
        extras={
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "video_duration_sec": video_duration_sec,
            "audio_duration_sec": audio_duration_sec,
            "shortfall_sec": audio_duration_sec - video_duration_sec,
        },
    )


def _invoke_supervisor_escalate(
    kind: str,
    context: str,
    state: dict,
    actions: list[str],
    extras: Optional[dict] = None,
) -> None:
    """Best-effort call into ``production_supervisor.supervisor_escalate``.

    Per W3: the supervisor exposes ``supervisor_escalate(kind, context,
    state, actions, extras)``. When W3 hasn't landed yet (ImportError)
    or the function raises, we log loudly and continue — the reflection
    event is already persisted in ``state["_reflection_events"]`` and the
    next pipeline pass can pick it up.
    """
    try:
        from agents.production_supervisor import supervisor_escalate  # type: ignore
    except ImportError:
        logger.error(
            "supervisor_escalate unavailable (W3 not merged yet) — "
            "logged %s escalation to state only: %s",
            kind, context,
        )
        return

    try:
        supervisor_escalate(
            kind=kind,
            context=context,
            state=state,
            actions=actions,
            extras=extras or {},
        )
    except Exception as e:  # noqa: BLE001 — must never crash the caller
        logger.error(
            "supervisor_escalate(%s) raised %s — continuing; context: %s",
            kind, e, context,
        )
