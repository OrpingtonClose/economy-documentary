"""Unit tests for slice 9f-multiscene-prod playground request wiring.

Slice 9f extends ``StartPipelineRunRequest`` and
:func:`playground._normalise_pipeline_request` to accept an optional
``num_scenes`` override. These tests pin the validation surface so a
future refactor can't silently drop the override or accept out-of-range
values.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from playground import (
    _PIPELINE_MAX_NUM_SCENES,
    _PIPELINE_MIN_NUM_SCENES,
    StartPipelineRunRequest,
    _normalise_pipeline_request,
)


class TestNormalisePipelineRequestNumScenes:
    """``num_scenes`` flows through normalisation untouched when valid."""

    def test_default_is_none(self) -> None:
        request = StartPipelineRunRequest(topic="topic", target_duration_sec=60)
        topic, duration, language, num_scenes = _normalise_pipeline_request(
            request
        )
        assert num_scenes is None
        assert topic == "topic"
        assert duration == 60
        assert language == "en"

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
    def test_in_range_passes_through(self, n: int) -> None:
        request = StartPipelineRunRequest(
            topic="topic", target_duration_sec=60, num_scenes=n
        )
        _, _, _, num_scenes = _normalise_pipeline_request(request)
        assert num_scenes == n

    def test_below_min_raises_400(self) -> None:
        request = StartPipelineRunRequest(
            topic="topic", target_duration_sec=60, num_scenes=0
        )
        with pytest.raises(HTTPException) as exc:
            _normalise_pipeline_request(request)
        assert exc.value.status_code == 400
        assert "num_scenes" in str(exc.value.detail)

    def test_above_max_raises_400(self) -> None:
        request = StartPipelineRunRequest(
            topic="topic",
            target_duration_sec=60,
            num_scenes=_PIPELINE_MAX_NUM_SCENES + 1,
        )
        with pytest.raises(HTTPException) as exc:
            _normalise_pipeline_request(request)
        assert exc.value.status_code == 400
        assert "num_scenes" in str(exc.value.detail)

    def test_clamp_constants_match_demo(self) -> None:
        """Playground bounds match ``pipeline_live_demo`` clamp constants.

        AGENTS.md "Timing stage" — both layers must agree on the soft
        cap, otherwise a request that passes validation can still be
        clamped silently downstream.
        """
        from strands_agents.playground import pipeline_live_demo as demo

        assert _PIPELINE_MIN_NUM_SCENES == demo._DEMO_MIN_SCENES
        assert _PIPELINE_MAX_NUM_SCENES == demo._DEMO_MAX_SCENES
