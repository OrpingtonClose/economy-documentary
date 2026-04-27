"""Slice 9l — pixel- and ffprobe-level quality gates wired as orchestrator tools.

The slice 9j-quality-pass run shipped a frozen-frame "documentary"
because every gate in the live path was *plumbing* (bytes flowed,
ffmpeg ran, file had ``ftypisom`` magic). None of them decoded a
frame or compared per-scene narration duration to per-scene rendered
video duration. The mode duality (deleted in slice 9k / PR #371) hid
this further: the scripted demo brain auto-approved every gate
without invoking the LLM-backed evaluators that exist in
``/components``.

This module ships three deterministic, credential-free gates the
orchestrator must call on every scene before assembly:

* :func:`qa_duration_align` — ffprobes the audio and video artifacts
  and hard-fails when ``|audio_dur - video_dur| > tolerance``. Catches
  the exact slice 9j regression: a 3.7 s video paired with a 13 s
  narration trips the gate at ``delta=9.3 s``.
* :func:`qa_stills_judge` — decodes ``num_samples`` evenly-spaced
  frames with ffmpeg and computes mean inter-frame L1 pixel delta.
  Hard-fails when the mean delta drops below ``min_mean_pixel_delta``.
  This catches a video that has the right duration but is visually
  frozen (the muxer pads the last frame, or LTX-2.3 emits N
  near-identical frames). Pure numpy — no vision LLM required, so
  CI stays hermetic.
* :func:`qa_video_artifact_probe` — thin wrapper around ffprobe
  exposing duration / frame count / dimensions / codec. The other
  two gates use it internally; exposing it as a tool gives the
  orchestrator (and the escalation SubAgent) a way to inspect an
  artifact without a second ffprobe invocation.

All three return the same envelope shape::

    {
        "tool": "<gate name>",
        "verdict": "pass" | "fail",
        "scene_id": "scene_<n>",
        ...gate-specific evidence fields...,
    }

When ``verdict == "fail"`` the orchestrator must delegate to the
``escalation`` SubAgent per AGENTS.md hard invariants §3-5 ("fail
closed on TTS / video render", "QA immediately after each artifact").
The orchestrator prompt enforces this; see ``ORCHESTRATOR_PROMPT``
in :mod:`strands_agents.pipeline`.

The gates are intentionally **deterministic** so they survive a
hermetic CI run: same MP4 in, same verdict out. The vision-LLM
visual-coherence judge that lives in
:mod:`strands_agents.coherence_evaluator` operates on concept-level
text (style_lock + shot_type strings) and is invoked at a
different point in the pipeline.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


#: Default tolerance for :func:`qa_duration_align`. Anything tighter
#: false-positives on legitimate worker variance (LTX-2.3 rounds
#: frame counts to the 8k+1 grid; Qwen3-TTS adds a small trailing
#: silence). 0.5 s is what the user instructed in the slice 9j
#: post-mortem ("hard fail if |delta| > 0.5s").
DEFAULT_DURATION_TOLERANCE_S: float = 0.5


#: Default frame-sample count for :func:`qa_stills_judge`. Eight
#: evenly-spaced samples covers a 4-15 s clip well — every sample
#: lands in a different ~0.5 s window so the mean delta is dominated
#: by motion, not by adjacent-frame noise.
DEFAULT_STILLS_NUM_SAMPLES: int = 8


#: Minimum mean inter-frame L1 pixel delta (0-255 scale). Below this
#: the clip is judged a still. Determined empirically from LTX-2.3
#: BASIC outputs: a healthy clip averages ~12-25 (slow camera
#: documentary shots), a fully frozen clip averages ~0.05 (only
#: encoding noise). 1.5 is comfortably above noise floor and below
#: any real-motion clip we've sampled.
DEFAULT_MIN_MEAN_PIXEL_DELTA: float = 1.5


#: Minimum video size (bytes) we will probe. Smaller files are
#: certainly truncated and we'd rather fail loudly than have
#: ffprobe report ``duration_sec=0`` and confuse the orchestrator.
MIN_VIDEO_BYTES: int = 1024


#: Slice 9p — :func:`qa_audio_completeness` thresholds.
#:
#: ``MIN_TRAILING_SILENCE_S`` is the minimum amount of trailing
#: silence a healthy narration must have. Qwen3-TTS ends a sentence
#: with a natural pause (typically 200-400 ms of decay + room tone);
#: a hard cut produces a file that ends *with* spoken energy and
#: zero trailing silence. We accept anything ≥ 0.15 s as natural.
#: Anything shorter is a candidate for "abruptly cut narration"
#: and the gate fails closed.
#:
#: ``SILENCE_NOISE_DB`` is the dBFS floor below which a sample is
#: counted as silence. Qwen3-TTS room-tone sits around -45 dBFS;
#: -40 dBFS is comfortably above that floor and below any real
#: speech energy.
MIN_TRAILING_SILENCE_S: float = 0.15
SILENCE_NOISE_DB: float = -40.0


#: Minimum duration before a narration audio file is even worth
#: probing for trailing silence. Below this the recording is too
#: short for trailing silence to be meaningful (Qwen3-TTS warmup
#: artifacts can dominate).
MIN_NARRATION_DURATION_S: float = 0.5


VERDICT_PASS: str = "pass"
VERDICT_FAIL: str = "fail"


class _ProbeError(RuntimeError):
    """Raised when ffprobe fails or returns an unparseable payload."""


def _ffprobe_path() -> str:
    """Resolve ffprobe on ``PATH``; raise loudly when missing."""
    found = shutil.which("ffprobe")
    if not found:
        raise _ProbeError("ffprobe not found on PATH")
    return found


def _ffmpeg_path() -> str:
    """Resolve ffmpeg on ``PATH``; raise loudly when missing."""
    found = shutil.which("ffmpeg")
    if not found:
        raise _ProbeError("ffmpeg not found on PATH")
    return found


def _ffprobe_json(media_path: Path) -> dict[str, Any]:
    """Run ``ffprobe -of json`` and return the parsed payload."""
    cmd = [
        _ffprobe_path(),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except subprocess.CalledProcessError as exc:
        raise _ProbeError(
            f"ffprobe failed for {media_path}: rc={exc.returncode} "
            f"stderr={exc.stderr.strip()[:200]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _ProbeError(f"ffprobe timed out for {media_path}") from exc
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise _ProbeError(
            f"ffprobe emitted non-JSON for {media_path}: {completed.stdout[:200]}"
        ) from exc


def _resolve_duration_s(payload: dict[str, Any]) -> float | None:
    """Extract a duration in seconds from an ffprobe payload.

    Tries ``format.duration`` first (most reliable for muxed files),
    then the first stream's ``duration``. Returns ``None`` when both
    are missing or unparseable so the caller can fail loudly with
    context instead of silently treating it as zero.
    """
    fmt = payload.get("format") or {}
    raw = fmt.get("duration")
    if raw is None:
        for stream in payload.get("streams") or []:
            if stream.get("duration") is not None:
                raw = stream["duration"]
                break
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _video_stream(payload: dict[str, Any]) -> dict[str, Any] | None:
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return None


def _decode_pgm(data: bytes) -> tuple[int, int, bytes]:
    """Parse a binary PGM (P5) header and return ``(w, h, pixels)``.

    ffmpeg writes PGM with format ``P5\\n<w> <h>\\n<maxval>\\n<bytes>``.
    Used by :func:`qa_stills_judge` to decode greyscale frames
    without pulling in PIL or numpy as a hard dependency.
    """
    buf = io.BytesIO(data)
    magic = buf.readline().strip()
    if magic != b"P5":
        raise _ProbeError(f"unexpected PGM magic: {magic!r}")
    line = buf.readline()
    while line.startswith(b"#"):
        line = buf.readline()
    try:
        width_str, height_str = line.strip().split()
        width = int(width_str)
        height = int(height_str)
    except ValueError as exc:
        raise _ProbeError(f"unparseable PGM dimensions: {line!r}") from exc
    maxval_line = buf.readline()
    try:
        maxval = int(maxval_line.strip())
    except ValueError as exc:
        raise _ProbeError(f"unparseable PGM maxval: {maxval_line!r}") from exc
    if maxval != 255:
        raise _ProbeError(f"unsupported PGM maxval (need 255): {maxval}")
    pixels = buf.read(width * height)
    if len(pixels) != width * height:
        raise _ProbeError(
            f"PGM payload length {len(pixels)} != w*h {width * height}"
        )
    return width, height, pixels


def _extract_grey_frames(
    video_path: Path,
    num_samples: int,
    *,
    width: int = 64,
    height: int = 64,
) -> list[bytes]:
    """Decode ``num_samples`` evenly-spaced greyscale frames as raw bytes.

    Uses ffmpeg's ``select`` filter to pluck frames at fixed timestamps
    (avoids the cost of decoding every frame), then ``scale`` +
    ``format=gray`` to compress them to a small fixed-size greyscale
    buffer for cheap pixel-delta math. Reads PGM straight off
    stdout so we don't touch the filesystem twice.
    """
    payload = _ffprobe_json(video_path)
    duration = _resolve_duration_s(payload)
    if duration is None or duration <= 0:
        raise _ProbeError(f"video {video_path} has no usable duration")
    # Sample at the *centre* of each evenly-spaced slot; avoids hitting
    # the very first / last frame where intro fades artificially
    # depress the inter-frame delta.
    samples: list[bytes] = []
    for i in range(num_samples):
        t = duration * (i + 0.5) / num_samples
        cmd = [
            _ffmpeg_path(),
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height},format=gray",
            "-c:v",
            "pgm",
            "-f",
            "image2pipe",
            "-",
        ]
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=15.0,
            )
        except subprocess.CalledProcessError as exc:
            raise _ProbeError(
                f"ffmpeg sample at t={t:.3f}s failed for {video_path}: "
                f"stderr={exc.stderr.decode('utf-8', 'replace')[:200]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise _ProbeError(
                f"ffmpeg sample at t={t:.3f}s timed out for {video_path}"
            ) from exc
        _, _, pixels = _decode_pgm(completed.stdout)
        samples.append(pixels)
    return samples


def _mean_l1_delta(frames: list[bytes]) -> float:
    """Mean per-pixel L1 delta between consecutive frame pairs."""
    if len(frames) < 2:
        return 0.0
    deltas: list[float] = []
    for prev, curr in zip(frames, frames[1:]):
        if len(prev) != len(curr) or not prev:
            continue
        total = 0
        for a, b in zip(prev, curr):
            total += a - b if a > b else b - a
        deltas.append(total / len(curr))
    if not deltas:
        return 0.0
    return sum(deltas) / len(deltas)


def _envelope(
    tool_name: str,
    *,
    scene_id: str,
    verdict: str,
    **fields: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tool": tool_name,
        "scene_id": scene_id,
        "verdict": verdict,
    }
    out.update(fields)
    return out


def _detect_silence_segments(
    audio_path: Path,
    *,
    noise_db: float = SILENCE_NOISE_DB,
    min_duration_s: float = 0.05,
) -> list[tuple[float, float]]:
    """Return ``[(silence_start_s, silence_end_s), ...]`` from ffmpeg's silencedetect.

    Uses ``ffmpeg -af silencedetect`` which writes ``silence_start``
    and ``silence_end`` lines to stderr. We do not need the audio
    output, so the muxer is set to ``null``. Each detected silence
    region must last at least ``min_duration_s`` seconds.

    A region that ends at the very end of the file may have no
    matching ``silence_end`` line — silencedetect only emits one
    when the silence is interrupted. We patch that case in the
    caller by treating "open" silence regions as ending at the
    file duration.
    """
    cmd = [
        _ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={noise_db}dB:duration={min_duration_s}",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except subprocess.CalledProcessError as exc:
        raise _ProbeError(
            f"ffmpeg silencedetect failed for {audio_path}: rc={exc.returncode} "
            f"stderr={exc.stderr.strip()[:200]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _ProbeError(
            f"ffmpeg silencedetect timed out for {audio_path}"
        ) from exc
    starts: list[float] = []
    ends: list[float] = []
    for raw_line in completed.stderr.splitlines():
        line = raw_line.strip()
        if "silence_start:" in line:
            try:
                starts.append(float(line.rsplit("silence_start:", 1)[1].strip()))
            except ValueError:
                continue
        elif "silence_end:" in line:
            tail = line.rsplit("silence_end:", 1)[1].strip()
            try:
                ends.append(float(tail.split()[0]))
            except (ValueError, IndexError):
                continue
    paired: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else float("nan")
        paired.append((start, end))
    return paired


def _measure_tail_rms_db(
    audio_path: Path,
    *,
    duration_s: float,
    tail_window_s: float = 0.05,
) -> float:
    """Measure mean dBFS of the last ``tail_window_s`` seconds of an audio file.

    Uses ffmpeg's ``astats`` filter which prints ``RMS_level`` lines
    to stderr. A healthy narration decays to ``≤ -40 dBFS`` (room
    tone); an abrupt cut leaves speech-band energy (``≥ -25 dBFS``)
    right up against the file end.

    Implementation note: ``astats=metadata=1:reset=1`` resets every
    block; ``metadata=1`` exposes per-channel stats in the per-frame
    log. We trim the input to the tail window with ``-ss`` so astats
    only measures the boundary samples.

    Returns ``-inf`` when astats produced no usable RMS line (e.g.
    file is shorter than the window). Caller treats ``-inf`` as
    pass-by-no-evidence rather than fail, since the gate's
    primary signal is trailing silence.
    """
    if duration_s <= tail_window_s:
        return float("-inf")
    seek = max(0.0, duration_s - tail_window_s)
    cmd = [
        _ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{seek:.6f}",
        "-i",
        str(audio_path),
        "-t",
        f"{tail_window_s:.6f}",
        "-af",
        "astats=metadata=1:reset=0",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except subprocess.CalledProcessError as exc:
        raise _ProbeError(
            f"ffmpeg astats failed for {audio_path}: rc={exc.returncode} "
            f"stderr={exc.stderr.strip()[:200]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _ProbeError(f"ffmpeg astats timed out for {audio_path}") from exc
    rms_values: list[float] = []
    for raw_line in completed.stderr.splitlines():
        line = raw_line.strip()
        # astats with metadata=1 emits "[Parsed_astats_0 @ ...] Overall"
        # blocks plus per-channel "RMS level dB:" lines. We accept
        # both ``RMS level dB:`` and ``RMS_level=`` shapes since the
        # exact label depends on ffmpeg version.
        if "RMS level dB:" in line:
            tail = line.rsplit("RMS level dB:", 1)[1].strip()
        elif "RMS_level=" in line:
            tail = line.rsplit("RMS_level=", 1)[1].strip()
        else:
            continue
        try:
            rms_values.append(float(tail.split()[0]))
        except (ValueError, IndexError):
            continue
    if not rms_values:
        return float("-inf")
    finite = [v for v in rms_values if v != float("-inf") and v == v]
    if not finite:
        return float("-inf")
    return sum(finite) / len(finite)


@tool
def qa_video_artifact_probe(
    scene_id: str,
    video_path: str,
) -> dict[str, Any]:
    """Probe a rendered MP4 with ffprobe and return its key facts.

    Returns ``duration_s``, ``width``, ``height``, ``codec``,
    ``nb_frames`` (when the codec exposes it), and the on-disk size
    in bytes. Verdict is ``pass`` when probe succeeds, ``fail`` on
    any error (file missing, ffprobe failure, no video stream).
    The orchestrator can call this directly or rely on the gates
    that wrap it.
    """
    path = Path(video_path)
    if not path.is_file():
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            error=f"video_path does not exist: {path}",
        )
    size = path.stat().st_size
    if size < MIN_VIDEO_BYTES:
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            size_bytes=size,
            error=f"video too small ({size} < {MIN_VIDEO_BYTES})",
        )
    try:
        payload = _ffprobe_json(path)
    except _ProbeError as exc:
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            size_bytes=size,
            error=str(exc),
        )
    duration_s = _resolve_duration_s(payload)
    stream = _video_stream(payload)
    if stream is None:
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            size_bytes=size,
            duration_s=duration_s,
            error="no video stream in artifact",
        )
    nb_frames_raw = stream.get("nb_frames")
    try:
        nb_frames = int(nb_frames_raw) if nb_frames_raw is not None else None
    except (TypeError, ValueError):
        nb_frames = None
    return _envelope(
        "qa_video_artifact_probe",
        scene_id=scene_id,
        verdict=VERDICT_PASS,
        video_path=str(path),
        size_bytes=size,
        duration_s=duration_s,
        width=stream.get("width"),
        height=stream.get("height"),
        codec=stream.get("codec_name"),
        nb_frames=nb_frames,
    )


@tool
def qa_duration_align(
    scene_id: str,
    audio_path: str,
    video_path: str,
    tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
) -> dict[str, Any]:
    """Hard-fail when audio and video durations diverge.

    AGENTS.md hard invariant §5 ("QA immediately after each
    artifact") requires this gate to run after every
    ``launch_visual_production`` whose paired ``launch_audio_render``
    has already returned. The orchestrator must escalate via the
    ``escalation`` SubAgent on ``verdict == "fail"``.

    Slice 9j-quality-pass shipped frozen frames because no such
    gate existed: a 3.7 s video paired with a 13 s narration tripped
    nothing. With a 0.5 s tolerance this gate would have fired
    ``delta=9.3 s`` and prevented the run from reaching assembly.

    Args:
        scene_id: The scene this artifact belongs to.
        audio_path: Filesystem path to the rendered audio
            (WAV, MP3, etc.; ffprobe figures out the container).
        video_path: Filesystem path to the rendered MP4.
        tolerance_s: Max ``|audio_dur - video_dur|`` before failing.
            Defaults to 0.5 s, the ceiling the operator set in the
            slice 9j post-mortem.
    """
    audio = Path(audio_path)
    video = Path(video_path)
    if not audio.is_file():
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            tolerance_s=float(tolerance_s),
            error=f"audio_path does not exist: {audio}",
        )
    if not video.is_file():
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            tolerance_s=float(tolerance_s),
            error=f"video_path does not exist: {video}",
        )
    try:
        audio_payload = _ffprobe_json(audio)
        video_payload = _ffprobe_json(video)
    except _ProbeError as exc:
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            tolerance_s=float(tolerance_s),
            error=str(exc),
        )
    audio_dur = _resolve_duration_s(audio_payload)
    video_dur = _resolve_duration_s(video_payload)
    if audio_dur is None or video_dur is None:
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            audio_duration_s=audio_dur,
            video_duration_s=video_dur,
            tolerance_s=float(tolerance_s),
            error="ffprobe returned no duration on at least one artifact",
        )
    delta_s = abs(audio_dur - video_dur)
    verdict = VERDICT_PASS if delta_s <= float(tolerance_s) else VERDICT_FAIL
    out = _envelope(
        "qa_duration_align",
        scene_id=scene_id,
        verdict=verdict,
        audio_path=str(audio),
        video_path=str(video),
        audio_duration_s=audio_dur,
        video_duration_s=video_dur,
        delta_s=delta_s,
        tolerance_s=float(tolerance_s),
    )
    if verdict == VERDICT_FAIL:
        out["reason"] = (
            f"|audio_dur - video_dur| = {delta_s:.3f} s > "
            f"tolerance {float(tolerance_s):.3f} s"
        )
    return out


@tool
def qa_stills_judge(
    scene_id: str,
    video_path: str,
    num_samples: int = DEFAULT_STILLS_NUM_SAMPLES,
    min_mean_pixel_delta: float = DEFAULT_MIN_MEAN_PIXEL_DELTA,
) -> dict[str, Any]:
    """Hard-fail when a rendered video has no meaningful motion.

    Decodes ``num_samples`` evenly-spaced greyscale frames and
    computes the mean L1 pixel delta between consecutive samples.
    Below ``min_mean_pixel_delta`` (default 1.5 on a 0-255 scale)
    the clip is judged a still — either LTX-2.3 produced
    near-identical frames, or the muxer froze the last frame and
    padded to longer duration.

    The user explicitly flagged stills as a hard-fail in the slice
    9j post-mortem ("stills are a hard fail for an LLM-based
    check"). This deterministic implementation is the
    credential-free floor; a vision-LLM judge can layer over the
    same envelope without changing the orchestrator contract.

    Args:
        scene_id: The scene this artifact belongs to.
        video_path: Filesystem path to the rendered MP4.
        num_samples: Number of evenly-spaced frames to sample.
            Defaults to 8.
        min_mean_pixel_delta: Floor for the mean inter-frame L1
            delta. Below this the verdict is ``fail``. Defaults
            to 1.5.
    """
    if num_samples < 2:
        raise ValueError("num_samples must be >= 2")
    video = Path(video_path)
    if not video.is_file():
        return _envelope(
            "qa_stills_judge",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(video),
            num_samples=int(num_samples),
            min_mean_pixel_delta=float(min_mean_pixel_delta),
            error=f"video_path does not exist: {video}",
        )
    try:
        frames = _extract_grey_frames(video, int(num_samples))
    except _ProbeError as exc:
        return _envelope(
            "qa_stills_judge",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(video),
            num_samples=int(num_samples),
            min_mean_pixel_delta=float(min_mean_pixel_delta),
            error=str(exc),
        )
    mean_delta = _mean_l1_delta(frames)
    verdict = (
        VERDICT_PASS
        if mean_delta >= float(min_mean_pixel_delta)
        else VERDICT_FAIL
    )
    out = _envelope(
        "qa_stills_judge",
        scene_id=scene_id,
        verdict=verdict,
        video_path=str(video),
        num_samples=int(num_samples),
        min_mean_pixel_delta=float(min_mean_pixel_delta),
        mean_pixel_delta=mean_delta,
    )
    if verdict == VERDICT_FAIL:
        out["reason"] = (
            f"mean inter-frame pixel delta {mean_delta:.3f} below "
            f"floor {float(min_mean_pixel_delta):.3f}"
        )
    return out


@tool
def qa_audio_completeness(
    scene_id: str,
    audio_path: str,
    min_trailing_silence_s: float = MIN_TRAILING_SILENCE_S,
    silence_noise_db: float = SILENCE_NOISE_DB,
    max_tail_rms_db: float = -25.0,
    tail_window_s: float = 0.05,
) -> dict[str, Any]:
    """Hard-fail when narration ends abruptly mid-utterance.

    Slice 9p — the slice 9j-quality-pass run shipped with several
    audio clips that ended mid-word because Qwen3-TTS ran out of
    decoded token budget. The previous gates didn't catch it:
    :func:`qa_duration_align` only looks at total duration,
    :func:`qa_stills_judge` looks at video pixels. Neither
    decodes audio. The user flagged this as an *auditory* signature
    rather than a transcript-content concern, so this gate works on
    waveform energy alone — no transcription, no LLM.

    Two independent waveform tests, both must pass:

    1. **Trailing silence** (``silencedetect``): the file's last
       silence region must extend to (or past) the file end and
       must be at least ``min_trailing_silence_s`` long. A natural
       sentence ending leaves room tone; a hard cut leaves none.
    2. **End-of-file RMS energy** (``astats``): the mean dBFS over
       the last ``tail_window_s`` seconds must be below
       ``max_tail_rms_db``. A natural decay sits at ``≤ -40 dBFS``
       (room tone); spoken energy near the boundary indicates the
       waveform was sliced mid-utterance.

    The orchestrator must escalate via the ``escalation`` SubAgent
    when ``verdict == "fail"`` (AGENTS.md hard invariant §3 / §5
    "fail closed on TTS / QA immediately after each artifact").
    Both tests are deterministic, ffmpeg-only, and CI-hermetic.

    Args:
        scene_id: The scene this artifact belongs to.
        audio_path: Filesystem path to the rendered audio (WAV/MP3).
        min_trailing_silence_s: Minimum trailing silence required
            for a healthy narration. Default 0.15 s.
        silence_noise_db: dBFS floor below which a sample counts
            as silence for ``silencedetect``. Default ``-40`` dB.
        max_tail_rms_db: Maximum mean dBFS allowed in the final
            ``tail_window_s`` seconds. Default ``-25`` dB.
        tail_window_s: Window length for the end-of-file RMS check.
            Default 0.05 s (50 ms).
    """
    audio = Path(audio_path)
    if not audio.is_file():
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            min_trailing_silence_s=float(min_trailing_silence_s),
            silence_noise_db=float(silence_noise_db),
            max_tail_rms_db=float(max_tail_rms_db),
            tail_window_s=float(tail_window_s),
            error=f"audio_path does not exist: {audio}",
        )
    try:
        payload = _ffprobe_json(audio)
    except _ProbeError as exc:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            error=str(exc),
        )
    duration_s = _resolve_duration_s(payload)
    if duration_s is None or duration_s < MIN_NARRATION_DURATION_S:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            audio_duration_s=duration_s,
            error=(
                f"audio too short or unparseable (duration_s={duration_s}, "
                f"floor={MIN_NARRATION_DURATION_S})"
            ),
        )
    try:
        silence_segments = _detect_silence_segments(
            audio,
            noise_db=float(silence_noise_db),
            min_duration_s=0.05,
        )
    except _ProbeError as exc:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            audio_duration_s=duration_s,
            error=str(exc),
        )
    # silencedetect reports the last silence_start that has no
    # matching silence_end as an "open" region (NaN end). We treat
    # such a region as ending at the file boundary — that's the
    # canonical "trailing silence" case.
    trailing_silence_s = 0.0
    if silence_segments:
        last_start, last_end = silence_segments[-1]
        # ``last_end != last_end`` is the NaN check (a NaN never
        # equals itself). When silencedetect emitted no
        # ``silence_end`` for the last region, treat the silence as
        # extending to the file end.
        effective_end = duration_s if last_end != last_end else last_end
        if effective_end >= duration_s - 1e-3:
            trailing_silence_s = max(0.0, effective_end - last_start)
    try:
        tail_rms_db = _measure_tail_rms_db(
            audio,
            duration_s=duration_s,
            tail_window_s=float(tail_window_s),
        )
    except _ProbeError as exc:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            audio_duration_s=duration_s,
            trailing_silence_s=trailing_silence_s,
            error=str(exc),
        )
    silence_pass = trailing_silence_s >= float(min_trailing_silence_s)
    # ``-inf`` means astats produced no measurement (file too short
    # for the window). We don't fail on missing evidence here — the
    # primary signal is trailing silence.
    rms_pass = (
        tail_rms_db == float("-inf") or tail_rms_db <= float(max_tail_rms_db)
    )
    verdict = VERDICT_PASS if silence_pass and rms_pass else VERDICT_FAIL
    out = _envelope(
        "qa_audio_completeness",
        scene_id=scene_id,
        verdict=verdict,
        audio_path=str(audio),
        audio_duration_s=duration_s,
        trailing_silence_s=trailing_silence_s,
        tail_rms_db=tail_rms_db if tail_rms_db != float("-inf") else None,
        min_trailing_silence_s=float(min_trailing_silence_s),
        max_tail_rms_db=float(max_tail_rms_db),
        silence_noise_db=float(silence_noise_db),
        tail_window_s=float(tail_window_s),
    )
    if verdict == VERDICT_FAIL:
        reasons: list[str] = []
        if not silence_pass:
            reasons.append(
                f"trailing silence {trailing_silence_s:.3f} s < "
                f"{float(min_trailing_silence_s):.3f} s "
                "(narration ends without a natural pause — likely cut mid-utterance)"
            )
        if not rms_pass:
            reasons.append(
                f"end-of-file RMS {tail_rms_db:.2f} dBFS > "
                f"{float(max_tail_rms_db):.2f} dBFS "
                "(spoken-band energy at file boundary — likely cut mid-utterance)"
            )
        out["reason"] = "; ".join(reasons)
    return out


__all__ = [
    "DEFAULT_DURATION_TOLERANCE_S",
    "DEFAULT_MIN_MEAN_PIXEL_DELTA",
    "DEFAULT_STILLS_NUM_SAMPLES",
    "MIN_NARRATION_DURATION_S",
    "MIN_TRAILING_SILENCE_S",
    "SILENCE_NOISE_DB",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "qa_audio_completeness",
    "qa_duration_align",
    "qa_stills_judge",
    "qa_video_artifact_probe",
]
