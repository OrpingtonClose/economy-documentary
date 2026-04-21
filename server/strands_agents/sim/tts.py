"""Deterministic TTS + WhisperX alignment fake.

The audio tool (component 04) takes four helpers — ``tts_generate``,
``whisperx_align``, ``loudness_normalize``, ``b2_upload`` — and the
pipeline calls them in order for every narration block. :class:`FakeTTS`
implements the first three; the fourth is served by :class:`FakeB2`
and wired in by :class:`~strands_agents.sim.substrate.Substrate`.

Faithful shape, not faithful sound
----------------------------------

Real TTS returns a WAV containing speech. The fake returns a WAV
containing silence of the requested duration. Downstream code cares
about two things: how long the file is and what words align to what
timestamps. Both are produced directly from the input text.

Scripting the duration
----------------------

The default word-rate is 2.5 words/second, matching the AGENTS.md
heuristic. Tests that need to force a timing failure call
:meth:`set_next_duration` to override one specific narration block:

.. code-block:: python

    fake_tts.set_next_duration(
        scene_num=3, voice_role="V1", language="en",
        duration=9.9,  # scene target is 6.0 — timing loop must fire
    )
"""

from __future__ import annotations

import os
import struct
import tempfile
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from strands_agents.sim.recorder import CallRecord, Recorder

# Silence sample — 16-bit PCM mono @ 22.05 kHz is small and decodes cleanly
# in every downstream reader. Matches the sample rate typical of the real
# TTS output without being so high it bloats the in-memory fake store.
_SAMPLE_RATE = 22050
_SAMPLE_WIDTH = 2  # bytes (16-bit)
_CHANNELS = 1
_WORDS_PER_SECOND = 2.5


@dataclass(frozen=True)
class _BlockKey:
    scene_num: int
    voice_role: str
    language: str


class FakeTTS:
    """TTS + alignment fake that writes real (silent) WAV files."""

    def __init__(
        self,
        *,
        recorder: Recorder | None = None,
        tmpdir: str | None = None,
    ) -> None:
        """Create a fake TTS.

        Args:
            recorder: Optional :class:`Recorder`.
            tmpdir: Directory to write WAV files into. Defaults to a
                fresh ``tempfile.mkdtemp()``-owned directory. Tests
                that care about cleanup should pass their own.
        """
        self._lock = threading.Lock()
        self._recorder = recorder
        self._tmpdir = tmpdir or tempfile.mkdtemp(prefix="fake-tts-")
        self._duration_overrides: dict[_BlockKey, float] = {}
        self._post_align_hooks: list[Callable[[dict[str, Any]], None]] = []
        self._counter = 0

    # ------------------------------------------------------------------
    # Scripting controls
    # ------------------------------------------------------------------

    def set_next_duration(
        self, *, scene_num: int, voice_role: str, language: str, duration: float
    ) -> None:
        """Force the next matching render to produce ``duration`` seconds.

        The override is consumed by the first matching call. Later
        calls for the same key fall back to the default word-rate
        computation — so a test can inject a single failure without
        affecting neighbouring scenes.
        """
        if duration <= 0:
            msg = f"duration must be positive, got {duration}"
            raise ValueError(msg)
        key = _BlockKey(scene_num, voice_role, language)
        with self._lock:
            self._duration_overrides[key] = duration

    # ------------------------------------------------------------------
    # Helper surfaces — these are what the audio tool expects.
    # ------------------------------------------------------------------

    def tts_generate(
        self, scene_num: int, voice_role: str, text: str, language: str
    ) -> str:
        """Write a silent WAV for the block and return its path."""
        key = _BlockKey(scene_num, voice_role, language)
        with self._lock:
            duration = self._duration_overrides.pop(key, None)
            self._counter += 1
            seq = self._counter
        if duration is None:
            duration = max(
                len(text.split()) / _WORDS_PER_SECOND, 0.5
            )  # 500ms floor keeps WhisperX happy
        wav_path = os.path.join(
            self._tmpdir,
            f"scene{scene_num}_{voice_role}_{language}_{seq:04d}.wav",
        )
        _write_silent_wav(wav_path, duration_sec=duration)
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="tts",
                    op="tts_generate",
                    kwargs={
                        "scene_num": scene_num,
                        "voice_role": voice_role,
                        "language": language,
                        "text_len": len(text),
                    },
                    result_summary=f"path={wav_path} dur={duration:.3f}",
                )
            )
        return wav_path

    def whisperx_align(
        self, wav_path: str, text: str, language: str  # noqa: ARG002 — matches protocol
    ) -> dict[str, Any]:
        """Return deterministic per-word timings derived from the WAV length."""
        total_duration = _wav_duration(wav_path)
        words = text.split()
        timings = _even_word_timings(words, total_duration)
        segment = {
            "total_duration": total_duration,
            "word_count": len(words),
            "words": timings,
        }
        for hook in list(self._post_align_hooks):
            hook(segment)
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="tts",
                    op="whisperx_align",
                    kwargs={"wav_path": wav_path, "text_len": len(text)},
                    result_summary=(
                        f"total_duration={total_duration:.3f} word_count={len(words)}"
                    ),
                )
            )
        return segment

    def loudness_normalize(self, wav_path: str, target_lufs: float) -> None:
        """No-op normaliser (our WAVs are silent, LUFS doesn't apply).

        Records the call so trajectory tests can assert it fires.
        """
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="tts",
                    op="loudness_normalize",
                    kwargs={"wav_path": wav_path, "target_lufs": target_lufs},
                    result_summary="noop",
                )
            )

    # ------------------------------------------------------------------
    # Extensibility — tests can register a hook to mutate the alignment
    # after it's computed (e.g. to inject a word-level misalignment that
    # the content analyst must detect).
    # ------------------------------------------------------------------

    def add_post_align_hook(
        self, hook: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register ``hook`` to run on every alignment dict before return."""
        self._post_align_hooks.append(hook)


# ---------------------------------------------------------------------------
# WAV helpers — kept at module scope so other fakes (e.g. FakeRenderer if it
# ever needs audio input) can reuse them.
# ---------------------------------------------------------------------------


def _write_silent_wav(path: str, *, duration_sec: float) -> None:
    total_samples = max(int(duration_sec * _SAMPLE_RATE), 1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(_CHANNELS)
        wf.setsampwidth(_SAMPLE_WIDTH)
        wf.setframerate(_SAMPLE_RATE)
        silent_frame = struct.pack("<h", 0)
        wf.writeframes(silent_frame * total_samples)


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    return frames / float(rate) if rate else 0.0


def _even_word_timings(
    words: list[str], total_duration: float
) -> list[dict[str, Any]]:
    if not words:
        return []
    per_word = total_duration / len(words)
    return [
        {
            "word": w,
            "start": round(idx * per_word, 3),
            "end": round((idx + 1) * per_word, 3),
        }
        for idx, w in enumerate(words)
    ]
