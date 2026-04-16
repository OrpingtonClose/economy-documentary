"""
Shared model configuration for all Strands agents.

Uses ``strands.models.litellm.LiteLLMModel`` instead of Google ADK's
``LiteLlm``.  Preserves the 4 model roles (primary, synthesis, thinker,
vision) and the Venice/OpenRouter ``extra_body`` logic.

Model roles:
  - ADK_MODEL: primary model for tool-capable agents
  - ADK_SYNTHESIS_MODEL: model for synthesis/evaluation agents
  - ADK_THINKER_MODEL: model for deep reasoning agents (content analyst)
  - ADK_VISION_MODEL: model for clip coherence evaluation (needs vision)
"""

from __future__ import annotations

import json
import logging
import os

from strands.models.litellm import LiteLLMModel

logger = logging.getLogger(__name__)

# -- Model names ---------------------------------------------------------------
_raw_model = os.environ.get("ADK_MODEL", "openai/gpt-4o")
ADK_MODEL_NAME = _raw_model.split(":")[0]

_raw_synthesis = os.environ.get("ADK_SYNTHESIS_MODEL", "")
_has_separate_synthesis = bool(_raw_synthesis)
ADK_SYNTHESIS_MODEL_NAME = (
    _raw_synthesis.split(":")[0] if _raw_synthesis else ADK_MODEL_NAME
)

_raw_thinker = os.environ.get("ADK_THINKER_MODEL", "")
_has_separate_thinker = bool(_raw_thinker)
ADK_THINKER_MODEL_NAME = (
    _raw_thinker.split(":")[0] if _raw_thinker else ADK_SYNTHESIS_MODEL_NAME
)

_raw_vision = os.environ.get("ADK_VISION_MODEL", "")
_has_separate_vision = bool(_raw_vision)
ADK_VISION_MODEL_NAME = (
    _raw_vision.split(":")[0] if _raw_vision else ADK_MODEL_NAME
)

# -- Extra body parameters (vendor-specific) ------------------------------------
_api_base = os.environ.get("OPENAI_API_BASE", "")
_is_venice = "venice.ai" in _api_base

_default_venice_params = json.dumps(
    {"include_venice_system_prompt": False} if _is_venice else {}
)
VENICE_PARAMS: dict = json.loads(
    os.environ.get("VENICE_PARAMS", _default_venice_params)
)

_extra_body: dict = {}
if VENICE_PARAMS:
    _extra_body["venice_parameters"] = VENICE_PARAMS

# -- Synthesis model config -----------------------------------------------------
_synthesis_api_base = os.environ.get("SYNTHESIS_API_BASE", "")
_synthesis_api_key = os.environ.get("SYNTHESIS_API_KEY", "")
_synthesis_is_venice = "venice.ai" in _synthesis_api_base
_synthesis_venice_params: dict = (
    json.loads(
        os.environ.get(
            "VENICE_PARAMS",
            json.dumps({"include_venice_system_prompt": False}),
        )
    )
    if _synthesis_is_venice
    else {}
)
_synthesis_extra_body: dict = {}
if _synthesis_venice_params:
    _synthesis_extra_body["venice_parameters"] = _synthesis_venice_params

# -- Thinker model config -------------------------------------------------------
_thinker_api_base = os.environ.get("THINKER_API_BASE", _synthesis_api_base)
_thinker_api_key = os.environ.get("THINKER_API_KEY", _synthesis_api_key)
_thinker_is_venice = "venice.ai" in _thinker_api_base
_thinker_venice_params: dict = (
    json.loads(
        os.environ.get(
            "VENICE_PARAMS",
            json.dumps({"include_venice_system_prompt": False}),
        )
    )
    if _thinker_is_venice
    else {}
)
_thinker_extra_body: dict = {}
if _thinker_venice_params:
    _thinker_extra_body["venice_parameters"] = _thinker_venice_params

# -- Vision model config --------------------------------------------------------
_vision_api_base = os.environ.get("VISION_API_BASE", _api_base)
_vision_api_key = os.environ.get("VISION_API_KEY", "")


def _pick_model_name(synthesis: bool, thinker: bool, vision: bool) -> str:
    """Select model name based on role flags."""
    if vision:
        return ADK_VISION_MODEL_NAME
    if thinker:
        return ADK_THINKER_MODEL_NAME
    if synthesis:
        return ADK_SYNTHESIS_MODEL_NAME
    return ADK_MODEL_NAME


def _pick_extra_body(synthesis: bool, thinker: bool, vision: bool) -> dict:
    """Select vendor-specific extra_body based on role flags."""
    if vision and _has_separate_vision:
        return {}
    if thinker and _has_separate_thinker and _thinker_api_base:
        return _thinker_extra_body
    if (synthesis or thinker) and _has_separate_synthesis and _synthesis_api_base:
        return _synthesis_extra_body
    return _extra_body


def _pick_client_args(synthesis: bool, thinker: bool, vision: bool) -> dict:
    """Build client_args (api_key, api_base) for the selected model role."""
    args: dict = {}

    if vision and _vision_api_key:
        args["api_key"] = _vision_api_key
    elif thinker and _thinker_api_key:
        args["api_key"] = _thinker_api_key
    elif (synthesis or thinker) and _synthesis_api_key:
        args["api_key"] = _synthesis_api_key

    if vision and _vision_api_base:
        args["api_base"] = _vision_api_base
    elif thinker and _thinker_api_base:
        args["api_base"] = _thinker_api_base
    elif (synthesis or thinker) and _synthesis_api_base:
        args["api_base"] = _synthesis_api_base

    return args


def build_model(
    *,
    synthesis: bool = False,
    thinker: bool = False,
    vision: bool = False,
) -> LiteLLMModel:
    """Return a Strands LiteLLMModel for Agent(model=...).

    Args:
        synthesis: Use the synthesis model (ADK_SYNTHESIS_MODEL).
        thinker: Use the thinker model (ADK_THINKER_MODEL).
        vision: Use the vision model (ADK_VISION_MODEL) for clip evaluation.
    """
    name = _pick_model_name(synthesis, thinker, vision)
    extra = _pick_extra_body(synthesis, thinker, vision)
    client_args = _pick_client_args(synthesis, thinker, vision)

    # Strip the ``litellm/`` prefix -- ADK routing convention
    if name.startswith("litellm/"):
        name = name[len("litellm/"):]

    config: dict = {}
    if extra:
        config["extra_body"] = extra

    logger.info("Building LiteLLMModel: model_id=%s", name)
    return LiteLLMModel(
        model_id=name,
        client_args=client_args if client_args else None,
        **config,
    )
