"""Real Qwen3-TTS engine wrapper.

Production engine for the Qwen3-TTS worker. Wraps the official
``qwen-tts`` package (https://pypi.org/project/qwen-tts/) and exposes
the :class:`~strands_agents.qwen3_tts_worker.engine.TTSEngine` protocol
that the FastAPI surface expects.

This module is **not** imported in CI — the runner factory falls back
to :class:`StubTTSEngine` when ``qwen_tts`` is not installed (see
:func:`strands_agents.qwen3_tts_worker.runner._real_tts_engine_factory`).
On a Vast.ai VM the ``scripts/qwen3_tts_worker_bootstrap.sh`` script
installs ``torch``, ``qwen-tts``, ``soundfile``, and (best-effort)
``flash-attn`` before launching the worker, so the import succeeds and
this engine takes over.

Voice pinning
-------------

The Qwen3-TTS-CustomVoice checkpoint ships with a fixed roster of nine
named speakers. The worker's ``WORKER_VOICE_ID`` env var pins exactly
one of these speakers per VM (or one of our friendly aliases). Two
voices on the same worker pool is a hard invariant violation — see
``docs/strands-migration/AGENTS.md`` (hard invariant 1).

The engine accepts both the canonical Qwen speaker names (``Ryan``,
``Vivian``, …) and the short aliases the documentary pipeline tends to
use (``alex`` → ``Ryan``, ``default`` → ``Ryan``, …) so callers don't
have to know the model's internal speaker labels.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from typing import Any

import numpy as np

from strands_agents._model_pin import verify_pin

from ._model_pin import QWEN3_TTS_PIN
from .engine import (
    DEFAULT_SAMPLE_RATE_HZ,
    SynthesisRequest,
    SynthesisResult,
    TTSEngineError,
)

logger = logging.getLogger(__name__)


# Production model is pinned via :data:`QWEN3_TTS_PIN`. The model id
# below mirrors that pin and is exposed only for log lines and
# legacy callers; the engine never reads it for the actual load.
DEFAULT_MODEL_ID = QWEN3_TTS_PIN.model_id
ENGINE_ID = "qwen3-tts-12hz-1.7b-customvoice"


# Canonical Qwen3-TTS speakers (CustomVoice checkpoint). Keys are the
# strings the model accepts via ``speaker=...``.
_CANONICAL_SPEAKERS: frozenset[str] = frozenset(
    {
        "Vivian",
        "Serena",
        "Uncle_Fu",
        "Dylan",
        "Eric",
        "Ryan",
        "Aiden",
        "Ono_Anna",
        "Sohee",
    }
)


# Friendly aliases the documentary pipeline tends to use, mapped to the
# canonical speaker. ``default`` and ``alex`` map to ``Ryan`` (English
# male, dynamic) because that's the closest fit for a typical
# economics-documentary narrator. Callers can pass any canonical
# speaker directly and skip the alias map entirely.
_VOICE_ALIASES: dict[str, str] = {
    "default": "Ryan",
    "alex": "Ryan",
    "narrator": "Ryan",
    "male_english": "Ryan",
    "male_english_warm": "Aiden",
    "female_chinese": "Vivian",
    "female_chinese_warm": "Serena",
    "male_chinese": "Uncle_Fu",
    "female_japanese": "Ono_Anna",
    "female_korean": "Sohee",
}


# BCP-47 language tag → Qwen3-TTS language string. Qwen accepts the
# English-name forms (``"English"``, ``"Chinese"``, …). Unknown tags
# fall back to ``"Auto"`` which lets the model auto-detect.
_LANGUAGE_MAP: dict[str, str] = {
    "en": "English",
    "en-US": "English",
    "en-GB": "English",
    "zh": "Chinese",
    "zh-CN": "Chinese",
    "zh-TW": "Chinese",
    "ja": "Japanese",
    "ja-JP": "Japanese",
    "ko": "Korean",
    "ko-KR": "Korean",
    "de": "German",
    "de-DE": "German",
    "fr": "French",
    "fr-FR": "French",
    "ru": "Russian",
    "ru-RU": "Russian",
    "pt": "Portuguese",
    "pt-PT": "Portuguese",
    "pt-BR": "Portuguese",
    "es": "Spanish",
    "es-ES": "Spanish",
    "es-MX": "Spanish",
    "it": "Italian",
    "it-IT": "Italian",
}


def _resolve_speaker(voice_id: str) -> str:
    """Map ``voice_id`` to a canonical Qwen3-TTS speaker name.

    Accepts canonical names (``Ryan``) directly, lowercase aliases
    (``narrator``), or names that differ only in case (``ryan``).
    Raises :class:`TTSEngineError` if no match.
    """
    if voice_id in _CANONICAL_SPEAKERS:
        return voice_id
    lowered = voice_id.lower()
    if lowered in _VOICE_ALIASES:
        return _VOICE_ALIASES[lowered]
    for canonical in _CANONICAL_SPEAKERS:
        if canonical.lower() == lowered:
            return canonical
    raise TTSEngineError(
        f"unknown voice_id={voice_id!r}; expected one of "
        f"{sorted(_CANONICAL_SPEAKERS)} or alias "
        f"{sorted(_VOICE_ALIASES)}"
    )


def _resolve_language(language: str) -> str:
    """Map a BCP-47 tag to the Qwen3-TTS language string.

    Falls back to ``"Auto"`` for unknown tags so the model can
    auto-detect. We never raise here — language hints are advisory.
    """
    if language in _LANGUAGE_MAP:
        return _LANGUAGE_MAP[language]
    primary = language.split("-")[0].lower()
    return _LANGUAGE_MAP.get(primary, "Auto")


def _np_to_wav_bytes(samples: np.ndarray, sample_rate_hz: int) -> bytes:
    """Encode a numpy waveform as 16-bit mono PCM WAV bytes.

    Qwen3-TTS returns floats in roughly ``[-1, 1]``. We convert to
    int16, clamp on overflow, and write a standard PCM WAV via
    :mod:`wave`. Multi-channel waveforms are downmixed to mono by
    averaging.
    """
    if samples.ndim > 1:
        samples = samples.mean(axis=tuple(range(samples.ndim - 1)))
    samples = np.asarray(samples, dtype=np.float32)
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate_hz))
        wav.writeframes(int16.tobytes())
    return buf.getvalue()


class Qwen3TTSEngine:
    """Production engine wrapping ``qwen_tts.Qwen3TTSModel``.

    The model is loaded lazily on the first :meth:`synthesize` call so
    importing this module on a CPU-only box (e.g., for a worker
    bootstrap dry-run) doesn't trigger a CUDA init or weight download.

    The model identity (``model_id`` and HF revision) is **not**
    configurable — it is locked by :data:`QWEN3_TTS_PIN`. Only
    operational knobs are read from the environment:

    * ``QWEN3_TTS_DEVICE_MAP`` — ``device_map`` arg for
      ``from_pretrained``. Defaults to ``cuda:0``.
    * ``QWEN3_TTS_DTYPE`` — ``"bfloat16"`` (default) or ``"float16"``.
    * ``QWEN3_TTS_ATTN_IMPL`` — ``flash_attention_2`` (default) or
      ``eager`` to skip flash-attn. The bootstrap installs flash-attn
      best-effort; we fall back to ``eager`` automatically if the
      flash-attn import fails.

    The constructor still accepts ``model_id`` for legacy callers,
    but if it differs from :data:`QWEN3_TTS_PIN.model_id` the engine
    raises :class:`TTSEngineError` immediately. There is no env-var
    override for the protected fields. See
    :mod:`strands_agents._model_pin` for the rationale.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        device_map: str | None = None,
        dtype_name: str | None = None,
        attn_implementation: str | None = None,
    ) -> None:
        if model_id is not None and model_id != QWEN3_TTS_PIN.model_id:
            raise TTSEngineError(
                f"model_id override is forbidden: requested={model_id!r}, "
                f"pinned={QWEN3_TTS_PIN.model_id!r} (see strands_agents._model_pin)"
            )
        self._model_id = QWEN3_TTS_PIN.model_id
        self._device_map = device_map or os.environ.get(
            "QWEN3_TTS_DEVICE_MAP", "cuda:0"
        )
        self._dtype_name = (
            dtype_name or os.environ.get("QWEN3_TTS_DTYPE", "bfloat16")
        ).lower()
        self._attn_implementation = attn_implementation or os.environ.get(
            "QWEN3_TTS_ATTN_IMPL", "flash_attention_2"
        )
        self._model: Any | None = None
        self._sample_rate: int = DEFAULT_SAMPLE_RATE_HZ

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    def _load_model(self) -> Any:
        """Load the Qwen3TTSModel once, cache it for subsequent calls."""
        if self._model is not None:
            return self._model

        # Local imports — torch + qwen_tts pull in heavy CUDA / CUDA
        # extension code we don't want in CI.
        import torch  # noqa: PLC0415
        from qwen_tts import Qwen3TTSModel  # noqa: PLC0415

        dtype_lookup = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        if self._dtype_name not in dtype_lookup:
            raise TTSEngineError(
                f"unsupported QWEN3_TTS_DTYPE={self._dtype_name!r}; "
                f"expected one of {sorted(dtype_lookup)}"
            )
        dtype = dtype_lookup[self._dtype_name]

        attn_impl = self._attn_implementation
        if attn_impl == "flash_attention_2":
            try:
                import flash_attn  # noqa: PLC0415, F401
            except ImportError:
                logger.warning(
                    "flash_attn import failed; falling back to attn_implementation=<eager>",
                )
                attn_impl = "eager"

        # Verify the pinned model bytes BEFORE loading the model.
        # ``verify_pin`` materializes the locked HF revision into the
        # local cache and SHA256s every required file. On mismatch it
        # raises ``ModelPinMismatchError`` and the worker startup
        # bails out before the TTS model can synthesize anything.
        snapshot_dir = verify_pin(QWEN3_TTS_PIN)

        logger.info(
            "model_id=<%s>, revision=<%s>, snapshot_dir=<%s>, "
            "device_map=<%s>, dtype=<%s>, attn=<%s> | loading qwen3-tts model",
            QWEN3_TTS_PIN.model_id,
            QWEN3_TTS_PIN.revision,
            snapshot_dir,
            self._device_map,
            self._dtype_name,
            attn_impl,
        )
        model = Qwen3TTSModel.from_pretrained(
            str(snapshot_dir),
            device_map=self._device_map,
            dtype=dtype,
            attn_implementation=attn_impl,
        )
        # qwen_tts exposes the canonical sample rate via the model's
        # tokenizer. Default to 24 kHz if it's not surfaced (matches
        # the Qwen3-TTS-Tokenizer-12Hz spec, which decodes at 24 kHz).
        sr_attr = getattr(model, "sample_rate", None)
        if isinstance(sr_attr, int) and sr_attr > 0:
            self._sample_rate = sr_attr
        self._model = model
        return model

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize one utterance with the real Qwen3-TTS model."""
        if not request.text.strip():
            raise TTSEngineError("text must be non-empty")
        if not request.voice_id:
            raise TTSEngineError("voice_id must be non-empty")

        speaker = _resolve_speaker(request.voice_id)
        language = _resolve_language(request.language)

        model = self._load_model()

        generate_kwargs: dict[str, Any] = {
            "text": request.text,
            "language": language,
            "speaker": speaker,
        }
        if request.style:
            generate_kwargs["instruct"] = request.style

        try:
            wavs, sr = model.generate_custom_voice(**generate_kwargs)
        except Exception as exc:
            raise TTSEngineError(
                f"qwen3-tts generation failed: {exc}"
            ) from exc

        if not isinstance(wavs, (list, tuple)) or len(wavs) == 0:
            raise TTSEngineError(
                "qwen3-tts returned empty waveform list"
            )
        wav = np.asarray(wavs[0])
        sample_rate = int(sr) if sr else self._sample_rate
        wav_bytes = _np_to_wav_bytes(wav, sample_rate)
        # ``len(wav)`` may be a tensor in some qwen-tts builds; coerce
        # to int via numpy for a stable scalar.
        num_samples = int(np.asarray(wav).shape[-1])
        duration_s = float(num_samples) / float(sample_rate)

        logger.info(
            "voice_id=<%s>, speaker=<%s>, language=<%s>, chars=<%d>, "
            "duration_s=<%.3f>, bytes=<%d> | qwen3-tts synth ok",
            request.voice_id,
            speaker,
            language,
            len(request.text),
            duration_s,
            len(wav_bytes),
        )
        return SynthesisResult(
            wav_bytes=wav_bytes,
            duration_s=duration_s,
            sample_rate_hz=sample_rate,
            voice_id=request.voice_id,
            engine=self.engine_id,
        )
