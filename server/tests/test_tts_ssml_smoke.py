"""
Unit tests for the TTS SSML smoke test.

The smoke test is the pipeline's defence against PAG-style
mispronunciations: run one probe per voice, let WhisperX judge whether
SSML was honoured, cache the result in pipeline_state, and fall back to
deterministic literal rewriting when SSML is unsupported.

Run from ``server/``::

    poetry run pytest tests/test_tts_ssml_smoke.py
"""

from __future__ import annotations

import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import tempfile

import pytest  # noqa: E402

from tools.tts_ssml_smoke import (  # noqa: E402
    CacheEntry,
    apply_pronunciation_hints,
    build_probe_ssml,
    is_voice_ssml_supported,
    load_cache,
    save_cache,
    smoke_test_voice,
)


# ---------------------------------------------------------------------------
# apply_pronunciation_hints
# ---------------------------------------------------------------------------


def test_apply_hints_ssml_mode_wraps_say_as():
    out = apply_pronunciation_hints(
        "The PAG is a midbrain nucleus.",
        {"PAG": "P-A-G"},
        ssml_supported=True,
    )
    assert '<say-as interpret-as="characters">PAG</say-as>' in out
    assert "is a midbrain nucleus" in out


def test_apply_hints_literal_mode_rewrites_initialism():
    out = apply_pronunciation_hints(
        "The PAG is a midbrain nucleus.",
        {"PAG": "P-A-G"},
        ssml_supported=False,
    )
    assert "P-A-G" in out
    assert "PAG" not in out.replace("P-A-G", "")  # original form gone


def test_apply_hints_uses_default_letter_spell_when_hint_blank():
    out = apply_pronunciation_hints(
        "The PAG circuit.",
        {"PAG": ""},
        ssml_supported=False,
    )
    assert "P A G" in out


def test_apply_hints_whole_word_only_doesnt_mangle_paging():
    out = apply_pronunciation_hints(
        "The PAG is near the paging area.",
        {"PAG": "P-A-G"},
        ssml_supported=False,
    )
    # "paging" must be untouched; only the standalone PAG should be rewritten.
    assert "paging" in out
    assert "P-A-Ging" not in out


def test_apply_hints_idempotent_in_literal_mode():
    hints = {"PAG": "P-A-G"}
    once = apply_pronunciation_hints("PAG projects widely.", hints, ssml_supported=False)
    twice = apply_pronunciation_hints(once, hints, ssml_supported=False)
    assert once == twice


def test_apply_hints_handles_empty_inputs():
    assert apply_pronunciation_hints("", {"PAG": "P-A-G"}, False) == ""
    assert apply_pronunciation_hints("text", {}, False) == "text"
    assert apply_pronunciation_hints("text", {}, True) == "text"


def test_apply_hints_longer_keys_win_over_shorter():
    # Prevents "AB" rewrite from partially matching inside "ABC".
    out = apply_pronunciation_hints(
        "ABC and AB are different.",
        {"AB": "A B", "ABC": "A B C"},
        ssml_supported=False,
    )
    # "ABC" rewritten to "A B C" first (longer key wins) then the standalone
    # "AB" rewritten to "A B".  Both initialisms are spelled out.
    assert "A B C" in out
    assert out.endswith("A B are different.")


# ---------------------------------------------------------------------------
# cache round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_cache_roundtrip():
    state: dict = {}
    entry = CacheEntry(
        voice_id="qwen-female-en",
        tts_engine_version="qwen3-tts-v1.0",
        ssml_supported=True,
        transcript="p a g",
        tested_at=1234.56,
    )
    save_cache(state, {"qwen-female-en::qwen3-tts-v1.0": entry})
    loaded = load_cache(state)
    assert "qwen-female-en::qwen3-tts-v1.0" in loaded
    round_entry = loaded["qwen-female-en::qwen3-tts-v1.0"]
    assert round_entry.ssml_supported is True
    assert round_entry.transcript == "p a g"


def test_load_cache_from_json_string():
    # pipeline_state persistence may store the cache as a JSON string.
    state = {
        "tts_ssml_support_cache": (
            '{"v1::eng1": {"voice_id": "v1", "tts_engine_version": "eng1", '
            '"ssml_supported": false, "transcript": "pag", "tested_at": 0.0, '
            '"probe_text": "PAG", "expected_letters": "p a g"}}'
        )
    }
    loaded = load_cache(state)
    assert "v1::eng1" in loaded
    assert loaded["v1::eng1"].ssml_supported is False


def test_load_cache_handles_missing_and_malformed():
    assert load_cache({}) == {}
    assert load_cache({"tts_ssml_support_cache": "not json"}) == {}
    assert load_cache({"tts_ssml_support_cache": 42}) == {}


def test_is_voice_ssml_supported_lookup():
    state: dict = {}
    assert is_voice_ssml_supported("v1", "eng1", state) is None
    entry = CacheEntry(
        voice_id="v1", tts_engine_version="eng1", ssml_supported=True, transcript=""
    )
    save_cache(state, {"v1::eng1": entry})
    assert is_voice_ssml_supported("v1", "eng1", state) is True


