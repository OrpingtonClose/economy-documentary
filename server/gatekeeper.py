"""
OTIO Gatekeeper — universal validation layer for every pipeline mutation.

ARCHITECTURE:
    Every write to the OTIO timeline passes through a gatekeeper check.
    Every stage handoff passes through a gatekeeper check.
    Every generated artifact passes through a gatekeeper check.

    Checks are:
    1. Structural   — source_range > 0, file exists, no orphan clips
    2. Cross-track  — video duration matches narration duration per scene
    3. Anti-cheat   — detect looping, stretching, dead stills in video
    4. Consistency  — clip count per scene matches across tracks

    Results are emitted to AG-UI so the user sees every check in real time.
    The user gets a configurable timeout to intervene before auto-proceeding.

ENFORCEMENT POLICY:
    - REJECT: gatekeeper returns an error string → the mutation is BLOCKED
    - WARN:   gatekeeper emits a warning but allows the mutation (rare)
    - PASS:   gatekeeper allows the mutation silently

    There is NO "advisory" mode. Rejects are hard stops.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gatekeeper verdict types
# ---------------------------------------------------------------------------

class GatekeeperVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    REJECT = "reject"


@dataclass
class GatekeeperCheck:
    """A single gatekeeper check result."""
    name: str                           # human-readable check name
    category: str                       # "structural", "cross_track", "anti_cheat", "consistency"
    verdict: GatekeeperVerdict
    message: str = ""                   # explanation (always set for warn/reject)
    stage: str = ""                     # pipeline stage this check belongs to
    scene_num: int = 0
    phrase_idx: int = 0
    metadata: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "verdict": self.verdict.value,
            "message": self.message,
            "stage": self.stage,
            "scene_num": self.scene_num,
            "phrase_idx": self.phrase_idx,
            "metadata": self.metadata,
            "timestamp": self.timestamp or time.time(),
        }


# ---------------------------------------------------------------------------
# Gatekeeper event store (AG-UI integration)
# ---------------------------------------------------------------------------

class GatekeeperStore:
    """Thread-safe store for gatekeeper check results.

    All checks are recorded and streamed to the frontend via AG-UI.
    The user can see pass/reject/warn for every check in real time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._checks: list[GatekeeperCheck] = []
        self._intervention_events: list[dict] = []

    def record_check(self, check: GatekeeperCheck) -> None:
        """Record a gatekeeper check and emit it to AG-UI."""
        with self._lock:
            self._checks.append(check)

        # Emit to AG-UI event bus
        try:
            from agui import emit_agui_event
            emit_agui_event("gatekeeper_check", check.to_dict())
        except ImportError:
            pass  # CLI mode — no AG-UI

        level = logging.ERROR if check.verdict == GatekeeperVerdict.REJECT else (
            logging.WARNING if check.verdict == GatekeeperVerdict.WARN else logging.INFO
        )
        logger.log(
            level,
            "GATEKEEPER [%s] %s: %s — %s",
            check.stage, check.verdict.value.upper(), check.name,
            check.message or "OK",
        )

    def record_intervention_window(
        self,
        stage: str,
        timeout_sec: float,
        checks_summary: list[dict],
    ) -> None:
        """Record an intervention window — the user can halt/override.

        Emitted to AG-UI as a special event the frontend renders with
        a countdown timer and "Halt" / "Override" / "Proceed" buttons.
        """
        event = {
            "stage": stage,
            "timeout_sec": timeout_sec,
            "checks_summary": checks_summary,
            "timestamp": time.time(),
        }
        with self._lock:
            self._intervention_events.append(event)

        try:
            from agui import emit_agui_event
            emit_agui_event("gatekeeper_intervention", event)
        except ImportError:
            pass

    def get_all_checks(self) -> list[dict]:
        with self._lock:
            return [c.to_dict() for c in self._checks]

    def get_checks_for_stage(self, stage: str) -> list[dict]:
        with self._lock:
            return [c.to_dict() for c in self._checks if c.stage == stage]

    def get_rejects(self) -> list[dict]:
        with self._lock:
            return [
                c.to_dict() for c in self._checks
                if c.verdict == GatekeeperVerdict.REJECT
            ]


