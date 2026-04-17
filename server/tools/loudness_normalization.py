"""Two-phase EBU R128 loudness normalization.

The PAG run measured integrated loudness -21.7 LUFS with LRA 7.4 LU and a
4.4 LU gap between the loudest and quietest 10-second narration windows.
Narration masters are expected to sit ≤ 3-5 LU for LRA, so both the
per-clip and the final-master loudness have to be controlled explicitly.

This module implements two distinct phases:

* **Phase A — per-clip.**  Each TTS clip is normalised to -23 LUFS with a
  two-pass ``loudnorm`` right after synthesis.  This closes the 4.4 LU
  per-window gap without destroying dynamics.  The -23 LUFS / LRA 7 target
  matches EBU R128 broadcast spec for narration pre-master.

* **Phase B — final master.**  At assembly time, after concatenation and
  before mux, the full narration stream is re-normalised to the master
  profile's integrated LUFS target (``YOUTUBE_1080P`` → -14 LUFS) and the
  result is verified.  Tolerances are ±0.5 LU on the integrated target and
  ≤ ``profile.max_lra`` LU on the loudness range; if either is violated
  the tool raises :class:`LoudnessOutOfSpec`.

The caller layer (``assembly_tools``) is responsible for ducking music
and SFX tracks before Phase B so that the measured loudness reflects the
broadcast mix, not just the narration.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from .master_profiles import MasterProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LoudnessMeasurementFailed(RuntimeError):
    """Raised when ffmpeg refuses to measure loudness on an input."""


class LoudnessOutOfSpec(RuntimeError):
    """Raised when post-normalisation loudness violates tolerance."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoudnessStats:
    """A single EBU R128 measurement from ``loudnorm`` pass 1."""

    input_i: float           # integrated loudness (LUFS)
    input_tp: float          # true peak (dBTP)
    input_lra: float         # loudness range (LU)
    input_thresh: float      # gating threshold (LUFS)

    def as_loudnorm_kwargs(self) -> dict:
        """Return values formatted for a second-pass ``loudnorm``."""
        return {
            "measured_I": f"{self.input_i:.2f}",
            "measured_TP": f"{self.input_tp:.2f}",
            "measured_LRA": f"{self.input_lra:.2f}",
            "measured_thresh": f"{self.input_thresh:.2f}",
        }


