"""Unit tests for the anti-drift model pin enforcement.

Covers the shared ``strands_agents._model_pin`` module plus the
per-worker pins for Qwen3-TTS and LTX-Video. The suite exercises:

* :class:`ModelPin` is frozen — runtime mutation raises.
* :func:`verify_pin` returns the snapshot dir on a happy path where
  every required file's SHA256 matches.
* :func:`verify_pin` raises :class:`ModelPinMismatchError` when a
  required file is missing.
* :func:`verify_pin` raises :class:`ModelPinMismatchError` when a
  required file's bytes have been tampered with (different SHA256).
* The per-worker pins point at the locked production model ids
  (``Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice``, ``Lightricks/LTX-2.3``)
  with 40-char commit-SHA revisions and full 64-char hex digests for
  every required file.

The suite never touches the network: ``verify_pin`` is called with a
synthetic ``snapshot_dir`` argument that points at a fixture
directory the test populates.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from types import MappingProxyType

import pytest

from strands_agents._model_pin import (
    ModelPin,
    ModelPinMismatchError,
    verify_pin,
)
from strands_agents.ltx_video_worker._model_pin import (
    LTX_VIDEO_GEMMA_PIN,
    LTX_VIDEO_PIN,
)
from strands_agents.qwen3_tts_worker._model_pin import QWEN3_TTS_PIN


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_pin_file(root: Path, relative: str, data: bytes) -> Path:
    """Write ``data`` to ``root / relative`` (creating parents)."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _build_synthetic_pin(
    root: Path, *, files: dict[str, bytes], purpose: str = "test"
) -> ModelPin:
    """Materialize ``files`` under ``root`` and build a matching pin."""
    required: dict[str, str] = {}
    for relative, data in files.items():
        _write_pin_file(root, relative, data)
        required[relative] = _sha256(data)
    return ModelPin(
        model_id="acme/test-model",
        revision="0123456789abcdef0123456789abcdef01234567",
        required_files=MappingProxyType(required),
        purpose=purpose,
    )


# ---------------------------------------------------------------------
# ModelPin dataclass behaviour
# ---------------------------------------------------------------------


def test_model_pin_is_frozen() -> None:
    """The dataclass must be frozen so live code can't mutate it."""
    assert dataclasses.is_dataclass(ModelPin)
    pin = ModelPin(
        model_id="acme/foo",
        revision="0" * 40,
        required_files=MappingProxyType({"a": "b" * 64}),
        purpose="test",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pin.model_id = "acme/bar"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        pin.revision = "1" * 40  # type: ignore[misc]


def test_model_pin_required_files_is_immutable_view() -> None:
    """The pinned mapping should not be mutable through the field."""
    pin = ModelPin(
        model_id="acme/foo",
        revision="0" * 40,
        required_files=MappingProxyType({"a": "b" * 64}),
        purpose="test",
    )
    # MappingProxyType blocks direct mutation.
    with pytest.raises(TypeError):
        pin.required_files["a"] = "z" * 64  # type: ignore[index]


# ---------------------------------------------------------------------
# verify_pin happy path
# ---------------------------------------------------------------------


def test_verify_pin_passes_when_all_files_match(tmp_path: Path) -> None:
    """A snapshot dir whose files hash to the pinned values verifies."""
    pin = _build_synthetic_pin(
        tmp_path,
        files={
            "weights/main.safetensors": b"hello world",
            "weights/aux.safetensors": b"goodbye world",
        },
        purpose="happy-path",
    )
    out = verify_pin(pin, snapshot_dir=tmp_path)
    assert out == tmp_path


def test_verify_pin_returns_snapshot_dir_unchanged(tmp_path: Path) -> None:
    """The returned path is exactly the verified snapshot dir."""
    pin = _build_synthetic_pin(
        tmp_path,
        files={"only.safetensors": b"x" * 1024},
    )
    out = verify_pin(pin, snapshot_dir=tmp_path)
    assert out == tmp_path
    assert out.is_dir()


# ---------------------------------------------------------------------
# verify_pin negative paths
# ---------------------------------------------------------------------


def test_verify_pin_raises_on_tampered_file(tmp_path: Path) -> None:
    """Flipping a single byte in a required file fails verification."""
    pin = _build_synthetic_pin(
        tmp_path,
        files={"a.safetensors": b"original-bytes"},
        purpose="tamper-test",
    )
    target = tmp_path / "a.safetensors"
    target.write_bytes(b"tampered-bytes")
    with pytest.raises(ModelPinMismatchError) as exc_info:
        verify_pin(pin, snapshot_dir=tmp_path)
    msg = str(exc_info.value)
    assert "a.safetensors" in msg
    assert "tamper-test" in msg
    assert "expected_sha256" in msg


def test_verify_pin_raises_on_missing_file(tmp_path: Path) -> None:
    """A required file that doesn't exist fails verification."""
    pin = ModelPin(
        model_id="acme/test",
        revision="0" * 40,
        required_files=MappingProxyType(
            {"missing.safetensors": "f" * 64},
        ),
        purpose="missing-test",
    )
    with pytest.raises(ModelPinMismatchError) as exc_info:
        verify_pin(pin, snapshot_dir=tmp_path)
    msg = str(exc_info.value)
    assert "missing.safetensors" in msg
    assert "missing under" in msg


def test_verify_pin_raises_on_first_mismatch_only(tmp_path: Path) -> None:
    """Verification must fail loudly on the first bad file, not silently skip."""
    good = b"good-bytes"
    bad_actual = b"bad-bytes"
    bad_expected = b"bad-bytes-expected"
    _write_pin_file(tmp_path, "good.safetensors", good)
    _write_pin_file(tmp_path, "bad.safetensors", bad_actual)
    pin = ModelPin(
        model_id="acme/test",
        revision="0" * 40,
        required_files=MappingProxyType(
            {
                "good.safetensors": _sha256(good),
                "bad.safetensors": _sha256(bad_expected),
            }
        ),
        purpose="first-mismatch",
    )
    with pytest.raises(ModelPinMismatchError) as exc_info:
        verify_pin(pin, snapshot_dir=tmp_path)
    assert "bad.safetensors" in str(exc_info.value)


# ---------------------------------------------------------------------
# Production pin shape (per-worker)
# ---------------------------------------------------------------------


_HEX_SHA256_LEN = 64
_HEX_GIT_SHA_LEN = 40


def _is_hex(s: str, length: int) -> bool:
    if len(s) != length:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    "pin",
    [
        pytest.param(QWEN3_TTS_PIN, id="qwen3-tts"),
        pytest.param(LTX_VIDEO_PIN, id="ltx-video"),
        pytest.param(LTX_VIDEO_GEMMA_PIN, id="ltx-video-gemma"),
    ],
)
def test_production_pin_revision_is_full_commit_sha(pin: ModelPin) -> None:
    """Revisions must be 40-char commit SHAs, never branch names or tags."""
    assert _is_hex(pin.revision, _HEX_GIT_SHA_LEN), (
        f"pin {pin.purpose!r} revision must be a 40-char hex commit SHA, "
        f"got {pin.revision!r} (length {len(pin.revision)})"
    )