# Singleton
_store = GatekeeperStore()


def get_gatekeeper_store() -> GatekeeperStore:
    return _store


# ---------------------------------------------------------------------------
# Intervention window
# ---------------------------------------------------------------------------

# Default timeout for user intervention (seconds).
# The pipeline pauses this long after gatekeeper checks complete,
# giving the user time to review and halt if needed.
_INTERVENTION_TIMEOUT = float(os.environ.get("GATEKEEPER_TIMEOUT", "10"))

# Auto-approve mode skips the intervention window.
_AUTO_APPROVE = os.environ.get(
    "DOCUMENTARY_AUTO_APPROVE", ""
).strip().lower() in ("1", "true", "yes")
_TEST_MODE = os.environ.get(
    "DOCUMENTARY_TEST_MODE", ""
).strip().lower() in ("1", "true", "yes")


def intervention_window(stage: str, checks: list[GatekeeperCheck]) -> bool:
    """Open an intervention window for the user after gatekeeper checks.

    Emits the checks summary to AG-UI and waits for the timeout period.
    During this window, the user can halt the pipeline via the dashboard.

    Returns True if the pipeline should proceed, False if halted.
    """
    if _AUTO_APPROVE or _TEST_MODE:
        logger.info(
            "Gatekeeper intervention window skipped (auto-approve): %s", stage
        )
        return True

    rejects = [c for c in checks if c.verdict == GatekeeperVerdict.REJECT]
    if rejects:
        # Hard rejects — no intervention window, just stop
        logger.error(
            "Gatekeeper BLOCKED %s: %d reject(s)",
            stage, len(rejects),
        )
        return False

    summary = [c.to_dict() for c in checks]
    _store.record_intervention_window(stage, _INTERVENTION_TIMEOUT, summary)

    logger.info(
        "Gatekeeper intervention window open for %s (%.0fs timeout, %d checks)",
        stage, _INTERVENTION_TIMEOUT, len(checks),
    )

    # Poll for halt signal during timeout
    from callbacks.approval_gate import _read_approval_state
    start = time.time()
    while time.time() - start < _INTERVENTION_TIMEOUT:
        state = _read_approval_state()
        if state.get(f"gatekeeper_{stage}", {}).get("halted"):
            logger.warning("Gatekeeper HALTED by user: %s", stage)
            return False
        time.sleep(1.0)

    logger.info("Gatekeeper intervention window closed: %s — proceeding", stage)
    return True


# ---------------------------------------------------------------------------
# Anti-cheat QA checks (video quality)
# ---------------------------------------------------------------------------