# ---------------------------------------------------------------------------
# smoke_test_voice
# ---------------------------------------------------------------------------


def _make_tts_stub(response_map: dict[str, str]):
    """Return a TTS callable that writes the given transcript-tag WAV for the voice."""
    def tts(_text_or_ssml: str, voice_id: str, _is_ssml: bool) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        # We don't need a real WAV — WhisperX is stubbed.  Write something.
        with open(path, "wb") as f:
            f.write(b"fake-wav-" + voice_id.encode())
        tts.calls.append((voice_id, path))  # type: ignore[attr-defined]
        tts.last_voice = voice_id  # type: ignore[attr-defined]
        return path

    tts.calls = []  # type: ignore[attr-defined]
    tts.last_voice = None  # type: ignore[attr-defined]
    tts.response_map = response_map  # type: ignore[attr-defined]
    return tts


def test_smoke_test_detects_ssml_supported_voice():
    tts = _make_tts_stub({"qwen-female-en": "p a g"})

    def whisperx(_wav: str) -> str:
        return "p a g"

    state: dict = {}
    entry = smoke_test_voice(
        "qwen-female-en",
        "qwen3-tts-v1.0",
        tts_callable=tts,
        whisperx_callable=whisperx,
        pipeline_state=state,
    )
    assert entry.ssml_supported is True
    assert entry.transcript == "p a g"


def test_smoke_test_detects_ssml_unsupported_voice():
    # PAG-run scenario: voice says "pag" (one word) instead of "p a g".
    tts = _make_tts_stub({"qwen-female-en": "pag"})

    def whisperx(_wav: str) -> str:
        return "pag"  # TTS ignored the SSML

    state: dict = {}
    entry = smoke_test_voice(
        "qwen-female-en",
        "qwen3-tts-v1.0",
        tts_callable=tts,
        whisperx_callable=whisperx,
        pipeline_state=state,
    )
    assert entry.ssml_supported is False
    assert entry.transcript == "pag"


def test_smoke_test_uses_cache_on_second_call():
    tts = _make_tts_stub({"v": "p a g"})
    calls = {"n": 0}

    def whisperx(_wav: str) -> str:
        calls["n"] += 1
        return "p a g"

    state: dict = {}
    first = smoke_test_voice(
        "v", "eng", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state
    )
    second = smoke_test_voice(
        "v", "eng", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state
    )
    assert first.ssml_supported == second.ssml_supported
    assert calls["n"] == 1  # WhisperX only called once — second was cached


def test_smoke_test_force_overrides_cache():
    tts = _make_tts_stub({"v": "ignored"})
    wx_calls = {"n": 0}

    def whisperx(_wav: str) -> str:
        wx_calls["n"] += 1
        return "p a g"

    state: dict = {}
    smoke_test_voice("v", "eng", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state)
    smoke_test_voice(
        "v", "eng", tts_callable=tts, whisperx_callable=whisperx,
        pipeline_state=state, force=True,
    )
    assert wx_calls["n"] == 2


def test_smoke_test_new_engine_version_reprobes():
    tts = _make_tts_stub({"v": "x"})
    wx_calls = {"n": 0}

    def whisperx(_wav: str) -> str:
        wx_calls["n"] += 1
        return "p a g"

    state: dict = {}
    smoke_test_voice("v", "eng-v1", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state)
    smoke_test_voice("v", "eng-v2", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state)
    assert wx_calls["n"] == 2


def test_smoke_test_tts_failure_marks_unsupported():
    def tts(*_args, **_kwargs) -> str:
        raise RuntimeError("TTS worker down")

    def whisperx(_w: str) -> str:
        return "should-not-be-called"

    state: dict = {}
    entry = smoke_test_voice(
        "v", "eng", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state
    )
    # A TTS that can't even run the probe clearly can't be trusted for SSML.
    assert entry.ssml_supported is False


def test_smoke_test_whisperx_failure_marks_unsupported():
    tts = _make_tts_stub({"v": "p a g"})

    def whisperx(_w: str) -> str:
        raise RuntimeError("WhisperX hiccup")

    state: dict = {}
    entry = smoke_test_voice(
        "v", "eng", tts_callable=tts, whisperx_callable=whisperx, pipeline_state=state
    )
    assert entry.ssml_supported is False


def test_smoke_test_requires_voice_id_and_engine():
    tts = _make_tts_stub({})
    wx = lambda _w: ""  # noqa: E731
    state: dict = {}
    with pytest.raises(ValueError):
        smoke_test_voice("", "eng", tts_callable=tts, whisperx_callable=wx, pipeline_state=state)
    with pytest.raises(ValueError):
        smoke_test_voice("v", "", tts_callable=tts, whisperx_callable=wx, pipeline_state=state)


def test_build_probe_ssml_contains_say_as():
    ssml = build_probe_ssml()
    assert "<say-as" in ssml
    assert "PAG" in ssml


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
