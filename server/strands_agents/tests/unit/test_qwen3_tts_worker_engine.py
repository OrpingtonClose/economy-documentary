"""Unit tests for the Qwen3-TTS stub engine."""

from __future__ import annotations

import io
import wave

import pytest

from strands_agents.qwen3_tts_worker.engine import (
    StubTTSEngine,
    SynthesisRequest,
    TTSEngineError,
)


def _read_wav_duration_s(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
        return frames / rate


def test_stub_engine_id_is_stub() -> None:
    assert StubTTSEngine().engine_id == "stub"


def test_stub_engine_produces_valid_wav() -> None:
    engine = StubTTSEngine()
    result = engine.synthesize(
        SynthesisRequest(text="Hello world, this is a short line.", voice_id="alex")
    )

    assert result.voice_id == "alex"
    assert result.engine == "stub"
    assert result.sample_rate_hz == engine.sample_rate_hz

    duration = _read_wav_duration_s(result.wav_bytes)
    assert duration == pytest.approx(result.duration_s, rel=0.01)


def test_stub_engine_scales_duration_with_text_length() -> None:
    engine = StubTTSEngine(chars_per_second=10.0)
    short = engine.synthesize(SynthesisRequest(text="short", voice_id="v"))
    long = engine.synthesize(
        SynthesisRequest(text="a" * 200, voice_id="v")
    )
    assert long.duration_s > short.duration_s
    assert long.duration_s == pytest.approx(20.0, rel=0.01)


def test_stub_engine_rejects_empty_text() -> None:
    engine = StubTTSEngine()
    with pytest.raises(TTSEngineError):
        engine.synthesize(SynthesisRequest(text="   ", voice_id="v"))


def test_stub_engine_rejects_empty_voice() -> None:
    engine = StubTTSEngine()
    with pytest.raises(TTSEngineError):
        engine.synthesize(SynthesisRequest(text="hi there", voice_id=""))


def test_stub_engine_minimum_duration_floor() -> None:
    engine = StubTTSEngine(chars_per_second=1000.0)
    result = engine.synthesize(SynthesisRequest(text="hi", voice_id="v"))
    assert result.duration_s >= 0.1


def test_stub_engine_simulated_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "strands_agents.qwen3_tts_worker.engine.time.sleep", _fake_sleep
    )
    engine = StubTTSEngine(simulated_latency_s=0.42)
    engine.synthesize(SynthesisRequest(text="hello", voice_id="v"))
    assert sleeps == [0.42]