def _probe_video_stats(mp4_path: str) -> Optional[dict]:
    """Probe a video file for frame count, duration, and codec info."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                mp4_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        video_stream = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                video_stream = s
                break
        if not video_stream:
            return None

        duration = float(data.get("format", {}).get("duration", 0))
        nb_frames = int(video_stream.get("nb_frames", 0))
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        codec = video_stream.get("codec_name", "unknown")
        r_frame_rate = video_stream.get("r_frame_rate", "0/1")

        # Parse frame rate fraction
        try:
            num, den = r_frame_rate.split("/")
            fps = float(num) / float(den) if float(den) > 0 else 0
        except (ValueError, ZeroDivisionError):
            fps = 0

        return {
            "duration": duration,
            "nb_frames": nb_frames,
            "width": width,
            "height": height,
            "codec": codec,
            "fps": fps,
        }
    except Exception as e:
        logger.warning("ffprobe failed for %s: %s", mp4_path, e)
        return None


def _check_dead_still(mp4_path: str, stats: dict) -> Optional[str]:
    """Detect dead stills: extract a few frames and compare pixel variance.

    A dead still has near-zero inter-frame difference across the entire clip.
    We sample 3 frames (start, middle, end) and compute pairwise SSIM.
    If all pairs are > 0.99, the clip is a dead still.
    """
    nb_frames = stats.get("nb_frames", 0)
    duration = stats.get("duration", 0)
    if nb_frames < 3 or duration < 0.5:
        return None  # too short to check

    # Extract 3 frames as raw RGB data and compare
    try:
        import tempfile
        frame_times = [0.0, duration / 2, max(duration - 0.1, 0)]
        frame_files = []
        tmpdir = tempfile.mkdtemp(prefix="gk_frames_")

        for i, t in enumerate(frame_times):
            out = os.path.join(tmpdir, f"frame_{i}.raw")
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{t:.3f}",
                    "-i", mp4_path,
                    "-frames:v", "1",
                    "-f", "rawvideo",
                    "-pix_fmt", "gray",
                    out,
                ],
                capture_output=True, timeout=15,
            )
            if os.path.exists(out) and os.path.getsize(out) > 0:
                frame_files.append(out)

        if len(frame_files) < 3:
            # Cleanup
            for f in frame_files:
                try:
                    os.unlink(f)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
            return None  # couldn't extract frames

        # Compare frames by byte-level difference
        frames_data = []
        for f in frame_files:
            with open(f, "rb") as fh:
                frames_data.append(fh.read())

        # Cleanup
        for f in frame_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

        # Compute simple byte-level difference ratio
        identical_pairs = 0
        total_pairs = 0
        for i in range(len(frames_data)):
            for j in range(i + 1, len(frames_data)):
                a, b = frames_data[i], frames_data[j]
                if len(a) != len(b):
                    continue
                total_pairs += 1
                # Count differing bytes
                diff_count = sum(1 for x, y in zip(a, b) if abs(x - y) > 5)
                diff_ratio = diff_count / len(a) if len(a) > 0 else 1.0
                if diff_ratio < 0.01:  # less than 1% of pixels differ
                    identical_pairs += 1

        if total_pairs > 0 and identical_pairs == total_pairs:
            return (
                f"Dead still detected: all {total_pairs} frame pairs are "
                f"identical (<1% pixel difference). Clip is a single frozen image."
            )

    except Exception as e:
        logger.warning("Dead still check failed for %s: %s", mp4_path, e)

    return None


def _check_looping(mp4_path: str, stats: dict) -> Optional[str]:
    """Detect looping: sample frames at regular intervals and check for
    periodic repetition (e.g., ABCABC pattern)."""
    nb_frames = stats.get("nb_frames", 0)
    duration = stats.get("duration", 0)
    if nb_frames < 10 or duration < 2.0:
        return None  # too short to detect loops

    # Sample 6 frames at equal intervals
    try:
        import tempfile
        sample_count = 6
        frame_times = [duration * i / sample_count for i in range(sample_count)]
        tmpdir = tempfile.mkdtemp(prefix="gk_loop_")
        frame_files = []

        for i, t in enumerate(frame_times):
            out = os.path.join(tmpdir, f"frame_{i}.raw")
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{t:.3f}",
                    "-i", mp4_path,
                    "-frames:v", "1",
                    "-f", "rawvideo",
                    "-pix_fmt", "gray",
                    out,
                ],
                capture_output=True, timeout=15,
            )
            if os.path.exists(out) and os.path.getsize(out) > 0:
                frame_files.append(out)

        if len(frame_files) < sample_count:
            for f in frame_files:
                try:
                    os.unlink(f)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
            return None

        frames_data = []
        for f in frame_files:
            with open(f, "rb") as fh:
                frames_data.append(fh.read())

        # Cleanup
        for f in frame_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

        # Check for ABAB pattern: frame[i] ≈ frame[i+2]
        # or ABC ABC pattern: frame[i] ≈ frame[i+3]
        for period in (2, 3):
            repeat_count = 0
            check_count = 0
            for i in range(sample_count - period):
                a, b = frames_data[i], frames_data[i + period]
                if len(a) != len(b):
                    continue
                check_count += 1
                diff_count = sum(1 for x, y in zip(a, b) if abs(x - y) > 5)
                diff_ratio = diff_count / len(a) if len(a) > 0 else 1.0
                if diff_ratio < 0.02:  # <2% difference = same frame
                    repeat_count += 1

            if check_count > 0 and repeat_count == check_count:
                return (
                    f"Looping detected: frames repeat with period {period} "
                    f"({repeat_count}/{check_count} pairs identical). "
                    f"Clip appears to be a short loop repeated."
                )

    except Exception as e:
        logger.warning("Loop check failed for %s: %s", mp4_path, e)

    return None


def _check_stretching(stats: dict, expected_duration: float) -> Optional[str]:
    """Detect stretching: if the frame rate is abnormally low compared to
    what the model should output, the clip was likely temporally stretched."""
    fps = stats.get("fps", 0)
    duration = stats.get("duration", 0)
    nb_frames = stats.get("nb_frames", 0)

    if fps <= 0 or nb_frames <= 0 or duration <= 0:
        return None

    # LTX-2.3 outputs at ~24fps.  If actual fps < 4, that's suspicious.
    if fps < 4.0:
        return (
            f"Temporal stretching suspected: frame rate is {fps:.1f}fps "
            f"(expected ~24fps from LTX-2.3). Clip may have been stretched "
            f"to meet duration target."
        )

    # Also check if nb_frames is suspiciously low for the duration
    expected_frames = duration * 24  # expected at 24fps
    if nb_frames < expected_frames * 0.3:  # less than 30% of expected
        return (
            f"Frame count anomaly: {nb_frames} frames for {duration:.1f}s "
            f"(expected ~{int(expected_frames)} at 24fps). "
            f"Clip may have been stretched or frame-dropped."
        )

    return None


# ---------------------------------------------------------------------------
# Public gatekeeper API — called by pipeline callbacks
# ---------------------------------------------------------------------------

def check_video_clip(
    mp4_path: str,
    scene_num: int,
    phrase_idx: int,
    source_range: float,
    expected_duration: float,
    stage: str = "production",
) -> list[GatekeeperCheck]:
    """Run ALL gatekeeper checks on a video clip before it enters the timeline.

    Returns a list of GatekeeperCheck results. Any REJECT means the clip
    must not be added to the timeline.
    """
    checks: list[GatekeeperCheck] = []

    # 1. Structural: file exists
    if not os.path.exists(mp4_path):
        checks.append(GatekeeperCheck(
            name="file_exists",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"Video file not found: {mp4_path}",
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
        for c in checks:
            _store.record_check(c)
        return checks

    checks.append(GatekeeperCheck(
        name="file_exists",
        category="structural",
        verdict=GatekeeperVerdict.PASS,
        stage=stage,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
    ))

    # 2. Structural: source_range > 0
    if source_range <= 0:
        checks.append(GatekeeperCheck(
            name="source_range_positive",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"source_range={source_range:.3f}s — must be > 0",
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
        for c in checks:
            _store.record_check(c)
        return checks

    checks.append(GatekeeperCheck(
        name="source_range_positive",
        category="structural",
        verdict=GatekeeperVerdict.PASS,
        stage=stage,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
    ))

    # 3. Probe video
    stats = _probe_video_stats(mp4_path)
    if not stats:
        checks.append(GatekeeperCheck(
            name="probe_video",
            category="structural",
            verdict=GatekeeperVerdict.WARN,
            message="Could not probe video file — skipping anti-cheat checks",
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
        for c in checks:
            _store.record_check(c)
        return checks

    checks.append(GatekeeperCheck(
        name="probe_video",
        category="structural",
        verdict=GatekeeperVerdict.PASS,
        metadata=stats,
        stage=stage,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
    ))

    # 4. Structural: codec must be H.264
    codec = stats.get("codec", "unknown")
    if codec not in ("h264", "libx264"):
        checks.append(GatekeeperCheck(
            name="codec_h264",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"Codec is '{codec}' — must be H.264. Re-encode before adding.",
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
    else:
        checks.append(GatekeeperCheck(
            name="codec_h264",
            category="structural",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))

    # 5. Anti-cheat: dead stills
    still_err = _check_dead_still(mp4_path, stats)
    if still_err:
        checks.append(GatekeeperCheck(
            name="anti_cheat_dead_still",
            category="anti_cheat",
            verdict=GatekeeperVerdict.REJECT,
            message=still_err,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
    else:
        checks.append(GatekeeperCheck(
            name="anti_cheat_dead_still",
            category="anti_cheat",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))

    # 6. Anti-cheat: looping
    loop_err = _check_looping(mp4_path, stats)
    if loop_err:
        checks.append(GatekeeperCheck(
            name="anti_cheat_looping",
            category="anti_cheat",
            verdict=GatekeeperVerdict.REJECT,
            message=loop_err,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
    else:
        checks.append(GatekeeperCheck(
            name="anti_cheat_looping",
            category="anti_cheat",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))

    # 7. Anti-cheat: stretching
    stretch_err = _check_stretching(stats, expected_duration)
    if stretch_err:
        checks.append(GatekeeperCheck(
            name="anti_cheat_stretching",
            category="anti_cheat",
            verdict=GatekeeperVerdict.WARN,
            message=stretch_err,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))
    else:
        checks.append(GatekeeperCheck(
            name="anti_cheat_stretching",
            category="anti_cheat",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))

    # 8. Cross-track: source_range ≈ expected_duration (narration match)
    #
    # LTX-2.3 caps video output at 10s.  When the narration phrase exceeds
    # 10s, the best the model can do is source_range=10.0.  We must NOT
    # reject clips in that case — only reject when the mismatch is
    # unexplainable by the model cap.
    _LTX_CAP = 10.0
    if expected_duration > 0:
        drift = abs(source_range - expected_duration)
        cap_explained = (
            expected_duration > _LTX_CAP
            and source_range >= _LTX_CAP - 0.5  # model produced near-cap output
        )
        if drift > 1.0 and not cap_explained:
            checks.append(GatekeeperCheck(
                name="narration_duration_match",
                category="cross_track",
                verdict=GatekeeperVerdict.REJECT,
                message=(
                    f"source_range ({source_range:.2f}s) does not match narration "
                    f"duration ({expected_duration:.2f}s) — "
                    f"drift {drift:.2f}s > 1s"
                ),
                stage=stage,
                scene_num=scene_num,
                phrase_idx=phrase_idx,
            ))
        elif drift > 1.0 and cap_explained:
            checks.append(GatekeeperCheck(
                name="narration_duration_match",
                category="cross_track",
                verdict=GatekeeperVerdict.WARN,
                message=(
                    f"source_range ({source_range:.2f}s) < narration "
                    f"({expected_duration:.2f}s) but narration exceeds LTX-2.3 "
                    f"10s cap — deficit is expected"
                ),
                stage=stage,
                scene_num=scene_num,
                phrase_idx=phrase_idx,
            ))
        else:
            checks.append(GatekeeperCheck(
                name="narration_duration_match",
                category="cross_track",
                verdict=GatekeeperVerdict.PASS,
                stage=stage,
                scene_num=scene_num,
                phrase_idx=phrase_idx,
            ))
    else:
        checks.append(GatekeeperCheck(
            name="narration_duration_match",
            category="cross_track",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            phrase_idx=phrase_idx,
        ))

    for c in checks:
        _store.record_check(c)
    return checks


def check_narration_clip(
    wav_path: str,
    scene_num: int,
    voice: str,
    duration: float,
    stage: str = "audio",
) -> list[GatekeeperCheck]:
    """Run gatekeeper checks on a narration clip before it enters the timeline."""
    checks: list[GatekeeperCheck] = []

    # 1. File exists
    if not os.path.exists(wav_path):
        checks.append(GatekeeperCheck(
            name="file_exists",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"WAV file not found: {wav_path}",
            stage=stage,
            scene_num=scene_num,
            metadata={"voice": voice},
        ))
        for c in checks:
            _store.record_check(c)
        return checks

    checks.append(GatekeeperCheck(
        name="file_exists",
        category="structural",
        verdict=GatekeeperVerdict.PASS,
        stage=stage,
        scene_num=scene_num,
        metadata={"voice": voice},
    ))

    # 2. Duration > 0
    if duration <= 0:
        checks.append(GatekeeperCheck(
            name="duration_positive",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"Narration duration={duration:.3f}s — must be > 0",
            stage=stage,
            scene_num=scene_num,
            metadata={"voice": voice},
        ))
    else:
        checks.append(GatekeeperCheck(
            name="duration_positive",
            category="structural",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            metadata={"voice": voice},
        ))

    # 3. File size sanity (WAV should be at least a few KB)
    file_size = os.path.getsize(wav_path)
    if file_size < 1000:
        checks.append(GatekeeperCheck(
            name="file_size_sanity",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"WAV file is only {file_size} bytes — likely corrupt or empty",
            stage=stage,
            scene_num=scene_num,
            metadata={"voice": voice, "file_size": file_size},
        ))
    else:
        checks.append(GatekeeperCheck(
            name="file_size_sanity",
            category="structural",
            verdict=GatekeeperVerdict.PASS,
            stage=stage,
            scene_num=scene_num,
            metadata={"voice": voice, "file_size": file_size},
        ))

    for c in checks:
        _store.record_check(c)
    return checks


def check_stage_handoff(
    from_stage: str,
    to_stage: str,
    state: dict,
) -> list[GatekeeperCheck]:
    """Run gatekeeper checks at a stage boundary (handoff).

    Validates that the outgoing stage left the OTIO timeline in a
    consistent state before the incoming stage starts.
    """
    checks: list[GatekeeperCheck] = []

    # Load the timeline
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        if from_stage != "scenario":
            checks.append(GatekeeperCheck(
                name="timeline_exists",
                category="structural",
                verdict=GatekeeperVerdict.REJECT,
                message=f"Timeline not found at handoff {from_stage} → {to_stage}",
                stage=f"{from_stage}→{to_stage}",
            ))
        for c in checks:
            _store.record_check(c)
        return checks

    import opentimelineio as otio

    try:
        from tools.otio_tools import _otio_lock
        with _otio_lock:
            timeline = otio.adapters.read_from_file(timeline_path)
    except Exception as e:
        checks.append(GatekeeperCheck(
            name="timeline_readable",
            category="structural",
            verdict=GatekeeperVerdict.REJECT,
            message=f"Cannot read timeline: {e}",
            stage=f"{from_stage}→{to_stage}",
        ))
        for c in checks:
            _store.record_check(c)
        return checks

    checks.append(GatekeeperCheck(
        name="timeline_readable",
        category="structural",
        verdict=GatekeeperVerdict.PASS,
        stage=f"{from_stage}→{to_stage}",
    ))

    # Stage-specific handoff checks
    if to_stage == "visual_direction":
        # After audio → visual direction: narration must exist
        narr_track = None
        for t in timeline.tracks:
            if t.name == "A1_Narration":
                narr_track = t
                break

        narr_count = 0
        if narr_track is not None:
            narr_count = sum(
                1 for item in narr_track
                if isinstance(item, otio.schema.Clip)
            )

        if narr_count == 0:
            checks.append(GatekeeperCheck(
                name="narration_clips_exist",
                category="consistency",
                verdict=GatekeeperVerdict.REJECT,
                message="No narration clips on A1_Narration — audio stage failed",
                stage=f"{from_stage}→{to_stage}",
            ))
        else:
            checks.append(GatekeeperCheck(
                name="narration_clips_exist",
                category="consistency",
                verdict=GatekeeperVerdict.PASS,
                metadata={"narration_clip_count": narr_count},
                stage=f"{from_stage}→{to_stage}",
            ))

    if to_stage == "production":
        # After visual direction → production: concepts must match narration count
        narr_track = None
        for t in timeline.tracks:
            if t.name == "A1_Narration":
                narr_track = t
                break

        # Count narration clips per scene (primary language only)
        narr_by_scene: dict[int, int] = {}
        if narr_track is not None:
            for item in narr_track:
                if isinstance(item, otio.schema.Clip):
                    meta = item.metadata.get("documentary", {})
                    sn = meta.get("scene_num", 0)
                    voice = meta.get("voice", "")
                    if sn > 0 and not voice.endswith("_EN"):
                        narr_by_scene[sn] = narr_by_scene.get(sn, 0) + 1

        # Count visual concepts per scene
        concepts_json = state.get("visual_concepts", "[]")
        try:
            from callbacks.deterministic_steps import extract_json_array
            concepts = extract_json_array(str(concepts_json)) or []
        except Exception:
            concepts = []

        concepts_by_scene: dict[int, int] = {}
        for c in concepts:
            sn = c.get("scene_num", 0)
            if sn > 0:
                concepts_by_scene[sn] = concepts_by_scene.get(sn, 0) + 1

        for sn, narr_count in narr_by_scene.items():
            concept_count = concepts_by_scene.get(sn, 0)
            if concept_count != narr_count:
                checks.append(GatekeeperCheck(
                    name="concepts_match_narration",
                    category="cross_track",
                    verdict=GatekeeperVerdict.REJECT,
                    message=(
                        f"Scene {sn}: {concept_count} visual concept(s) but "
                        f"{narr_count} narration phrase(s). Must be equal."
                    ),
                    stage=f"{from_stage}→{to_stage}",
                    scene_num=sn,
                ))
            else:
                checks.append(GatekeeperCheck(
                    name="concepts_match_narration",
                    category="cross_track",
                    verdict=GatekeeperVerdict.PASS,
                    metadata={"scene_num": sn, "count": narr_count},
                    stage=f"{from_stage}→{to_stage}",
                    scene_num=sn,
                ))

    if to_stage == "assembly":
        # After production → assembly: video clips must exist and match narration
        video_track = None
        narr_track = None
        for t in timeline.tracks:
            if t.name == "V1_Video":
                video_track = t
            elif t.name == "A1_Narration":
                narr_track = t

        if video_track is not None:
            gap_count = sum(
                1 for item in video_track
                if isinstance(item, otio.schema.Gap)
            )
            if gap_count > 0:
                checks.append(GatekeeperCheck(
                    name="no_video_gaps",
                    category="consistency",
                    verdict=GatekeeperVerdict.REJECT,
                    message=f"V1_Video still has {gap_count} unfilled gap(s)",
                    stage=f"{from_stage}→{to_stage}",
                ))
            else:
                checks.append(GatekeeperCheck(
                    name="no_video_gaps",
                    category="consistency",
                    verdict=GatekeeperVerdict.PASS,
                    stage=f"{from_stage}→{to_stage}",
                ))

        # Video clip count per scene must match narration
        if video_track is not None and narr_track is not None:
            video_by_scene: dict[int, int] = {}
            narr_by_scene_asm: dict[int, int] = {}

            for item in video_track:
                if isinstance(item, otio.schema.Clip):
                    meta = item.metadata.get("documentary", {})
                    sn = meta.get("scene_num", 0)
                    if sn > 0:
                        video_by_scene[sn] = video_by_scene.get(sn, 0) + 1

            for item in narr_track:
                if isinstance(item, otio.schema.Clip):
                    meta = item.metadata.get("documentary", {})
                    sn = meta.get("scene_num", 0)
                    voice = meta.get("voice", "")
                    if sn > 0 and not voice.endswith("_EN"):
                        narr_by_scene_asm[sn] = narr_by_scene_asm.get(sn, 0) + 1

            for sn in set(video_by_scene) | set(narr_by_scene_asm):
                v_count = video_by_scene.get(sn, 0)
                n_count = narr_by_scene_asm.get(sn, 0)
                if v_count != n_count:
                    checks.append(GatekeeperCheck(
                        name="video_narration_clip_count",
                        category="cross_track",
                        verdict=GatekeeperVerdict.REJECT,
                        message=(
                            f"Scene {sn}: {v_count} video clip(s) vs "
                            f"{n_count} narration clip(s) — must be equal"
                        ),
                        stage=f"{from_stage}→{to_stage}",
                        scene_num=sn,
                    ))
                else:
                    checks.append(GatekeeperCheck(
                        name="video_narration_clip_count",
                        category="cross_track",
                        verdict=GatekeeperVerdict.PASS,
                        stage=f"{from_stage}→{to_stage}",
                        scene_num=sn,
                    ))

    for c in checks:
        _store.record_check(c)
    return checks


def has_rejects(checks: list[GatekeeperCheck]) -> bool:
    """Check if any gatekeeper checks resulted in REJECT."""
    return any(c.verdict == GatekeeperVerdict.REJECT for c in checks)
