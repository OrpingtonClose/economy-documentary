"""Unit tests for the LTX-Video stub engine."""

from __future__ import annotations

import pytest

from strands_agents.ltx_video_worker.engine import (
    MAX_DURATION_S,
    MIN_DURATION_S,
    RenderRequest,
    StubVideoEngine,
    VideoEngineError,
)


def _parse_iso_bmff(mp4: bytes) -> list[tuple[bytes, int]]:
    """Walk ISO-BMFF boxes; return [(type, size), ...]."""
    boxes: list[tuple[bytes, int]] = []
    pos = 0
    while pos + 8 <= len(mp4):
        size = int.from_bytes(mp4[pos : pos + 4], "big")
        btype = mp4[pos + 4 : pos + 8]
        if size < 8 or pos + size > len(mp4):
            raise AssertionError(
                f"bad box at pos={pos}: size={size} type={btype!r}"
            )
        boxes.append((btype, size))
        pos += size
    return boxes


def test_stub_engine_id_is_stub() -> None:
    assert StubVideoEngine().engine_id == "stub"


def test_stub_engine_emits_valid_ftyp_plus_mdat() -> None:
    engine = StubVideoEngine()
    result = engine.render(
        RenderRequest(prompt="A river.", duration_s=1.0)
    )
    boxes = _parse_iso_bmff(result.mp4_bytes)
    assert [b[0] for b in boxes] == [b"ftyp", b"mdat"]


def test_stub_engine_mp4_size_scales_with_duration() -> None:
    engine = StubVideoEngine(bytes_per_second=10_000)
    short = engine.render(RenderRequest(prompt="p", duration_s=0.5))
    long = engine.render(RenderRequest(prompt="p", duration_s=5.0))
    assert len(long.mp4_bytes) > len(short.mp4_bytes)


def test_stub_engine_clamps_below_minimum() -> None:
    engine = StubVideoEngine()
    result = engine.render(
        RenderRequest(prompt="p", duration_s=0.01)
    )
    assert result.duration_s == MIN_DURATION_S


def test_stub_engine_clamps_above_maximum() -> None:
    engine = StubVideoEngine()
    result = engine.render(
        RenderRequest(prompt="p", duration_s=MAX_DURATION_S + 50)
    )
    assert result.duration_s == MAX_DURATION_S


def test_stub_engine_rejects_empty_prompt() -> None:
    engine = StubVideoEngine()
    with pytest.raises(VideoEngineError):
        engine.render(RenderRequest(prompt="   ", duration_s=1.0))


def test_stub_engine_rejects_zero_duration() -> None:
    engine = StubVideoEngine()
    with pytest.raises(VideoEngineError):
        engine.render(RenderRequest(prompt="p", duration_s=0.0))


def test_stub_engine_rejects_negative_dimensions() -> None:
    engine = StubVideoEngine()
    with pytest.raises(VideoEngineError):
        engine.render(
            RenderRequest(prompt="p", duration_s=1.0, width=0, height=720)
        )
    with pytest.raises(VideoEngineError):
        engine.render(
            RenderRequest(prompt="p", duration_s=1.0, width=1280, height=-1)
        )


def test_stub_engine_rejects_zero_fps() -> None:
    engine = StubVideoEngine()
    with pytest.raises(VideoEngineError):
        engine.render(RenderRequest(prompt="p", duration_s=1.0, fps=0))


def test_stub_engine_passes_through_dimensions_and_fps() -> None:
    engine = StubVideoEngine()
    result = engine.render(
        RenderRequest(
            prompt="p", duration_s=1.0, width=1920, height=1080, fps=30
        )
    )
    assert result.width == 1920
    assert result.height == 1080
    assert result.fps == 30
    assert result.engine == "stub"


def test_stub_engine_is_deterministic_for_same_prompt() -> None:
    engine = StubVideoEngine()
    a = engine.render(RenderRequest(prompt="same", duration_s=1.0))
    b = engine.render(RenderRequest(prompt="same", duration_s=1.0))
    assert a.mp4_bytes == b.mp4_bytes


def test_stub_engine_differs_for_different_prompts() -> None:
    engine = StubVideoEngine()
    a = engine.render(RenderRequest(prompt="alpha", duration_s=1.0))
    b = engine.render(RenderRequest(prompt="beta", duration_s=1.0))
    assert a.mp4_bytes != b.mp4_bytes


def test_stub_engine_mdat_has_minimum_payload() -> None:
    engine = StubVideoEngine(bytes_per_second=1)
    result = engine.render(RenderRequest(prompt="p", duration_s=MIN_DURATION_S))
    boxes = _parse_iso_bmff(result.mp4_bytes)
    mdat = next(b for b in boxes if b[0] == b"mdat")
    # Minimum payload floor is 64 bytes + 8-byte header = 72.
    assert mdat[1] >= 72


def test_stub_engine_simulated_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "strands_agents.ltx_video_worker.engine.time.sleep", _fake_sleep
    )
    engine = StubVideoEngine(simulated_latency_s=0.25)
    engine.render(RenderRequest(prompt="p", duration_s=1.0))
    assert sleeps == [0.25]


def test_render_request_defaults() -> None:
    req = RenderRequest(prompt="p", duration_s=1.0)
    assert req.width == 1280
    assert req.height == 720
    assert req.fps == 24
    assert req.style is None
    assert req.seed is None
    assert req.negative_prompt is None
