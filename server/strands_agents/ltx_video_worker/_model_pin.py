"""Frozen model pins for the LTX-Video worker.

Locks two production models to specific Hugging Face commits, with
SHA256s for every safetensors file the LTX-2.3 distilled two-stage
pipeline reads at inference time:

* :data:`LTX_VIDEO_PIN` — ``Lightricks/LTX-2.3`` weights (the
  22B-distilled checkpoint and the spatial x2 upscaler).
* :data:`LTX_VIDEO_GEMMA_PIN` — ``google/gemma-3-12b-it-qat-q4_0-unquantized``
  text encoder weights (5 sharded safetensors). LTX-2.3's
  ``PromptEncoder`` loads these via ``--gemma-root`` per the
  Lightricks/LTX-2 monorepo's ``ltx_pipelines.distilled`` CLI.

Per the user's standing rule (``docs/strands-migration/AGENTS.md`` and
slice 9d):

* Usage of LTX-2.3 is **mandatory**. The original ``Lightricks/LTX-
  Video`` 2B model and the 13B variant are not acceptable
  substitutes. A future LLM agent that tries to swap the model will
  hit either a visible code diff (caught in PR review) or a runtime
  ``ModelPinMismatchError`` at engine startup (caught in CI / live).
* The hash check runs **always**, before any inference. There is no
  ``--skip-pin-check`` flag and no env-var override.

Required-files set is restricted to the bytes the
``ltx_pipelines.distilled`` two-stage pipeline actually reads (one
distilled checkpoint + one spatial upscaler + the Gemma text encoder
shards). Other LTX-2.3 assets — the non-distilled ``dev``
checkpoint, the ``distilled-lora-384`` LoRA, the temporal upscaler —
exist in the HF repo but are not loaded by ``DistilledPipeline``, so
verifying them every startup would burn ~70 GB of disk reads for no
integrity gain. Future pipeline swaps (e.g. to ``ti2vid_two_stages``)
should add the new files to this map.

Hashes were sourced from the LFS ``oid sha256`` field returned by

* ``GET https://huggingface.co/api/models/Lightricks/LTX-2.3/tree/<rev>``
* ``GET https://huggingface.co/api/models/google/gemma-3-12b-it-qat-q4_0-unquantized/tree/<rev>``

at the pinned commits below.
"""

from __future__ import annotations

from types import MappingProxyType

from strands_agents._model_pin import ModelPin

LTX_VIDEO_PIN: ModelPin = ModelPin(
    model_id="Lightricks/LTX-2.3",
    revision="76730e634e70a28f4e8d51f5e29c08e40e2d8e74",
    required_files=MappingProxyType(
        {
            "ltx-2.3-22b-distilled-1.1.safetensors": (
                "b33b7fe4bbfe084f484be4aaf90b0f1d95dca20d403ac4c0e037eb8c4f0af7cc"
            ),
            "ltx-2.3-spatial-upscaler-x2-1.1.safetensors": (
                "5f416311fa8172b65af67530758964708d29a317b830d689a51143b7f91913ed"
            ),
        }
    ),
    purpose="ltx-video",
)


LTX_VIDEO_GEMMA_PIN: ModelPin = ModelPin(
    model_id="google/gemma-3-12b-it-qat-q4_0-unquantized",
    revision="68f7ee4fbd59087436ada77ed2d62f373fdd4482",
    required_files=MappingProxyType(
        {
            "model-00001-of-00005.safetensors": (
                "e6fb899db428481aafb45a20130457df6e247e7cb03b7d9f01ee4bc2a9a08138"
            ),
            "model-00002-of-00005.safetensors": (
                "d251e7fe9799d529405ddb61705a44cd700bd30a8b66a8d44ae26ddf8365dbc6"
            ),
            "model-00003-of-00005.safetensors": (
                "0684ef801385f0669a0b3e4ab160c50877efdbfa40eb97788595985de2743e78"
            ),
            "model-00004-of-00005.safetensors": (
                "b4b964e6526f81ccfa625c900b72ce92d5e0fd2debb75998763038ad06b9c541"
            ),
            "model-00005-of-00005.safetensors": (
                "4ef2de8f93e165b4e02425769fc566000b0674256ef0c3a27b23a0d45eb12088"
            ),
        }
    ),
    purpose="ltx-video-gemma",
)


__all__ = ["LTX_VIDEO_PIN", "LTX_VIDEO_GEMMA_PIN"]
