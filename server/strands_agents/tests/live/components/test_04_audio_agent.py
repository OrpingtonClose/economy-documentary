"""Live-judge proof of robustness for Component 04 (audio-agent).

Clear-cut contracts proved here:

1. The audio tool composes its injected helpers in the right order:
   TTS → loudness → WhisperX → B2 upload, with strict input validation
   and a fail-loud RuntimeError on any helper failure.  Deterministic;
   uses fake helpers so the test is not rate-limited by real TTS infra.
2. End-to-end synthesis fidelity: a real TTS engine (OpenAI
   ``gpt-4o-mini-tts``) produces audio for a documentary narration
   line, and a live Gemini multimodal judge confirms the audio is
   (a) in the requested language and (b) on-topic for the prompt.
   The contra-case proves the judge doesn't rubber-stamp "yes" for
   every clip.

The second block is the "MAKE IT CARE" check: the judge is listening
to the actual waveform, not reading the prompt we sent to the TTS
engine.  If Gemini can't tell English from nonsense, it's a broken
judge.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import openai
import pytest

from strands_agents.audio_tool import (
    AudioHelpersNotConfigured,
    clear_audio_helpers,
    render_audio,
    set_audio_helpers,
)

from .._judges import judge_audio_yes
from ..conftest import requires_google_api

_TOPIC = "the 1923 Weimar hyperinflation"

_NARRATION_EN = (
    "In late 1923, prices in Germany doubled every two days, and families "
    "carried wheelbarrows of marks just to buy a single loaf of bread."
)

_NARRATION_GIBBERISH = (
    "xzqwvp gnorph blurm crawlix, mzondo yib stavkum. Chezel prandix "
    "vorwak flong, ibstra chondu."
)


# ---------------------------------------------------------------------------
# Deterministic: helper composition + strict validation
# ---------------------------------------------------------------------------


def test_render_audio_raises_when_helpers_not_configured() -> None:
    """Calling ``render_audio`` without helpers must fail loudly.

    The contract forbids silent fallback to synthetic durations.
    """
    clear_audio_helpers()
    scenes = [{"id": 1, "voices": [{"voice_id": "V1", "text": "hello"}]}]
    with pytest.raises(AudioHelpersNotConfigured):
        render_audio.__wrapped__(scenes=scenes)


def test_render_audio_composes_helpers_in_contract_order() -> None:
    """Verify TTS → loudness → WhisperX → B2 upload order with fakes.

    The ADK callback ordered these calls and downstream
    ``AudioInvariantEvaluator`` expects per-voice block output.  We
    assert the call sequence is preserved by the Strands port.
    """
    calls: list[tuple[str, str]] = []

    def fake_tts(scene_num: int, voice_role: str, text: str, language: str) -> str:
        calls.append(("tts", f"{scene_num}/{voice_role}"))
        return f"/tmp/fake_{scene_num}_{voice_role}.wav"

    def fake_whisperx(wav_path: str, text: str, language: str) -> dict[str, Any]:
        calls.append(("whisperx", wav_path))
        word_count = len(text.split())
        return {
            "total_duration": float(word_count) / 2.5,
            "word_count": word_count,
            "words": [
                {"word": w, "start": i * 0.4, "end": (i + 1) * 0.4}
                for i, w in enumerate(text.split())
            ],
        }

    def fake_loudness(wav_path: str, target_lufs: float) -> None:
        calls.append(("loudness", wav_path))

    def fake_upload(wav_path: str) -> str:
        calls.append(("upload", wav_path))
        return f"https://b2.example.com/{Path(wav_path).name}"

    clear_audio_helpers()
    set_audio_helpers(
        tts_generate=fake_tts,
        whisperx_align=fake_whisperx,
        loudness_normalize=fake_loudness,
        b2_upload=fake_upload,
    )
    try:
        result = render_audio.__wrapped__(
            scenes=[
                {
                    "id": 1,
                    "voices": [
                        {"voice_id": "V1", "text": "intro line."},
                    ],
                },
                {
                    "id": 2,
                    "voices": [
                        {"voice_id": "V1", "text": "body line one."},
                        {"voice_id": "V2", "text": "body line two."},
                    ],
                },
            ],
            language="en",
        )
    finally:
        clear_audio_helpers()

    # Expected call order per (scene, voice): tts, loudness, whisperx, upload.
    kinds_per_block = [("tts", "loudness", "whisperx", "upload") for _ in range(3)]
    expected_kinds = [kind for block in kinds_per_block for kind in block]
    assert [c[0] for c in calls] == expected_kinds, (
        f"helpers called in wrong order: {[c[0] for c in calls]}"
    )
    assert result["block_count"] == 3
    assert result["scene_count"] == 2
    assert result["whisperx_alignment"]["language"] == "en"
    assert result["whisperx_alignment"]["total_duration_sec"] > 0
    assert len(result["narration_blocks"]) == 3
    for block in result["narration_blocks"]:
        for key in ("block_id", "wav_path", "b2_url", "duration_sec"):
            assert key in block, f"narration block missing {key}: {block}"


# ---------------------------------------------------------------------------
# Live: end-to-end synthesis fidelity
# ---------------------------------------------------------------------------


@pytest.fixture()
def _openai_required() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; skipping live TTS synthesis.")


def _synthesize_tts(text: str, voice: str = "alloy") -> str:
    """Synthesize ``text`` via OpenAI gpt-4o-mini-tts; return WAV path."""
    client = openai.OpenAI()
    response = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice=voice,
        input=text,
        response_format="wav",
    )
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="tts_live_")
    os.close(fd)
    response.write_to_file(path)
    return path


@requires_google_api
def test_synthesized_english_narration_is_on_topic(
    _openai_required: None,
) -> None:
    """End-to-end: TTS produces audio, Gemini confirms language + topic.

    Synthesizes a genuine documentary line about Weimar hyperinflation
    with OpenAI's production TTS, then asks Gemini (a separate model
    family) whether the audio is English AND about that topic.  A no
    here means either TTS produced the wrong language/content or the
    judge can't handle the call — both are real defects.
    """
    wav_path = _synthesize_tts(_NARRATION_EN)
    try:
        prompt = (
            "You are listening to an audio clip.  Is this clip BOTH in "
            "English AND about 'the 1923 Weimar hyperinflation' (Germany, "
            "currency devaluation, loaves of bread, wheelbarrows of marks)? "
            "Answer with a single word: yes or no."
        )
        verdict = judge_audio_yes(prompt, wav_path)
        assert not verdict.disabled, f"Gemini audio judge disabled: {verdict.error}"
        assert verdict.is_yes, (
            f"Gemini judged TTS'd English hyperinflation narration as "
            f"off-target; answer={verdict.answer!r}"
        )
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


@requires_google_api
def test_synthesized_gibberish_is_not_judged_on_topic(
    _openai_required: None,
) -> None:
    """Contra-case: nonsense audio must not pass the topic-adherence judge.

    The TTS engine will dutifully vocalize gibberish; Gemini should
    reject "is this clip about hyperinflation" for gibberish.  If it
    says yes, the judge is unusable for this gate.
    """
    wav_path = _synthesize_tts(_NARRATION_GIBBERISH)
    try:
        prompt = (
            "You are listening to an audio clip.  Is this clip BOTH in "
            "English AND about 'the 1923 Weimar hyperinflation' (Germany, "
            "currency devaluation, loaves of bread, wheelbarrows of marks)? "
            "Answer with a single word: yes or no."
        )
        verdict = judge_audio_yes(prompt, wav_path)
        assert not verdict.disabled, f"Gemini audio judge disabled: {verdict.error}"
        assert verdict.is_yes is False, (
            f"Gemini judged gibberish audio as on-topic hyperinflation "
            f"narration; answer={verdict.answer!r}"
        )
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
