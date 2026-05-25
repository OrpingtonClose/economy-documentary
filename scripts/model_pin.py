"""Anti-drift model pin verification.

Verifies that model weight files on disk match pinned SHA256 hashes
before inference.  No network calls — pure local hashing.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

_HASH_CHUNK_BYTES = 1024 * 1024


class ModelPinMismatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPin:
    purpose: str
    required_files: Mapping[str, str]  # relative path -> expected sha256 hex


def _hash_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_pin(pin: ModelPin, base_dir: Path) -> None:
    """Verify pinned files under base_dir.  Raises on first mismatch."""
    for relative_path, expected_sha256 in pin.required_files.items():
        file_path = base_dir / relative_path
        if not file_path.is_file():
            raise ModelPinMismatchError(
                f"model pin verification failed for purpose={pin.purpose!r}: "
                f"required file {relative_path!r} missing under {base_dir}"
            )
        actual = _hash_file_sha256(file_path)
        if actual != expected_sha256:
            raise ModelPinMismatchError(
                f"model pin verification failed for purpose={pin.purpose!r}: "
                f"file={relative_path!r} actual_sha256={actual!r} "
                f"expected_sha256={expected_sha256!r}"
            )
        logger.debug("purpose=<%s>, file=<%s>, sha256=<%s> | verified", pin.purpose, relative_path, actual)
    logger.info("purpose=<%s>, files=<%d> | model pin verified", pin.purpose, len(pin.required_files))


# ---------------------------------------------------------------------------
# Production pins
# ---------------------------------------------------------------------------

QWEN3_TTS_PIN = ModelPin(
    purpose="qwen3-tts",
    required_files={
        "model.safetensors": "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137",
        "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
    },
)

LTX_VIDEO_PIN = ModelPin(
    purpose="ltx-video",
    required_files={
        "ltx-2.3-22b-dev.safetensors": "7ab7225325bc403448ea84b6db2269811a880e5118cd2ee2b6282a93d585016f",
    },
)

LTX_VIDEO_GEMMA_PIN = ModelPin(
    purpose="ltx-video-gemma",
    required_files={
        "model-00001-of-00005.safetensors": "e6fb899db428481aafb45a20130457df6e247e7cb03b7d9f01ee4bc2a9a08138",
        "model-00002-of-00005.safetensors": "d251e7fe9799d529405ddb61705a44cd700bd30a8b66a8d44ae26ddf8365dbc6",
        "model-00003-of-00005.safetensors": "0684ef801385f0669a0b3e4ab160c50877efdbfa40eb97788595985de2743e78",
        "model-00004-of-00005.safetensors": "b4b964e6526f81ccfa625c900b72ce92d5e0fd2debb75998763038ad06b9c541",
        "model-00005-of-00005.safetensors": "4ef2de8f93e165b4e02425769fc566000b0674256ef0c3a27b23a0d45eb12088",
    },
)


__all__ = [
    "ModelPin",
    "ModelPinMismatchError",
    "verify_pin",
    "QWEN3_TTS_PIN",
    "LTX_VIDEO_PIN",
    "LTX_VIDEO_GEMMA_PIN",
]
