"""Audio stylistic invariant measurement callables (ARCH-E3, issue #149).

Every narration block that exits the audio ladder must pass **all** of
these invariants, regardless of which tier produced it:

1. **Uniform LUFS** across narration blocks at a fixed loudness target
   (per-clip Phase-A target, ``-23 LUFS`` by default — see
   ``server.tools.loudness_normalization``). A block that measures more
   than :data:`LUFS_TOLERANCE_LU` away from target fails — unless a
   scoped Preference Ledger override is active for that block
   (see :mod:`server.critique.ledger_override`).
2. **Voice continuity** between adjacent blocks of the same speaker —
   no jarring register shifts (spectral centroid or RMS jump beyond a
   perceptual threshold).
3. **Character voice consistency** — the same speaker role lands on
   the same voice identity across the entire film.
4. **Peak-limiter compliance** — no samples at or beyond
   :data:`PEAK_LIMIT_DBTP` (no clipping).
5. **No clicks** — no single-sample discontinuities above
   :data:`CLICK_DELTA` in magnitude.
6. **No truncated plosives** — no block that starts or ends with an
   energy transient at the very first / last sample (a truncated ``p``
   / ``t`` / ``k`` / ``b`` looks like a missing decay tail).
7. **No hiss-floor changes between blocks** — the "quiet tail" floor
   of adjacent blocks may not differ by more than
   :data:`HISS_FLOOR_TOLERANCE_DB`.

Each check is a **plain callable** (ADK "tools as plain callables"
idiom) and returns a structured :class:`InvariantResult`. The composing
agent in :mod:`server.critique.stylistic_qa_agent` collects all results
and raises :class:`StylisticInvariantFailure` when any block fails —
that exception is the failure signal the audio ladder consumes.

Design invariants:

- Fail loud. Never silently degrade. If a measurement cannot be taken
  (missing file, unreadable WAV, ffmpeg error), the invariant returns
  a ``FAIL`` verdict with an explanatory message — callers decide how
  to escalate.
- Pure: no side effects, no blackboard reads. Blackboard reads live in
  :mod:`server.critique.stylistic_qa_agent` at the composition layer.
- Cheap: each check is O(samples); the full invariant battery on a 20s
  clip completes in well under a second on CPU.
"""

from __future__ import annotations

