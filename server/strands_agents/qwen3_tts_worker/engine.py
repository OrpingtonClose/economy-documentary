"""TTS engine abstraction.

The :class:`TTSEngine` protocol is what the FastAPI surface calls. The
production implementation wraps Qwen3-TTS and loads weights lazily
inside the Vast.ai VM's CUDA runtime — see
``scripts/qwen3_tts_worker_bootstrap.sh`` for the weight-pull step.

Unit tests use :class:`StubTTSEngine`, a deterministic in-memory
engine that emits a silent 16-bit PCM WAV of the requested duration so
the FastAPI, registry, and bump-middleware layers can be covered
without any GPU or model weights in CI.
"""

from __future__ import annotations

import io
import logging
import struct
import time
import wave
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


DEFAULT_SAMPLE_RATE_HZ = 24_000
DEFAULT_CHARS_PER_SECOND = 15.0


class TTSEngineError(Exception):
    """Raised when synthesis fails in a way the worker must surface."""


@dataclass(frozen=True)
class SynthesisRequest:
    """Inputs to a single ``/tts/render`` call.

    Attributes:
        text: The narration string to synthesize. Must be non-empty.
        voice_id: The voice pinned to this VM. Must match the VM's
            pinned voice — the worker rejects mismatches.
        language: BCP-47 tag (e.g. ``en``, ``en-US``). The engine may
            ignore the region subtag depending on model support.
        style: Optional style hint (e.g. ``neutral``, ``warm``,
            ``documentary``). Interpretation is engine-specific.
        seed: Optional deterministic seed. Engines that don't support
            seeding ignore this silently.
    """

    text: str
    voice_id: str
    language: str = "en"
    style: str | None = None
    seed: int | None = None


@dataclass(frozen=True)
class SynthesisResult:
    """Output of a successful synthesis.

    Attributes:
        wav_bytes: Raw WAV payload, 16-bit PCM, mono.
        duration_s: Audio duration in seconds.
        sample_rate_hz: Sample rate of the rendered audio.
        voice_id: The voice that rendered the audio (should equal
            :attr:`SynthesisRequest.voice_id`).
        engine: Human-readable engine identifier (e.g.
            ``qwen3-tts-1.7b`` or ``stub``).
    """

    wav_bytes: bytes
    duration_s: float
    sample_rate_hz: int
    voice_id: str
    engine: str


class TTSEngine(Protocol):
    """Minimal interface every TTS backend implements."""

    @property
    def engine_id(self) -> str:
        """Short identifier, e.g. ``qwen3-tts-1.7b`` or ``stub``."""
        ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize one utterance, blocking until complete."""
        ...


@dataclass
class StubTTSEngine:
    """Deterministic in-memory engine used by unit tests.

    Emits a silent 16-bit mono WAV whose duration is
    ``len(text) / chars_per_second``, rounded up. Voice and language
    are echoed back unchanged. Good enough for middleware and routing
    tests that should not depend on a real model.

    Attributes:
        sample_rate_hz: Sample rate for the stub WAVs.
        chars_per_second: Approximate speaking rate used to compute the
            synthetic duration from the input text.
        simulated_latency_s: Optional sleep before returning, so a
            bump-on-request test can observe the bump during synthesis.
    """

    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    chars_per_second: float = DEFAULT_CHARS_PER_SECOND
    simulated_latency_s: float = 0.0

    @property
    def engine_id(self) -> str:
        return "stub"

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if not request.text.strip():
            raise TTSEngineError("text must be non-empty")
        if not request.voice_id:
            raise TTSEngineError("voice_id must be non-empty")

        if self.simulated_latency_s > 0:
            time.sleep(self.simulated_latency_s)

        duration_s = max(len(request.text) / self.chars_per_second, 0.1)
        num_samples = int(duration_s * self.sample_rate_hz)
        silence = struct.pack("<h", 0) * num_samples

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate_hz)
            wav.writeframes(silence)
        wav_bytes = buffer.getvalue()

        logger.debug(
            "voice_id=<%s>, chars=<%d>, duration_s=<%.3f>, bytes=<%d> | stub synth ok",
            request.voice_id,
            len(request.text),
            duration_s,
            len(wav_bytes),
        )
        return SynthesisResult(
            wav_bytes=wav_bytes,
            duration_s=duration_s,
            sample_rate_hz=self.sample_rate_hz,
            voice_id=request.voice_id,
            engine=self.engine_id,
        )
