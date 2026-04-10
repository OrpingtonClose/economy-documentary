"""
Shared model configuration for all ADK agents.

For native Gemini models (``gemini-*``), returns a plain string so ADK
uses its built-in handler.  For LiteLLM-routed models, returns a
``LiteLlm`` instance with vendor-specific ``extra_body`` forwarding
(e.g. Venice ``venice_parameters``).

Model roles:
  - ADK_MODEL: primary model for tool-capable agents
  - ADK_SYNTHESIS_MODEL: model for synthesis/evaluation agents
  - ADK_THINKER_MODEL: model for deep reasoning agents (content analyst)
  - ADK_VISION_MODEL: model for clip coherence evaluation (needs vision)
"""

from __future__ import annotations

import json
import os
from typing import Union

from google.adk.models import LiteLlm

# -- Model names ---------------------------------------------------------------
_raw_model = os.environ.get("ADK_MODEL", "litellm/openai/gpt-4o")
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


def build_model(
    *,
    parallel_tool_calls: bool = True,
    synthesis: bool = False,
    thinker: bool = False,
    vision: bool = False,
) -> Union[str, LiteLlm]:
    """Return the model for ADK Agent(model=...).

    * Native Gemini models (``gemini-*``) -> plain string (ADK native path).
    * Everything else -> ``LiteLlm`` with vendor-specific ``extra_body``.

    Args:
        parallel_tool_calls: Whether the model may emit multiple tool calls
            in a single response.
        synthesis: Use the synthesis model (ADK_SYNTHESIS_MODEL).
        thinker: Use the thinker model (ADK_THINKER_MODEL).
        vision: Use the vision model (ADK_VISION_MODEL) for clip evaluation.
    """
    if vision:
        name = ADK_VISION_MODEL_NAME
    elif thinker:
        name = ADK_THINKER_MODEL_NAME
    elif synthesis:
        name = ADK_SYNTHESIS_MODEL_NAME
    else:
        name = ADK_MODEL_NAME

    # Determine extra_body based on model role
    if vision and _has_separate_vision:
        extra = {}
    elif thinker and _has_separate_thinker and _thinker_api_base:
        extra = _thinker_extra_body
    elif (synthesis or thinker) and _has_separate_synthesis and _synthesis_api_base:
        extra = _synthesis_extra_body
    else:
        extra = _extra_body

    # Strip the ``litellm/`` prefix -- ADK routing convention
    if name.startswith("litellm/"):
        name = name[len("litellm/"):]

    # Native Gemini models use ADK's built-in handler
    if name.startswith("gemini"):
        return name

    kwargs: dict = {"extra_body": extra}
    if not parallel_tool_calls:
        kwargs["parallel_tool_calls"] = False

    # Per-model api_key and api_base for separate providers
    if vision and _vision_api_key:
        kwargs["api_key"] = _vision_api_key
    elif thinker and _thinker_api_key:
        kwargs["api_key"] = _thinker_api_key
    elif (synthesis or thinker) and _synthesis_api_key:
        kwargs["api_key"] = _synthesis_api_key

    if vision and _vision_api_base:
        kwargs["api_base"] = _vision_api_base
    elif thinker and _thinker_api_base:
        kwargs["api_base"] = _thinker_api_base
    elif (synthesis or thinker) and _synthesis_api_base:
        kwargs["api_base"] = _synthesis_api_base

    return LiteLlm(model=name, **kwargs)