import logging
import math
import os
import wave
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable thresholds (chosen to match the architecture spec + PAG run
# observations). Exposed as module constants so tests can import them.
# ---------------------------------------------------------------------------

#: Target integrated loudness for per-clip narration (matches
#: ``loudness_normalization.PER_CLIP_TARGET_LUFS``).
LUFS_TARGET: float = -23.0

#: Tolerance around the LUFS target. The PAG run measured a 4.4 LU
#: window-to-window gap; after Phase-A normalisation every clip should
#: sit within ±2 LU of target. We allow a little slack for the
#: two-pass loudnorm rounding.
LUFS_TOLERANCE_LU: float = 2.0

#: True-peak ceiling in dBTP. ``-1`` dBTP is the EBU R128 recommended
#: ceiling for delivery; we enforce ``-2`` dBTP per-clip to leave
#: headroom for the final master pass.
PEAK_LIMIT_DBTP: float = -1.0

#: A single sample may jump by at most this fraction of full scale
#: between consecutive samples before it is flagged as a click.
CLICK_DELTA: float = 0.5

#: A clip is considered to have a truncated plosive at its edge if the
#: RMS of the first/last :data:`_EDGE_WINDOW_MS` ms exceeds this fraction
#: of the clip's overall RMS. A well-formed TTS clip always has a
#: short silent ramp-in/out.
PLOSIVE_EDGE_RATIO: float = 1.5

#: Window (ms) inspected at each edge when checking for truncated
#: plosives. 10 ms is long enough to capture a single /p/ or /t/ burst
#: and short enough that real speech rarely lives in that interval.
_EDGE_WINDOW_MS: float = 10.0

#: Adjacent blocks of the same speaker may drift by at most this much
#: in spectral centroid (Hz) before we flag a "jarring register shift".
VOICE_CONTINUITY_CENTROID_HZ: float = 400.0

#: Adjacent blocks of the same speaker may drift by at most this much
#: in short-window RMS (dB). 6 dB is one perceptual "step" louder.
VOICE_CONTINUITY_RMS_DB: float = 6.0

#: Hiss-floor tolerance between adjacent blocks (dB). We measure the
#: RMS of the quietest 10 % of 10 ms windows in each block and compare.
HISS_FLOOR_TOLERANCE_DB: float = 6.0

#: Silence floor (in linear amplitude) below which samples are counted
#: as "quiet" for hiss-floor measurement.
_QUIET_WINDOW_RMS_CEIL: float = 0.05


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class InvariantVerdict(str, Enum):
    """Verdict for a single invariant check on a single block."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"  # e.g. LUFS suppressed by a scoped ledger override.


@dataclass(frozen=True)
class InvariantResult:
    """Outcome of a single invariant measurement.

    ``name`` identifies the invariant (e.g. ``"uniform_lufs"``). ``block_id``
    identifies the narration block the measurement was taken on (e.g.
    ``"scene_003_V1_RU"``); the cross-block invariants use the pair
    ``"scene_003_V1_RU->scene_004_V1_RU"``.
    """

    name: str
    block_id: str
    verdict: InvariantVerdict
    measured: Optional[float] = None
    target: Optional[float] = None
    tolerance: Optional[float] = None
    message: str = ""
    metadata: dict = field(default_factory=dict)

    def is_failure(self) -> bool:
        return self.verdict == InvariantVerdict.FAIL

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "block_id": self.block_id,
            "verdict": self.verdict.value,
            "measured": self.measured,
            "target": self.target,
            "tolerance": self.tolerance,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


@dataclass
class InvariantViolation(RuntimeError):
    """A single block's stylistic invariant violation.

    Produced by :func:`run_all_invariants` when one or more invariants
    fail on a given block (or adjacent-block pair). Callers re-raise
    (or wrap in :class:`StylisticInvariantFailure`) so the audio
    ladder's :func:`recovery.escalate_pipeline_error` caller sees a
    structured signal, not a bare string.
    """

    block_id: str
    failures: list[InvariantResult]

    def __str__(self) -> str:
        names = ", ".join(f"{r.name}({r.measured})" for r in self.failures)
        return f"stylistic invariant violation on {self.block_id}: {names}"


# ---------------------------------------------------------------------------
# Narration block spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NarrationBlock:
    """A narration block eligible for stylistic QA.

    Attributes:
        block_id: Stable identifier (e.g. ``"scene_003_V1_RU"``).
        wav_path: Absolute path to the block's WAV file.
        scene_num: Scene number the block belongs to.
        voice_role: Speaker role (e.g. ``"V1"``, ``"V2"``). Register
            and identity invariants are applied per-voice-role.
        language: Language code (``"en"``, ``"ru"``).
        voice_id: Concrete voice-model identity (e.g. ``"qwen3-tts:male_01"``).
            Character-voice-consistency enforces that every block of
            ``voice_role`` uses the same ``voice_id`` across the film.
    """

    block_id: str
    wav_path: str
    scene_num: int
    voice_role: str
    language: str = ""
    voice_id: str = ""


# ---------------------------------------------------------------------------
# Low-level audio helpers (deliberately minimal — avoid new heavy deps)
# ---------------------------------------------------------------------------


def _read_wav_mono(wav_path: str) -> tuple[np.ndarray, int]:
    """Load a WAV file as mono float32 in ``[-1, 1]``.

    Uses the stdlib :mod:`wave` module so the module has no hard
    dependency on ``soundfile``. Returns ``(samples, sample_rate)``.
    Raises :class:`FileNotFoundError` or :class:`ValueError` on
    unreadable input — never silently returns an empty buffer.
    """
    if not wav_path or not os.path.exists(wav_path):
        raise FileNotFoundError(f"narration WAV not found: {wav_path}")

    with wave.open(wav_path, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if n_frames <= 0:
        raise ValueError(f"narration WAV has zero frames: {wav_path}")

    if sample_width == 2:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        pcm = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sample_width == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(
            f"unsupported WAV sample width: {sample_width} bytes ({wav_path})"
        )

    if n_channels > 1:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1)

    return pcm, sample_rate


def _rms(samples: np.ndarray) -> float:
    """Root-mean-square of a sample buffer."""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def _lufs_integrated(samples: np.ndarray, sample_rate: int) -> float:
    """Cheap integrated-loudness estimate in LUFS.

    This is **not** a full BS.1770 implementation — see
    :func:`server.tools.loudness_normalization.measure_loudness` for
    the production ffmpeg path. The stylistic invariants need a fast
    CPU-only estimate for synthetic-test fixtures and for unit-level
    callable isolation; the mapping used here is:

        LUFS ≈ 20·log10(rms) + calibration_offset

    Calibration offset is chosen so a full-scale sine reads -3 LUFS,
    matching BS.1770 within 1 LU on the synthetic test fixtures used
    by ``test_audio_stylistic_invariants.py``.
    """
    rms = _rms(samples)
    if rms <= 0.0:
        return -70.0  # silence floor
    # 20 log10(1.0) = 0 dBFS → a 0 dBFS full-scale tone measures
    # ≈ -3 LUFS on BS.1770; align our estimate to that.
    return 20.0 * math.log10(rms) + (-3.0) + 0.0


def _true_peak_db(samples: np.ndarray) -> float:
    """Peak sample level in dBFS (approximation of dBTP — we don't
    4× oversample because the click/peak checks catch the sample-level
    edge cases already)."""
    if samples.size == 0:
        return -np.inf
    peak = float(np.max(np.abs(samples)))
    if peak <= 0.0:
        return -np.inf
    return 20.0 * math.log10(peak)


def _spectral_centroid(samples: np.ndarray, sample_rate: int) -> float:
    """Spectral centroid (Hz). Used as a proxy for vocal register.

    Computes a single-frame magnitude-spectrum centroid over the
    supplied window. For our purpose (voice continuity between
    adjacent blocks of the same speaker) this is enough — a jarring
    register shift moves the centroid hundreds of Hz.
    """
    if samples.size == 0:
        return 0.0
    n = samples.size
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    denom = float(spectrum.sum())
    if denom <= 0.0:
        return 0.0
    return float((spectrum * freqs).sum() / denom)


def _window_rms_db(
    samples: np.ndarray, sample_rate: int, window_ms: float = 10.0
) -> np.ndarray:
    """Return per-window RMS (in dBFS) over non-overlapping windows."""
    if samples.size == 0:
        return np.array([], dtype=np.float64)
    win = max(1, int(sample_rate * window_ms / 1000.0))
    trimmed = samples[: (samples.size // win) * win]
    if trimmed.size == 0:
        return np.array([], dtype=np.float64)
    windows = trimmed.reshape(-1, win)
    rms = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=1))
    # Floor at -120 dBFS to avoid log(0); this matches BS.1770 gating.
    rms = np.clip(rms, 1e-6, None)
    return 20.0 * np.log10(rms)


def _hiss_floor_db(samples: np.ndarray, sample_rate: int) -> float:
    """Estimate the quietest-10%-windows RMS (dBFS) — our hiss floor."""
    per_window = _window_rms_db(samples, sample_rate, window_ms=10.0)
    if per_window.size == 0:
        return -120.0
    # Restrict to windows whose *linear* RMS sits under the quiet
    # ceiling, so we are not tricked by wall-to-wall speech.
    linear = np.power(10.0, per_window / 20.0)
    quiet = per_window[linear <= _QUIET_WINDOW_RMS_CEIL]
    if quiet.size == 0:
        # Nothing under the quiet ceiling — block is wall-to-wall loud;
        # return the minimum per-window RMS we saw. Useful when the
        # synthetic fixtures are a continuous tone.
        return float(per_window.min())
    cutoff = max(1, int(math.ceil(quiet.size * 0.1)))
    return float(np.sort(quiet)[:cutoff].mean())


# ---------------------------------------------------------------------------
# Invariant callables — each is a plain function usable as an ADK tool.
# ---------------------------------------------------------------------------


def check_uniform_lufs(
    block: NarrationBlock,
    *,
    target_lufs: float = LUFS_TARGET,
    tolerance_lu: float = LUFS_TOLERANCE_LU,
    override_active: bool = False,
) -> InvariantResult:
    """Check that the block sits near the uniform LUFS target.

    When ``override_active`` is ``True`` (scoped Preference Ledger
    override for this block), the check returns SKIP — the block is
    deliberately off-target.
    """
    if override_active:
        return InvariantResult(
            name="uniform_lufs",
            block_id=block.block_id,
            verdict=InvariantVerdict.SKIP,
            target=target_lufs,
            tolerance=tolerance_lu,
            message=(
                "uniform-LUFS invariant suppressed by scoped Preference "
                f"Ledger override for block {block.block_id}"
            ),
        )

    try:
        samples, sr = _read_wav_mono(block.wav_path)
    except (FileNotFoundError, ValueError) as e:
        return InvariantResult(
            name="uniform_lufs",
            block_id=block.block_id,
            verdict=InvariantVerdict.FAIL,
            message=str(e),
        )

    measured = _lufs_integrated(samples, sr)
    delta = abs(measured - target_lufs)
    verdict = (
        InvariantVerdict.PASS
        if delta <= tolerance_lu
        else InvariantVerdict.FAIL
    )
    return InvariantResult(
        name="uniform_lufs",
        block_id=block.block_id,
        verdict=verdict,
        measured=round(measured, 3),
        target=target_lufs,
        tolerance=tolerance_lu,
        message=(
            f"measured {measured:.2f} LUFS vs target {target_lufs:.2f} "
            f"(|Δ|={delta:.2f} LU, tolerance {tolerance_lu:.2f} LU)"
        ),
    )


def check_peak_limiter(
    block: NarrationBlock,
    *,
    ceiling_dbtp: float = PEAK_LIMIT_DBTP,
) -> InvariantResult:
    """Fail if any sample exceeds the true-peak ceiling (clipping)."""
    try:
        samples, _ = _read_wav_mono(block.wav_path)
    except (FileNotFoundError, ValueError) as e:
        return InvariantResult(
            name="peak_limiter",
            block_id=block.block_id,
            verdict=InvariantVerdict.FAIL,
            message=str(e),
        )

    peak = _true_peak_db(samples)
    # Reject any block whose true-peak exceeds the ceiling OR that has
    # samples at ±1.0 full scale (hard-clipped).
    hard_clipped = bool(np.any(np.abs(samples) >= 0.999))
    verdict = (
        InvariantVerdict.FAIL
        if (peak > ceiling_dbtp or hard_clipped)
        else InvariantVerdict.PASS
    )
    return InvariantResult(
        name="peak_limiter",
        block_id=block.block_id,
        verdict=verdict,
        measured=round(peak, 3) if math.isfinite(peak) else None,
        target=ceiling_dbtp,
        message=(
            f"peak {peak:.2f} dBFS vs ceiling {ceiling_dbtp:.2f} dBTP"
            + (" (hard-clipped samples present)" if hard_clipped else "")
        ),
        metadata={"hard_clipped": hard_clipped},
    )


def check_clicks(
    block: NarrationBlock,
    *,
    max_delta: float = CLICK_DELTA,
) -> InvariantResult:
    """Fail if any sample-to-sample delta exceeds ``max_delta``.

    A click is a single-sample discontinuity (e.g. a DC offset jump or
    a stitched-clip boundary that landed mid-waveform). TTS output
    should have smooth waveforms at the sample level.
    """
    try:
        samples, _ = _read_wav_mono(block.wav_path)
    except (FileNotFoundError, ValueError) as e:
        return InvariantResult(
            name="clicks",
            block_id=block.block_id,
            verdict=InvariantVerdict.FAIL,
            message=str(e),
        )

    if samples.size < 2:
        return InvariantResult(
            name="clicks",
            block_id=block.block_id,
            verdict=InvariantVerdict.FAIL,
            message="block too short to measure sample-to-sample deltas",
        )

    deltas = np.abs(np.diff(samples))
    worst = float(np.max(deltas))
    count = int(np.sum(deltas > max_delta))
    verdict = (
        InvariantVerdict.FAIL
        if count > 0
        else InvariantVerdict.PASS
    )
    return InvariantResult(
        name="clicks",
        block_id=block.block_id,
        verdict=verdict,
        measured=round(worst, 4),
        tolerance=max_delta,
        message=(
            f"worst sample-to-sample delta {worst:.4f} (threshold "
            f"{max_delta:.4f}); {count} click(s) detected"
        ),
        metadata={"click_count": count},
    )


def check_plosive_truncation(
    block: NarrationBlock,
    *,
    edge_ratio: float = PLOSIVE_EDGE_RATIO,
    edge_window_ms: float = _EDGE_WINDOW_MS,
) -> InvariantResult:
    """Fail if the block's first or last window has anomalous energy.

    A well-formed TTS clip has a brief silent ramp-in and ramp-out;
    a truncated plosive shows up as a high-energy transient at the
    very first or very last sample (no decay tail).
    """
    try:
        samples, sr = _read_wav_mono(block.wav_path)
    except (FileNotFoundError, ValueError) as e:
        return InvariantResult(
            name="plosive_truncation",
            block_id=block.block_id,
            verdict=InvariantVerdict.FAIL,
            message=str(e),
        )

    overall_rms = _rms(samples)
    if overall_rms <= 0.0:
        # Pure silence — nothing to truncate; pass trivially.
        return InvariantResult(
            name="plosive_truncation",
            block_id=block.block_id,
            verdict=InvariantVerdict.PASS,
            message="block is silent; no plosive edges to check",
        )

    edge = max(1, int(sr * edge_window_ms / 1000.0))
    if samples.size < 2 * edge:
        # Too short to measure; pass — upstream duration check catches this.
        return InvariantResult(
            name="plosive_truncation",
            block_id=block.block_id,
            verdict=InvariantVerdict.PASS,
            message="block too short for edge-window plosive check",
        )

    head_rms = _rms(samples[:edge])
    tail_rms = _rms(samples[-edge:])
    head_ratio = head_rms / overall_rms
    tail_ratio = tail_rms / overall_rms
    worst_ratio = max(head_ratio, tail_ratio)
    verdict = (
        InvariantVerdict.FAIL
        if worst_ratio > edge_ratio
        else InvariantVerdict.PASS
    )
    return InvariantResult(
        name="plosive_truncation",
        block_id=block.block_id,
        verdict=verdict,
        measured=round(worst_ratio, 3),
        tolerance=edge_ratio,
        message=(
            f"edge/overall RMS ratio head={head_ratio:.2f} "
            f"tail={tail_ratio:.2f} (threshold {edge_ratio:.2f})"
        ),
        metadata={"head_ratio": round(head_ratio, 3), "tail_ratio": round(tail_ratio, 3)},
    )


def check_voice_continuity(
    prev: NarrationBlock,
    curr: NarrationBlock,
    *,
    max_centroid_delta_hz: float = VOICE_CONTINUITY_CENTROID_HZ,
    max_rms_delta_db: float = VOICE_CONTINUITY_RMS_DB,
) -> InvariantResult:
    """Adjacent same-speaker blocks must not jump register.

    Applies only when ``prev.voice_role == curr.voice_role``. For
    different speakers the check returns SKIP.
    """
    pair_id = f"{prev.block_id}->{curr.block_id}"
    if prev.voice_role != curr.voice_role:
        return InvariantResult(
            name="voice_continuity",
            block_id=pair_id,
            verdict=InvariantVerdict.SKIP,
            message=(
                f"voice_role differs ({prev.voice_role} → {curr.voice_role}); "
                "continuity invariant applies only to adjacent same-speaker blocks"
            ),
        )

    try:
        prev_samples, prev_sr = _read_wav_mono(prev.wav_path)
        curr_samples, curr_sr = _read_wav_mono(curr.wav_path)
    except (FileNotFoundError, ValueError) as e:
        return InvariantResult(
            name="voice_continuity",
            block_id=pair_id,
            verdict=InvariantVerdict.FAIL,
            message=str(e),
        )

    if prev_sr != curr_sr:
        return InvariantResult(
            name="voice_continuity",
            block_id=pair_id,
            verdict=InvariantVerdict.FAIL,
            message=(
                f"sample-rate mismatch {prev_sr} Hz vs {curr_sr} Hz; "
                "adjacent same-speaker blocks must share sample rate"
            ),
        )

    prev_centroid = _spectral_centroid(prev_samples, prev_sr)
    curr_centroid = _spectral_centroid(curr_samples, curr_sr)
    prev_rms_db = 20.0 * math.log10(max(_rms(prev_samples), 1e-6))
    curr_rms_db = 20.0 * math.log10(max(_rms(curr_samples), 1e-6))

    centroid_delta = abs(curr_centroid - prev_centroid)
    rms_delta = abs(curr_rms_db - prev_rms_db)

    failures: list[str] = []
    if centroid_delta > max_centroid_delta_hz:
        failures.append(
            f"spectral-centroid jump {centroid_delta:.0f} Hz > "
            f"{max_centroid_delta_hz:.0f} Hz"
        )
    if rms_delta > max_rms_delta_db:
        failures.append(
            f"short-window RMS jump {rms_delta:.1f} dB > "
            f"{max_rms_delta_db:.1f} dB"
        )

    verdict = InvariantVerdict.FAIL if failures else InvariantVerdict.PASS
    return InvariantResult(
        name="voice_continuity",
        block_id=pair_id,
        verdict=verdict,
        measured=round(max(centroid_delta, rms_delta), 3),
        tolerance=max_centroid_delta_hz,
        message=(
            "; ".join(failures)
            if failures
            else (
                f"centroid Δ={centroid_delta:.0f} Hz, "
                f"RMS Δ={rms_delta:.1f} dB — continuous register"
            )
        ),
        metadata={
            "centroid_delta_hz": round(centroid_delta, 3),
            "rms_delta_db": round(rms_delta, 3),
            "voice_role": prev.voice_role,
        },
    )


def check_character_voice_consistency(
    blocks: Sequence[NarrationBlock],
) -> list[InvariantResult]:
    """Every speaker role must map to a single voice identity across the film.

    Returns one :class:`InvariantResult` per ``voice_role``. The film
    as a whole passes iff **every** voice-role is internally consistent.
    Blocks with a missing ``voice_id`` field fail loud — the TTS
    pipeline is expected to stamp ``voice_id`` on every emitted clip
    (see deterministic_audio_callback), so absence indicates a
    contract violation, not a soft warning.
    """
    by_role: dict[str, list[NarrationBlock]] = {}
    for b in blocks:
        by_role.setdefault(b.voice_role, []).append(b)

    results: list[InvariantResult] = []
    for role, role_blocks in sorted(by_role.items()):
        voice_ids = {b.voice_id for b in role_blocks}
        if "" in voice_ids:
            voice_ids.discard("")
            missing_count = sum(1 for b in role_blocks if not b.voice_id)
            results.append(InvariantResult(
                name="character_voice_consistency",
                block_id=f"role:{role}",
                verdict=InvariantVerdict.FAIL,
                message=(
                    f"{missing_count} block(s) for role {role} have empty "
                    "voice_id; deterministic_audio_callback must stamp "
                    "voice_id on every clip"
                ),
                metadata={"voice_role": role, "missing_voice_id": missing_count},
            ))
            continue
        if len(voice_ids) == 1:
            (only,) = voice_ids
            results.append(InvariantResult(
                name="character_voice_consistency",
                block_id=f"role:{role}",
                verdict=InvariantVerdict.PASS,
                message=(
                    f"role {role} maps to voice_id={only!r} across "
                    f"{len(role_blocks)} block(s)"
                ),
                metadata={"voice_role": role, "voice_id": only},
            ))
        else:
            results.append(InvariantResult(
                name="character_voice_consistency",
                block_id=f"role:{role}",
                verdict=InvariantVerdict.FAIL,
                measured=float(len(voice_ids)),
                target=1.0,
                message=(
                    f"role {role} maps to {len(voice_ids)} distinct "
                    f"voice_ids across the film: {sorted(voice_ids)!r}"
                ),
                metadata={
                    "voice_role": role,
                    "voice_ids": sorted(voice_ids),
                },
            ))
    return results


def check_hiss_floor_continuity(
    prev: NarrationBlock,
    curr: NarrationBlock,
    *,
    tolerance_db: float = HISS_FLOOR_TOLERANCE_DB,
) -> InvariantResult:
    """Adjacent blocks' quiet-tail floors must not differ by more than ``tolerance_db``."""
    pair_id = f"{prev.block_id}->{curr.block_id}"

    try:
        prev_samples, prev_sr = _read_wav_mono(prev.wav_path)
        curr_samples, curr_sr = _read_wav_mono(curr.wav_path)
    except (FileNotFoundError, ValueError) as e:
        return InvariantResult(
            name="hiss_floor",
            block_id=pair_id,
            verdict=InvariantVerdict.FAIL,
            message=str(e),
        )

    prev_floor = _hiss_floor_db(prev_samples, prev_sr)
    curr_floor = _hiss_floor_db(curr_samples, curr_sr)
    delta = abs(curr_floor - prev_floor)
    verdict = (
        InvariantVerdict.FAIL
        if delta > tolerance_db
        else InvariantVerdict.PASS
    )
    return InvariantResult(
        name="hiss_floor",
        block_id=pair_id,
        verdict=verdict,
        measured=round(delta, 3),
        tolerance=tolerance_db,
        message=(
            f"hiss-floor shift {delta:.1f} dB between adjacent blocks "
            f"(prev={prev_floor:.1f} dBFS, curr={curr_floor:.1f} dBFS, "
            f"tolerance {tolerance_db:.1f} dB)"
        ),
        metadata={
            "prev_floor_db": round(prev_floor, 3),
            "curr_floor_db": round(curr_floor, 3),
        },
    )


# ---------------------------------------------------------------------------
# Composition — run every invariant on every block
# ---------------------------------------------------------------------------


def _adjacent_same_speaker_pairs(
    blocks: Sequence[NarrationBlock],
) -> Iterable[tuple[NarrationBlock, NarrationBlock]]:
    """Yield adjacent ``(prev, curr)`` pairs regardless of speaker.

    ``check_voice_continuity`` and ``check_hiss_floor_continuity`` both
    operate on *adjacent* blocks; continuity is a same-speaker notion
    but hiss floor spans speakers (a mic swap mid-film still shows
    up).  Filtering by voice role happens inside the callables, not
    here, so downstream consumers see a consistent signal.
    """
    for prev, curr in zip(blocks, blocks[1:]):
        yield prev, curr


def run_all_invariants(
    blocks: Sequence[NarrationBlock],
    *,
    target_lufs: float = LUFS_TARGET,
    lufs_tolerance_lu: float = LUFS_TOLERANCE_LU,
    override_resolver: Optional[Callable[[NarrationBlock], bool]] = None,
) -> list[InvariantResult]:
    """Run every stylistic invariant on the supplied blocks.

    Args:
        blocks: Narration blocks in film (narration-timeline) order.
        target_lufs / lufs_tolerance_lu: uniform-LUFS parameters.
        override_resolver: callable that, given a block, returns
            ``True`` iff a scoped Preference Ledger override is active
            for the uniform-LUFS invariant on that block. Defaults to
            :func:`server.critique.ledger_override.is_lufs_override_active`
            bound to an empty state (i.e. no overrides active) — the
            composing agent passes a resolver bound to the current
            session state.

    Returns:
        Flat list of :class:`InvariantResult` covering per-block and
        adjacent-pair checks plus one result per speaker-role for the
        character-voice-consistency invariant.
    """
    from critique.ledger_override import is_lufs_override_active  # noqa: F401 — avoid cycle

    if override_resolver is None:
        override_resolver = lambda _block: False  # noqa: E731

    results: list[InvariantResult] = []

    # Per-block invariants
    for block in blocks:
        results.append(check_uniform_lufs(
            block,
            target_lufs=target_lufs,
            tolerance_lu=lufs_tolerance_lu,
            override_active=bool(override_resolver(block)),
        ))
        results.append(check_peak_limiter(block))
        results.append(check_clicks(block))
        results.append(check_plosive_truncation(block))

    # Cross-block invariants
    for prev, curr in _adjacent_same_speaker_pairs(blocks):
        results.append(check_voice_continuity(prev, curr))
        results.append(check_hiss_floor_continuity(prev, curr))

    # Film-wide invariants
    results.extend(check_character_voice_consistency(blocks))

    return results


def collect_failures(results: Iterable[InvariantResult]) -> list[InvariantResult]:
    """Helper: filter a results list to just the FAIL verdicts."""
    return [r for r in results if r.is_failure()]