@pytest.mark.parametrize(
    "pin",
    [
        pytest.param(QWEN3_TTS_PIN, id="qwen3-tts"),
        pytest.param(LTX_VIDEO_PIN, id="ltx-video"),
        pytest.param(LTX_VIDEO_GEMMA_PIN, id="ltx-video-gemma"),
    ],
)
def test_production_pin_required_files_use_full_sha256(pin: ModelPin) -> None:
    """Every pinned hash must be a 64-char hex SHA256."""
    assert pin.required_files, f"pin {pin.purpose!r} has no required files"
    for path, digest in pin.required_files.items():
        assert _is_hex(digest, _HEX_SHA256_LEN), (
            f"pin {pin.purpose!r} file {path!r} must have a 64-char hex SHA256, "
            f"got {digest!r} (length {len(digest)})"
        )


def test_qwen3_tts_pin_targets_customvoice_checkpoint() -> None:
    """The TTS pin must point at the CustomVoice 12Hz checkpoint."""
    assert QWEN3_TTS_PIN.model_id == "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert "model.safetensors" in QWEN3_TTS_PIN.required_files
    assert "speech_tokenizer/model.safetensors" in QWEN3_TTS_PIN.required_files


def test_ltx_video_pin_targets_ltx_2_3() -> None:
    """The video pin MUST point at LTX-2.3 (mandatory per user rule).

    Required-files set is restricted to the safetensors that the
    ``ltx_pipelines.ti2vid_one_stage`` BASIC pipeline actually loads:
    a single base checkpoint. Other LTX-2.3 assets (distilled
    checkpoints, spatial / temporal upscalers, distilled LoRAs)
    exist in the repo but are not read by the BASIC pipeline, so
    verifying them every startup would burn ~70 GB of disk reads
    for no integrity gain.
    """
    assert LTX_VIDEO_PIN.model_id == "Lightricks/LTX-2.3"
    expected_files = {
        "ltx-2.3-22b-dev.safetensors",
    }
    assert set(LTX_VIDEO_PIN.required_files) == expected_files


def test_ltx_video_gemma_pin_targets_unquantized_gemma_3_12b() -> None:
    """The Gemma pin MUST point at Lightricks' non-gated Gemma-3-12B mirror.

    LTX-2.3's ``PromptEncoder`` requires Gemma-3-12B as its text
    encoder. The official ``google/gemma-3-12b-it-qat-q4_0-unquantized``
    repo is gated, but Lightricks publishes a byte-identical re-host
    at ``Lightricks/gemma-3-12b-it-qat-q4_0-unquantized`` that is
    pullable without a license-acceptance token. The BASIC
    ``ti2vid_one_stage`` pipeline receives the snapshot dir verified
    by this pin via ``--gemma-root``.
    """
    assert LTX_VIDEO_GEMMA_PIN.model_id == (
        "Lightricks/gemma-3-12b-it-qat-q4_0-unquantized"
    )
    # Gemma-3-12B ships as 5 sharded safetensors files.
    expected_files = {
        f"model-0000{i}-of-00005.safetensors" for i in range(1, 6)
    }
    assert set(LTX_VIDEO_GEMMA_PIN.required_files) == expected_files


def test_pins_have_unique_purposes() -> None:
    """Purposes are used in error messages — each worker needs its own."""
    purposes = {
        QWEN3_TTS_PIN.purpose,
        LTX_VIDEO_PIN.purpose,
        LTX_VIDEO_GEMMA_PIN.purpose,
    }
    assert len(purposes) == 3
    assert "qwen3-tts" in purposes
    assert "ltx-video" in purposes
    assert "ltx-video-gemma" in purposes
