"""Frozen model pin for the Qwen3-TTS worker.

Locks the production TTS model to a specific Hugging Face commit and
the SHA256 of every required safetensors file. See
:mod:`strands_agents._model_pin` for the rationale and the
verification protocol.

The values below are not configurable via environment variables.
Changing the pinned model — even to a different revision of the same
repo — requires a code edit *and* an update to the corresponding
SHA256 hashes, both of which are visible in any PR diff.

Hashes were sourced from the LFS ``oid sha256`` field returned by
``GET https://huggingface.co/api/models/Qwen/Qwen3-TTS-12Hz-1.7B-
CustomVoice/tree/main?recursive=true`` at revision
``0c0e3051f131929182e2c023b9537f8b1c68adfe``.
"""

from __future__ import annotations

from types import MappingProxyType

from strands_agents._model_pin import ModelPin

QWEN3_TTS_PIN: ModelPin = ModelPin(
    model_id="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    revision="0c0e3051f131929182e2c023b9537f8b1c68adfe",
    required_files=MappingProxyType(
        {
            "model.safetensors": (
                "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137"
            ),
            "speech_tokenizer/model.safetensors": (
                "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258"
            ),
        }
    ),
    purpose="qwen3-tts",
)


__all__ = []