@dataclass(frozen=True)
class LoudnessResult:
    """Outcome of a two-pass loudnorm run."""

    input_path: str
    output_path: str
    target_lufs: float
    true_peak_db: float
    measured_before: LoudnessStats
    measured_after: LoudnessStats

    def as_log_dict(self) -> dict:
        return {
            "input_path": self.input_path,
            "output_path": self.output_path,
            "target_lufs": self.target_lufs,
            "true_peak_db": self.true_peak_db,
            "before": {
                "i": self.measured_before.input_i,
                "tp": self.measured_before.input_tp,
                "lra": self.measured_before.input_lra,
            },
            "after": {
                "i": self.measured_after.input_i,
                "tp": self.measured_after.input_tp,
                "lra": self.measured_after.input_lra,
            },
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_ffmpeg(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _parse_loudnorm_json(stderr: str) -> Optional[LoudnessStats]:
    """Parse the JSON block that ``loudnorm`` writes to stderr in pass 1.

    Returns ``None`` if no valid block was found; callers decide whether
    to raise or degrade.
    """
    start = stderr.rfind("{")
    end = stderr.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        stats = json.loads(stderr[start:end])
    except json.JSONDecodeError:
        return None

    def _f(key: str, default: float) -> float:
        raw = stats.get(key, default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    return LoudnessStats(
        input_i=_f("input_i", -70.0),
        input_tp=_f("input_tp", -70.0),
        input_lra=_f("input_lra", 0.0),
        input_thresh=_f("input_thresh", -70.0),
    )


def measure_loudness(
    input_path: str,
    target_lufs: float = -23.0,
    true_peak_db: float = -2.0,
    lra: float = 7.0,
    timeout: int = 120,
) -> LoudnessStats:
    """Measure integrated loudness / TP / LRA with ``loudnorm`` pass 1.

    ``target_lufs`` / ``true_peak_db`` / ``lra`` only affect gating — the
    measurement itself is of the input signal.  Raises
    :class:`LoudnessMeasurementFailed` on ffmpeg errors.
    """
    if not os.path.exists(input_path):
        raise LoudnessMeasurementFailed(f"Input file not found: {input_path}")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af",
        f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA={lra}:print_format=json",
        "-f", "null", "-",
    ]
    result = _run_ffmpeg(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LoudnessMeasurementFailed(
            f"ffmpeg loudnorm measurement failed rc={result.returncode}: "
            f"{result.stderr[-500:]}"
        )
    stats = _parse_loudnorm_json(result.stderr)
    if stats is None:
        raise LoudnessMeasurementFailed(
            f"Could not parse loudnorm JSON for {input_path}"
        )
    return stats


def _two_pass_loudnorm(
    input_path: str,
    output_path: str,
    target_lufs: float,
    true_peak_db: float,
    lra: float,
    sample_rate: int,
    audio_codec: Optional[str] = None,
    audio_bitrate: Optional[str] = None,
    timeout: int = 600,
) -> LoudnessResult:
    """Run measurement + linear application, then remeasure the output.

    Always a two-pass run — single-pass loudnorm degrades dynamics in a
    way that is visible on narration.  If measurement fails we raise
    rather than silently degrade (see skill: "Never silently degrade").
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    before = measure_loudness(
        input_path,
        target_lufs=target_lufs,
        true_peak_db=true_peak_db,
        lra=lra,
        timeout=timeout,
    )

    # Pass 2 — apply with linear gain using the measured stats.
    linear_kwargs = before.as_loudnorm_kwargs()
    af_filter = (
        f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA={lra}:"
        + ":".join(f"{k}={v}" for k, v in linear_kwargs.items())
        + ":linear=true:print_format=summary"
    )

    cmd: list = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", af_filter,
        "-ar", str(sample_rate),
    ]
    if audio_codec:
        cmd += ["-c:a", audio_codec]
    if audio_bitrate:
        cmd += ["-b:a", audio_bitrate]
    cmd.append(output_path)

    result = _run_ffmpeg(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LoudnessMeasurementFailed(
            f"loudnorm pass 2 failed rc={result.returncode}: "
            f"{result.stderr[-500:]}"
        )

    # Remeasure the output so the caller can verify tolerances.
    after = measure_loudness(
        output_path,
        target_lufs=target_lufs,
        true_peak_db=true_peak_db,
        lra=lra,
        timeout=timeout,
    )
    return LoudnessResult(
        input_path=input_path,
        output_path=output_path,
        target_lufs=target_lufs,
        true_peak_db=true_peak_db,
        measured_before=before,
        measured_after=after,
    )


# ---------------------------------------------------------------------------
# Phase A — per-clip
# ---------------------------------------------------------------------------

# Narration per-clip intermediate target.  -23 LUFS / LRA 7 is the EBU R128
# default.  The second master pass (Phase B) lifts this to the profile's
# delivery target, but keeping every clip at -23 LUFS closes the 4.4 LU
# per-window gap measured on the PAG run.
PER_CLIP_TARGET_LUFS = -23.0
PER_CLIP_TRUE_PEAK_DB = -2.0
PER_CLIP_LRA = 7.0


def normalize_clip(
    input_path: str,
    output_path: str,
    target_lufs: float = PER_CLIP_TARGET_LUFS,
    true_peak_db: float = PER_CLIP_TRUE_PEAK_DB,
    lra: float = PER_CLIP_LRA,
    sample_rate: int = 48000,
) -> LoudnessResult:
    """Phase A: normalise a single TTS clip in place-style (writes output).

    Uses a two-pass ``loudnorm`` so gain is applied linearly — critical
    for narration where breath tails must not be over-compressed.

    The caller (TTS callback) is expected to replace the original clip
    with ``output_path`` and log pre/post stats for the dashboard.
    """
    result = _two_pass_loudnorm(
        input_path=input_path,
        output_path=output_path,
        target_lufs=target_lufs,
        true_peak_db=true_peak_db,
        lra=lra,
        sample_rate=sample_rate,
        audio_codec=None,       # keep pcm_s16le for WAV intermediate
        audio_bitrate=None,
        timeout=300,
    )
    logger.info(
        "Phase A loudnorm: %s I=%.2f->%.2f LUFS, LRA=%.2f->%.2f",
        input_path,
        result.measured_before.input_i,
        result.measured_after.input_i,
        result.measured_before.input_lra,
        result.measured_after.input_lra,
    )
    return result


# ---------------------------------------------------------------------------
# Phase B — final master
# ---------------------------------------------------------------------------

# Tolerance on the integrated loudness target after the master pass.  EBU R128
# compliance allows ±1 LU; we pick ±0.5 LU so that upstream music bedding
# cannot accidentally push the deliverable out of spec without tripping.
MASTER_I_TOLERANCE_LU = 0.5


def normalize_master(
    input_path: str,
    output_path: str,
    profile: MasterProfile,
    pcm_intermediate: bool = False,
) -> LoudnessResult:
    """Phase B: bring the full mix to the profile's loudness envelope.

    Runs the same two-pass loudnorm as Phase A but re-targets the
    profile's ``integrated_lufs`` / ``true_peak_db`` and encodes with the
    profile's audio codec + bitrate + sample rate.  Raises
    :class:`LoudnessOutOfSpec` if the remeasured output deviates from the
    target by more than ``MASTER_I_TOLERANCE_LU`` LU or if LRA exceeds
    ``profile.max_lra``.

    When ``pcm_intermediate=True`` the output is written as lossless PCM
    (so ``output_path`` must end in ``.wav``) and the profile's lossy
    codec is **not** applied.  This is the right choice when Phase B is
    an intermediate step feeding a later mux (see
    :func:`finalize_master`) — it keeps the audio lossless until the
    single final AAC encode in the mux step, avoiding 2-3 generations of
    lossy transcoding.
    """
    if pcm_intermediate:
        audio_codec: Optional[str] = None
        audio_bitrate: Optional[str] = None
        if not output_path.lower().endswith(".wav"):
            raise ValueError(
                "normalize_master(pcm_intermediate=True) requires a .wav "
                f"output_path, got {output_path!r}"
            )
    else:
        audio_codec = profile.audio_codec
        audio_bitrate = profile.audio_bitrate

    result = _two_pass_loudnorm(
        input_path=input_path,
        output_path=output_path,
        target_lufs=profile.integrated_lufs,
        true_peak_db=profile.true_peak_db,
        lra=profile.max_lra,
        sample_rate=profile.audio_sample_rate,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        timeout=900,
    )
    verify_master(result, profile)
    logger.info(
        "Phase B loudnorm (%s): I=%.2f->%.2f LUFS (target %.1f), "
        "LRA=%.2f->%.2f (max %.1f)",
        profile.name,
        result.measured_before.input_i,
        result.measured_after.input_i,
        profile.integrated_lufs,
        result.measured_before.input_lra,
        result.measured_after.input_lra,
        profile.max_lra,
    )
    return result


def verify_master(result: LoudnessResult, profile: MasterProfile) -> None:
    """Assert the master pass hit the profile envelope; raise if not.

    Two hard checks:

    * integrated LUFS within ±``MASTER_I_TOLERANCE_LU`` of the profile
      target;
    * loudness range ≤ ``profile.max_lra``.
    """
    i_err = abs(result.measured_after.input_i - profile.integrated_lufs)
    if i_err > MASTER_I_TOLERANCE_LU:
        raise LoudnessOutOfSpec(
            f"Integrated LUFS {result.measured_after.input_i:.2f} deviates "
            f"from {profile.integrated_lufs:.1f} by {i_err:.2f} LU "
            f"(tolerance {MASTER_I_TOLERANCE_LU} LU)"
        )
    if result.measured_after.input_lra > profile.max_lra:
        raise LoudnessOutOfSpec(
            f"LRA {result.measured_after.input_lra:.2f} exceeds "
            f"profile.max_lra {profile.max_lra:.1f} LU"
        )
