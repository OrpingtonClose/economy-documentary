"""Unit tests for engine-level model-pin wiring.

The pin module's correctness is exercised by ``test_model_pin.py``.
This file proves the *engines* actually honour the pin — that the
constructor refuses to accept a foreign ``model_id`` override and
that the pinned ``model_id`` is exposed as the engine's
``DEFAULT_MODEL_ID``. Together with the load-path call to
``verify_pin`` (covered by integration tests on the GPU host), this
closes the anti-drift loop:

* ``model_id`` cannot be overridden via constructor or environment.
* ``model_id`` cannot be overridden via the legacy ``QWEN3_TTS_MODEL_ID``
  / ``LTX_VIDEO_MODEL_ID`` env vars (they are no longer read).
* The engine asks ``verify_pin`` to materialize and hash the bytes
  before any inference call.
"""

from __future__ import annotations

import pytest

from strands_agents.ltx_video_worker import _ltx_engine as ltx_engine
from strands_agents.ltx_video_worker._model_pin import LTX_VIDEO_PIN
from strands_agents.qwen3_tts_worker import _qwen3_engine as qwen3_engine
from strands_agents.qwen3_tts_worker._model_pin import QWEN3_TTS_PIN


# ---------------------------------------------------------------------
# Qwen3-TTS engine
# ---------------------------------------------------------------------


def test_qwen3_engine_default_model_id_matches_pin() -> None:
    assert qwen3_engine.DEFAULT_MODEL_ID == QWEN3_TTS_PIN.model_id


def test_qwen3_engine_constructor_rejects_foreign_model_id() -> None:
    """Caller-supplied ``model_id`` overrides are forbidden."""
    with pytest.raises(qwen3_engine.TTSEngineError) as exc_info:
        qwen3_engine.Qwen3TTSEngine(model_id="acme/some-other-tts")
    msg = str(exc_info.value)
    assert "model_id override is forbidden" in msg
    assert "acme/some-other-tts" in msg
    assert QWEN3_TTS_PIN.model_id in msg


def test_qwen3_engine_constructor_accepts_pinned_model_id() -> None:
    """Passing the pinned id explicitly is a no-op (still allowed)."""
    engine = qwen3_engine.Qwen3TTSEngine(model_id=QWEN3_TTS_PIN.model_id)
    assert engine._model_id == QWEN3_TTS_PIN.model_id  # noqa: SLF001


def test_qwen3_engine_constructor_default_uses_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with the legacy env var set, the engine ignores it."""
    monkeypatch.setenv("QWEN3_TTS_MODEL_ID", "acme/should-be-ignored")
    engine = qwen3_engine.Qwen3TTSEngine()
    assert engine._model_id == QWEN3_TTS_PIN.model_id  # noqa: SLF001


# ---------------------------------------------------------------------
# LTX-Video engine
# ---------------------------------------------------------------------


def test_ltx_engine_default_model_id_matches_pin() -> None:
    assert ltx_engine.DEFAULT_MODEL_ID == LTX_VIDEO_PIN.model_id
    # Must be LTX-2.3 — the user's standing rule.
    assert ltx_engine.DEFAULT_MODEL_ID == "Lightricks/LTX-2.3"


def test_ltx_engine_constructor_rejects_foreign_model_id() -> None:
    with pytest.raises(ltx_engine.VideoEngineError) as exc_info:
        ltx_engine.LTXVideoEngine(model_id="Lightricks/LTX-Video")
    msg = str(exc_info.value)
    assert "model_id override is forbidden" in msg
    assert "Lightricks/LTX-Video" in msg
    assert LTX_VIDEO_PIN.model_id in msg


def test_ltx_engine_constructor_accepts_pinned_model_id() -> None:
    engine = ltx_engine.LTXVideoEngine(model_id=LTX_VIDEO_PIN.model_id)
    assert engine._model_id == LTX_VIDEO_PIN.model_id  # noqa: SLF001


def test_ltx_engine_constructor_default_uses_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy env var is no longer honoured."""
    monkeypatch.setenv("LTX_VIDEO_MODEL_ID", "Lightricks/LTX-Video")
    engine = ltx_engine.LTXVideoEngine()
    assert engine._model_id == LTX_VIDEO_PIN.model_id  # noqa: SLF001


# ---------------------------------------------------------------------
# argv composition — proves request fields reach the subprocess CLI
# ---------------------------------------------------------------------


def _build_argv(
    *,
    negative_prompt: str | None,
    seed: int | None = None,
) -> list[str]:
    from pathlib import Path

    engine = ltx_engine.LTXVideoEngine()
    return engine._build_ltx2_argv(  # noqa: SLF001
        ltx_dir=Path("/fake/ltx"),
        gemma_dir=Path("/fake/gemma"),
        prompt="a wide shot of the federal reserve building",
        output_path=Path("/tmp/out.mp4"),
        width=704,
        height=480,
        num_frames=121,
        fps=24,
        seed=seed,
        negative_prompt=negative_prompt,
    )


def test_build_argv_forwards_negative_prompt() -> None:
    """A non-empty ``negative_prompt`` must reach the CLI as ``--negative-prompt``."""
    argv = _build_argv(negative_prompt="blurry, low quality, watermark")

    assert "--negative-prompt" in argv
    idx = argv.index("--negative-prompt")
    assert argv[idx + 1] == "blurry, low quality, watermark"


def test_build_argv_omits_negative_prompt_when_none() -> None:
    """``None`` must NOT add a ``--negative-prompt`` flag (CLI default applies)."""
    argv = _build_argv(negative_prompt=None)
    assert "--negative-prompt" not in argv


def test_build_argv_omits_negative_prompt_when_blank() -> None:
    """Whitespace-only is treated as absent — keeps the CLI's own default."""
    argv = _build_argv(negative_prompt="   ")
    assert "--negative-prompt" not in argv


# ---------------------------------------------------------------------
# Subprocess timeout — bounds the blast radius of a hung GPU process
# ---------------------------------------------------------------------


def test_render_timeout_default_is_30_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LTX_VIDEO_LTX2_RENDER_TIMEOUT_S", raising=False)
    assert ltx_engine._ltx2_render_timeout_s() == 30 * 60  # noqa: SLF001


def test_render_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTX_VIDEO_LTX2_RENDER_TIMEOUT_S", "120")
    assert ltx_engine._ltx2_render_timeout_s() == 120  # noqa: SLF001


def test_render_timeout_env_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTX_VIDEO_LTX2_RENDER_TIMEOUT_S", "not-a-number")
    assert ltx_engine._ltx2_render_timeout_s() == 30 * 60  # noqa: SLF001


def test_render_timeout_env_non_positive_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTX_VIDEO_LTX2_RENDER_TIMEOUT_S", "0")
    assert ltx_engine._ltx2_render_timeout_s() == 30 * 60  # noqa: SLF001
    monkeypatch.setenv("LTX_VIDEO_LTX2_RENDER_TIMEOUT_S", "-7")
    assert ltx_engine._ltx2_render_timeout_s() == 30 * 60  # noqa: SLF001
