"""Frozen model pin for the LTX-Video worker.

Locks the production video model to ``Lightricks/LTX-2.3`` at a
specific Hugging Face commit, with SHA256s for every safetensors file
the two-stage LTX-2.3 pipeline needs.

Per the user's standing rule (``docs/strands-migration/AGENTS.md`` and
slice 9d):

* Usage of LTX-2.3 is **mandatory**. The original ``Lightricks/LTX-
  Video`` 2B model and the 13B variant are not acceptable
  substitutes. A future LLM agent that tries to swap the model will
  hit either a visible code diff (caught in PR review) or a runtime
  ``ModelPinMismatchError`` at engine startup (caught in CI / live).
* The hash check runs **always**, before any inference. There is no
  ``--skip-pin-check`` flag and no env-var override.

Required-files set covers the LTX-2.3 base model, distilled fast
secondary, distilled LoRA, and both upscalers — i.e. every weight
file the two-stage LTX-2.3 pipeline (slice 9d-wire) loads. Other
files in the repo (configs, schedulers, tokenizers) are downloaded
by ``snapshot_download`` but not hashed here; the SHA256s above
are the integrity-critical bytes.

Hashes were sourced from the LFS ``oid sha256`` field returned by
``GET https://huggingface.co/api/models/Lightricks/LTX-2.3/tree/main``
at revision ``76730e634e70a28f4e8d51f5e29c08e40e2d8e74``.
"""

from __future__ import annotations

from types import MappingProxyType

from strands_agents._model_pin import ModelPin

LTX_VIDEO_PIN: ModelPin = ModelPin(
    model_id="Lightricks/LTX-2.3",
    revision="76730e634e70a28f4e8d51f5e29c08e40e2d8e74",
    required_files=MappingProxyType(
        {
            "ltx-2.3-22b-dev.safetensors": (
                "7ab7225325bc403448ea84b6db2269811a880e5118cd2ee2b6282a93d585016f"
            ),
            "ltx-2.3-22b-distilled-1.1.safetensors": (
                "b33b7fe4bbfe084f484be4aaf90b0f1d95dca20d403ac4c0e037eb8c4f0af7cc"
            ),
            "ltx-2.3-22b-distilled-lora-384-1.1.safetensors": (
                "f5d4953f3386197a4b4f5abdb17616ff256171e8075c111d6e7d2dfa6e823b3a"
            ),
            "ltx-2.3-spatial-upscaler-x2-1.1.safetensors": (
                "5f416311fa8172b65af67530758964708d29a317b830d689a51143b7f91913ed"
            ),
            "ltx-2.3-temporal-upscaler-x2-1.0.safetensors": (
                "2bc3300f2b3c3c1834d72164fbf13a3b9fd73e5a741e8a2c3f4035f89a75c3fe"
            ),
        }
    ),
    purpose="ltx-video",
)


__all__ = ["LTX_VIDEO_PIN"]
