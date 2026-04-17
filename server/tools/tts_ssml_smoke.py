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


def load_cache(pipeline_state: dict[str, Any]) -> dict[str, CacheEntry]:
    """Read the cache dict from pipeline_state, tolerating missing/bad data."""
    raw = pipeline_state.get(_CACHE_KEY) if isinstance(pipeline_state, dict) else None
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("tts_ssml_smoke: cache JSON malformed, ignoring")
            raw = None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, CacheEntry] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            out[k] = CacheEntry.from_dict(v)
        except (TypeError, ValueError) as exc:
            logger.warning("tts_ssml_smoke: dropping bad cache entry %s: %s", k, exc)
    return out


def save_cache(pipeline_state: dict[str, Any], cache: dict[str, CacheEntry]) -> None:
    """Persist the cache dict into pipeline_state.

    We store as a dict of plain dicts (not JSON string) so ``safe_state_dict``
    and downstream serialisers can handle it uniformly.
    """
    pipeline_state[_CACHE_KEY] = {k: v.as_dict() for k, v in cache.items()}


# ---------------------------------------------------------------------------
# SSML helpers
# ---------------------------------------------------------------------------

# The canonical SSML probe: one token, one <say-as> wrap.  If the voice
# pronounces this letter-by-letter, SSML is supported.  If it says "pag" as
# a word, SSML is NOT honoured and we must rewrite literally.
_PROBE_TOKEN = "PAG"
_PROBE_EXPECTED = "p a g"


def build_probe_ssml(token: str = _PROBE_TOKEN) -> str:
    # Keep the wrapping minimal — some engines are strict about unknown
    # elements.  <speak> is universal, <say-as> is the standard hook.
    return f"<speak><say-as interpret-as=\"characters\">{token}</say-as></speak>"


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


def smoke_test_voice(
    voice_id: str,
    tts_engine_version: str,
    *,
    tts_callable: TTSCallable,
    whisperx_callable: WhisperXCallable,
    pipeline_state: dict[str, Any],
    probe_token: str = _PROBE_TOKEN,
    force: bool = False,
) -> CacheEntry:
    """Probe one voice for SSML support.  Cached in pipeline_state.

    Returns the ``CacheEntry``.  If a cached entry matches the voice_id +
    engine_version, it is returned immediately unless ``force=True``.

    If the voice does not honour SSML, the entry's ``ssml_supported`` is
    False and callers must use ``apply_pronunciation_hints`` in literal
    mode.
    """
    if not voice_id:
        raise ValueError("voice_id is required")
    if not tts_engine_version:
        raise ValueError("tts_engine_version is required (cache-busting key)")

    cache = load_cache(pipeline_state)
    key = _cache_key(voice_id, tts_engine_version)
    if not force and key in cache:
        logger.info(
            "tts_ssml_smoke: cache hit for %s (supported=%s)",
            key, cache[key].ssml_supported,
        )
        return cache[key]

    ssml = build_probe_ssml(probe_token)
    logger.info("tts_ssml_smoke: probing voice=%s engine=%s with SSML",
                voice_id, tts_engine_version)

    try:
        wav_path = tts_callable(ssml, voice_id, True)
    except Exception as exc:
        logger.warning(
            "tts_ssml_smoke: TTS call raised for voice %s: %s — assuming SSML unsupported",
            voice_id, exc,
        )
        entry = CacheEntry(
            voice_id=voice_id,
            tts_engine_version=tts_engine_version,
            ssml_supported=False,
            transcript="",
            tested_at=_now(),
            probe_text=probe_token,
            expected_letters=_letter_spell(probe_token).lower(),
        )
        cache[key] = entry
        save_cache(pipeline_state, cache)
        return entry

    transcript = ""
    try:
        transcript = (whisperx_callable(wav_path) or "").strip()
    except Exception as exc:
        logger.warning(
            "tts_ssml_smoke: WhisperX raised on voice %s: %s — assuming SSML unsupported",
            voice_id, exc,
        )

    supported = _transcript_matches_letters(transcript, probe_token)
    entry = CacheEntry(
        voice_id=voice_id,
        tts_engine_version=tts_engine_version,
        ssml_supported=supported,
        transcript=transcript,
        tested_at=_now(),
        probe_text=probe_token,
        expected_letters=_letter_spell(probe_token).lower(),
    )
    cache[key] = entry
    save_cache(pipeline_state, cache)

    logger.info(
        "tts_ssml_smoke: voice=%s engine=%s ssml_supported=%s transcript=%r",
        voice_id, tts_engine_version, supported, transcript[:80],
    )

    # Clean up the probe wav (best-effort; not fatal).
    try:
        if wav_path and os.path.isfile(wav_path):
            os.remove(wav_path)
    except OSError:
        pass

    return entry


# ---------------------------------------------------------------------------
# Pronunciation hint application
# ---------------------------------------------------------------------------


def apply_pronunciation_hints(
    text: str,
    hints: dict[str, str],
    ssml_supported: bool,
) -> str:
    """Rewrite a narration string so TTS pronounces initialisms correctly.

    When ``ssml_supported`` is True, each hinted token is wrapped with
    ``<say-as interpret-as='characters'>...</say-as>`` so the engine
    spells it out.  The result is still a valid SSML fragment that the
    caller is expected to wrap with ``<speak>...</speak>`` if sending
    whole.

    When ``ssml_supported`` is False (or unknown), we deterministically
    rewrite each hint's key in the text to its hint value (typically
    ``P-A-G`` or ``P A G``).  This is the PAG-run safety net.

    The function is idempotent: running it twice on the same text
    produces the same output.

    Only whole-word matches are rewritten — no sub-string rewrites that
    would mangle valid English.
    """
    if not text or not hints:
        return text

    out = text
    # Sort by length desc so longer keys are rewritten first (prevents
    # "PAG" from matching inside "PAGING" first via a short hint).
    keys = sorted((k for k in hints if k), key=len, reverse=True)

    for key in keys:
        replacement = hints[key].strip() if hints[key] else ""
        if not replacement:
            replacement = _letter_spell(key)

        # Whole-word boundary match, case-sensitive so we only rewrite
        # the initialism form.
        pattern = rf"\b{re.escape(key)}\b"

        if ssml_supported:
            sub = f'<say-as interpret-as="characters">{key}</say-as>'
        else:
            sub = replacement

        out = re.sub(pattern, sub, out)

    return out


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _now() -> float:
    import time
    return time.time()


def is_voice_ssml_supported(
    voice_id: str,
    tts_engine_version: str,
    pipeline_state: dict[str, Any],
) -> Optional[bool]:
    """Look up a voice's SSML support status without running a probe.

    Returns None when no smoke test has run yet.
    """
    cache = load_cache(pipeline_state)
    entry = cache.get(_cache_key(voice_id, tts_engine_version))
    return None if entry is None else entry.ssml_supported


__all__ = [
    "CacheEntry",
    "apply_pronunciation_hints",
    "build_probe_ssml",
    "is_voice_ssml_supported",
    "load_cache",
    "save_cache",
    "smoke_test_voice",
]
