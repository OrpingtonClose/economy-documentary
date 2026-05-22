"""
One-time-per-voice TTS SSML smoke test with WhisperX verification.

Motivation (PAG run):
    The scenario included the initialism "PAG".  The TTS engine pronounced
    it as a single word ("pag" rhyming with "bag") rather than spelling it
    out letter-by-letter.  The root cause: Qwen3-TTS does not uniformly
    honour SSML ``<say-as interpret-as='characters'>`` across every voice.

Rather than trust SSML blindly, this module runs a **per-voice smoke test**
exactly once per (voice_id, tts_engine_version) pair, caches the result in
``pipeline_state.json``, and falls back to a deterministic literal rewrite
(``PAG`` → ``P A G``) for voices that don't honour SSML.

Public API:
    ``smoke_test_voice(voice_id, tts_engine_version, *, tts_callable,
    whisperx_callable, pipeline_state)`` → CacheEntry

    ``apply_pronunciation_hints(text, hints, ssml_supported)`` → str
        For SSML-unsupported voices, rewrites "PAG" → "P A G" (etc.)
        deterministically before sending to TTS.  For SSML-supported voices,
        wraps hinted tokens with ``<say-as interpret-as='characters'>...``.

The TTS and WhisperX calls are injected as callables so this module stays
importable and unit-testable without any GPU worker configuration.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A TTS callable takes (text_or_ssml: str, voice_id: str, is_ssml: bool) and
# returns the path to a generated WAV file.  Smoke tests are short so
# timeouts are not a concern for callers.
TTSCallable = Callable[[str, str, bool], str]

# A WhisperX callable takes a WAV path and returns the transcript (lowercase).
WhisperXCallable = Callable[[str], str]


@dataclass
class CacheEntry:
    """Per-voice SSML support record, cached in pipeline_state."""

    voice_id: str
    tts_engine_version: str
    ssml_supported: bool
    transcript: str
    tested_at: float = 0.0
    # How the smoke test prompted for the initialism.  "PAG" pronounced
    # letter-by-letter looks like "p a g" in WhisperX output.
    probe_text: str = "PAG"
    expected_letters: str = "p a g"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CacheEntry":
        return cls(
            voice_id=str(raw.get("voice_id", "")),
            tts_engine_version=str(raw.get("tts_engine_version", "")),
            ssml_supported=bool(raw.get("ssml_supported", False)),
            transcript=str(raw.get("transcript", "")),
            tested_at=float(raw.get("tested_at", 0.0)),
            probe_text=str(raw.get("probe_text", "PAG")),
            expected_letters=str(raw.get("expected_letters", "p a g")),
        )


# Pipeline_state key for the cache dict. Keyed by "<voice_id>::<engine_ver>".
_CACHE_KEY = "tts_ssml_support_cache"


def _cache_key(voice_id: str, tts_engine_version: str) -> str:
    return f"{voice_id}::{tts_engine_version}"


# ---------------------------------------------------------------------------
# SSML helpers
# ---------------------------------------------------------------------------

# The canonical SSML probe: one token, one <say-as> wrap.  If the voice
# pronounces this letter-by-letter, SSML is supported.  If it says "pag" as
# a word, SSML is NOT honoured and we must rewrite literally.
_PROBE_TOKEN = "PAG"
_PROBE_EXPECTED = "p a g"


def _letter_spell(token: str) -> str:
    """Rewrite an all-caps token as space-separated letters.

    "PAG" -> "P A G", "DBS" -> "D B S".  We keep the case so downstream
    logging is readable; TTS typically lowercases internally anyway.
    """
    return " ".join(list(token))


def _transcript_matches_letters(transcript: str, token: str) -> bool:
    """Return True iff the transcript spells the token letter-by-letter.

    We look for the letters separated by whitespace OR common transcript
    separators (".-") in the expected order, case-insensitively.
    """
    if not transcript:
        return False
    letters = list(token.lower())
    if not letters:
        return False
    # Build a pattern like "p[\\s.,-]+a[\\s.,-]+g" and check for a match.
    pattern = r"[\s.,\-]+".join(re.escape(ch) for ch in letters)
    return re.search(pattern, transcript.lower()) is not None


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pronunciation hint application
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _now() -> float:
    import time
    return time.time()


__all__ = [
    "CacheEntry",
    "apply_pronunciation_hints",
    "build_probe_ssml",
    "is_voice_ssml_supported",
    "load_cache",
    "save_cache",
    "smoke_test_voice",
]
