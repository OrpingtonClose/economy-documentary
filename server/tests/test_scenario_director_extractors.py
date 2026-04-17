"""
Unit tests for scenario_director internal extractors.

Specifically verifies that ``_extract_style_lock`` pulls the style_lock
JSON object out of accumulated generator output even when a visual_style
object precedes it and the scenes array follows it.

Run from ``server/`` with::

    poetry run pytest tests/test_scenario_director_extractors.py
"""

from __future__ import annotations

import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from agents.scenario_director import _extract_style_lock


def test_extract_style_lock_basic():
    text = """
    Here is my plan.

    ```json
    {"style": "documentary", "avoid": ["cartoon"]}
    ```

    And the style lock:
    ```json
    {
      "dominant_style": "cinematic_documentary",
      "forbidden_styles": ["anime", "watercolor"],
      "positive_fragment": "cinematic documentary, 4k",
      "negative_fragment": "anime, cartoon"
    }
    ```

    Scenes: [{"scene_num": 1, "title": "x"}]
    """
    sl = _extract_style_lock(text)
    assert sl is not None
    assert sl["dominant_style"] == "cinematic_documentary"
    assert "anime" in sl["forbidden_styles"]
    assert sl["positive_fragment"]
    assert sl["negative_fragment"]


def test_extract_style_lock_skips_visual_style_object():
    # visual_style uses "style"/"avoid" keys, which must be skipped
    # so we don't accidentally return it as the style_lock.
    text = (
        '{"style": "documentary", "avoid": ["cartoon"]}\n'
        '{"dominant_style": "realistic_3d", "forbidden_styles": []}\n'
    )
    sl = _extract_style_lock(text)
    assert sl is not None
    assert sl["dominant_style"] == "realistic_3d"
    assert "style" not in sl  # we did NOT pick up the visual_style object


def test_extract_style_lock_missing_returns_none():
    text = "no JSON here at all"
    assert _extract_style_lock(text) is None


def test_extract_style_lock_returns_none_when_only_visual_style_present():
    text = '{"style": "documentary", "avoid": ["cartoon"]}'
    # Does NOT have dominant_style / forbidden_styles, so style_lock is absent.
    assert _extract_style_lock(text) is None


def test_extract_style_lock_handles_nested_braces_in_scenes():
    # Style_lock comes BEFORE a scenes array containing nested braces —
    # brace-matching must not be fooled by nested content inside other
    # objects after the style_lock.
    text = (
        '{"style": "doc", "avoid": []}\n'
        '{"dominant_style": "painterly", "forbidden_styles": ["anime"]}\n'
        '[{"scene_num": 1, "voices": [{"voice": "V1", "text": "hi"}]}]'
    )
    sl = _extract_style_lock(text)
    assert sl is not None
    assert sl["dominant_style"] == "painterly"


def test_extract_style_lock_empty_input():
    assert _extract_style_lock("") is None
    assert _extract_style_lock(None) is None  # type: ignore[arg-type]


def test_extract_style_lock_tolerates_stray_brace_in_prose():
    # Regression: a stray unmatched '{' in prose before the JSON used to
    # abort the whole scan, because the brace-matching loop never found
    # depth==0 and returned None immediately.  Now we skip past the stray
    # brace and continue searching.
    text = (
        'The PAG { area is critical. Here is the lock:\n'
        '{"dominant_style": "cinematic_documentary", "forbidden_styles": ["anime"]}'
    )
    sl = _extract_style_lock(text)
    assert sl is not None
    assert sl["dominant_style"] == "cinematic_documentary"


def test_extract_style_lock_tolerates_stray_brace_in_string_like_prose():
    # Another stray-brace variant: an f-string-like "{topic}" in prose.
    text = (
        'Generator wrote something about {topic} and then:\n'
        '{"dominant_style": "painterly", "forbidden_styles": []}'
    )
    sl = _extract_style_lock(text)
    assert sl is not None
    assert sl["dominant_style"] == "painterly"
