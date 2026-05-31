"""Frozen model pins for the LTX-Video worker.

Locks two production models to specific Hugging Face commits, with
SHA256s for every safetensors file the LTX-2.3 BASIC one-stage
pipeline (``ltx_pipelines.ti2vid_one_stage`` from the
Lightricks/LTX-2 monorepo) reads at inference time:

* :data:`LTX_VIDEO_PIN` — ``Lightricks/LTX-2.3`` weights, the full
  ``ltx-2.3-22b-dev.safetensors`` base checkpoint that the BASIC
  single-stage pipeline loads via ``--checkpoint-path``.
* :data:`LTX_VIDEO_GEMMA_PIN` — the Lightricks-hosted, non-gated
  re-publish ``Lightricks/gemma-3-12b-it-qat-q4_0-unquantized`` text
  encoder weights (5 sharded safetensors, byte-identical to
  ``google/gemma-3-12b-it-qat-q4_0-unquantized`` but pullable
  without an HF license token). LTX-2.3's ``PromptEncoder`` loads
  these via ``--gemma-root``.

Per the user's standing rule (``docs/strands-migration/AGENTS.md`` and
slice 9d):

* Usage of LTX-2.3 is **mandatory**. The original ``Lightricks/LTX-
  Video`` 2B model and the 13B variant are not acceptable
  substitutes. A future LLM agent that tries to swap the model will
  hit either a visible code diff (caught in PR review) or a runtime
  ``ModelPinMismatchError`` at engine startup (caught in CI / live).
* The hash check runs **always**, before any inference. There is no
  ``--skip-pin-check`` flag and no env-var override.

Required-files set is restricted to the bytes the BASIC one-stage
pipeline actually reads (one base checkpoint + the Gemma text encoder
shards). Other LTX-2.3 assets — the distilled / distilled-1.1
checkpoints, the spatial / temporal upscalers, the distilled LoRAs —
exist in the HF repo but are not loaded by
``TI2VidOneStagePipeline``, so verifying them every startup would
burn ~70 GB of disk reads for no integrity gain. Future pipeline
swaps (e.g. to ``ti2vid_two_stages``) should add the new files to
this map.

Hashes were sourced from the LFS ``oid sha256`` field returned by

* ``GET https://huggingface.co/api/models/Lightricks/LTX-2.3/tree/<rev>``
* ``GET https://huggingface.co/api/models/Lightricks/gemma-3-12b-it-qat-q4_0-unquantized/tree/<rev>``

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
            "ltx-2.3-22b-dev.safetensors": (
                "7ab7225325bc403448ea84b6db2269811a880e5118cd2ee2b6282a93d585016f"
            ),
        }
    ),
    purpose="ltx-video",
    # The BASIC ``ti2vid_one_stage`` CLI takes ``--checkpoint-path``
    # pointing at a single safetensors file; sibling configs /
    # distilled checkpoints / upscalers / LoRAs in the repo are not
    # read. Restrict the on-demand snapshot download to match the
    # bootstrap's pattern set so first-render does not silently
    # complete the rest of the ~70+ GB repo.
    download_allow_patterns=("ltx-2.3-22b-dev.safetensors",),
)


LTX_VIDEO_GEMMA_PIN: ModelPin = ModelPin(
    model_id="Lightricks/gemma-3-12b-it-qat-q4_0-unquantized",
    revision="d62fe4f1995ade703b49a0f3c0d0f161237ef437",
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


__all__ = []
