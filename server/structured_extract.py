"""Structured extraction from raw LLM text via instructor + DeepSeek v4-flash.

Every place that currently does ``json.loads()`` on LLM output should migrate
here.  The contract is:

    Raw text  →  instructor + Pydantic  →  strongly-typed Python object

No more ``dict[str, Any]``.  No more ``json.loads(str(raw))``.
"""

from __future__ import annotations

import logging
import os
from typing import TypeVar

import instructor
from openai import OpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=BaseModel)

# ---------------------------------------------------------------------------
# Shared DeepSeek v4-flash client (instructor-wrapped)
# ---------------------------------------------------------------------------

_DEEPSEEK_API_KEY = ""
_deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
if os.path.exists(_deepseek_key_path):
    with open(_deepseek_key_path) as _f:
        _DEEPSEEK_API_KEY = _f.read().strip()

_DS_CLIENT: instructor.Instructor | None = None


def _ds_client() -> instructor.Instructor:
    global _DS_CLIENT
    if _DS_CLIENT is None:
        _DS_CLIENT = instructor.from_openai(
            OpenAI(
                api_key=_DEEPSEEK_API_KEY,
                base_url="https://api.deepseek.com/v1",
            ),
            mode=instructor.Mode.JSON,
        )
    return _DS_CLIENT


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(
    response_model: type[_ModelT],
    raw_text: str,
    system_prompt: str = "Extract structured data from the raw text.",
    temperature: float = 0.0,
    max_retries: int = 3,
) -> _ModelT:
    """Parse *raw_text* into a strongly-typed *response_model*.

    This is the single point of contact for turning free-form LLM output
    (markdown fences, preamble, rambling) into validated Pydantic objects.

    Uses instructor reask validation: if the model produces malformed output,
    it is re-prompted with validation errors up to max_retries times.
    """
    client = _ds_client()
    return client.chat.completions.create(
        model="deepseek-v4-flash",
        response_model=response_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Raw text:\n\n{raw_text}"},
        ],
        temperature=temperature,
        max_retries=max_retries,
    )
