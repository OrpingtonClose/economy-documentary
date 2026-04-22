"""Unit tests for :mod:`failure_mode_detectors`.

The detectors themselves are thin wrappers over ``numpy`` and an
``ffmpeg``/``ffprobe`` subprocess; the interesting cases are the
thresholding logic against synthetic frame arrays and the error paths
when ffmpeg/ffprobe produce weird output. Real fixtures are exercised
end-to-end by the ``failure_mode_video`` Experiment — there is no
reason to duplicate that decode here.
"""

from __future__ import annotations

import numpy as np
import pytest

from strands_agents.evals.evaluators.failure_mode_detectors import (
    _parse_rational,
    black_frame_ratio,
    max_interframe_diff,
    white_frame_ratio,
)


class TestBlackFrameRatio:
    """Coverage for :func:`black_frame_ratio`."""

    def test_all_black_returns_one(self) -> None:
        frames = np.zeros((5, 4, 4, 3), dtype=np.uint8)
        assert black_frame_ratio(frames) == 1.0

    def test_all_white_returns_zero(self) -> None:
        frames = np.full((5, 4, 4, 3), 255, dtype=np.uint8)
        assert black_frame_ratio(frames) == 0.0

    def test_threshold_boundary_inclusive(self) -> None:
        frames = np.full((3, 2, 2, 3), 20, dtype=np.uint8)
        assert black_frame_ratio(frames, threshold=20) == 1.0

        frames_above = np.full((3, 2, 2, 3), 21, dtype=np.uint8)
        assert black_frame_ratio(frames_above, threshold=20) == 0.0

    def test_mixed_returns_fraction(self) -> None:
        dark = np.zeros((1, 2, 2, 3), dtype=np.uint8)
        bright = np.full((3, 2, 2, 3), 255, dtype=np.uint8)
        frames = np.concatenate([dark, bright], axis=0)
        assert black_frame_ratio(frames) == pytest.approx(0.25)

    def test_empty_returns_zero(self) -> None:
        frames = np.zeros((0, 4, 4, 3), dtype=np.uint8)
        assert black_frame_ratio(frames) == 0.0


class TestWhiteFrameRatio:
    """Coverage for :func:`white_frame_ratio`."""

    def test_all_white_returns_one(self) -> None:
        frames = np.full((5, 4, 4, 3), 255, dtype=np.uint8)
        assert white_frame_ratio(frames) == 1.0

    def test_all_black_returns_zero(self) -> None:
        frames = np.zeros((5, 4, 4, 3), dtype=np.uint8)
        assert white_frame_ratio(frames) == 0.0

    def test_threshold_boundary_inclusive(self) -> None:
        frames = np.full((3, 2, 2, 3), 235, dtype=np.uint8)
        assert white_frame_ratio(frames, threshold=235) == 1.0

        frames_below = np.full((3, 2, 2, 3), 234, dtype=np.uint8)
        assert white_frame_ratio(frames_below, threshold=235) == 0.0

    def test_mixed_returns_fraction(self) -> None:
        bright = np.full((2, 2, 2, 3), 255, dtype=np.uint8)
        mid = np.full((2, 2, 2, 3), 128, dtype=np.uint8)
        frames = np.concatenate([bright, mid], axis=0)
        assert white_frame_ratio(frames) == pytest.approx(0.5)

    def test_empty_returns_zero(self) -> None:
        frames = np.zeros((0, 4, 4, 3), dtype=np.uint8)
        assert white_frame_ratio(frames) == 0.0


class TestMaxInterframeDiff:
    """Coverage for :func:`max_interframe_diff`."""

    def test_identical_frames_returns_zero(self) -> None:
        frame = np.full((4, 4, 3), 128, dtype=np.uint8)
        frames = np.stack([frame] * 5, axis=0)
        assert max_interframe_diff(frames) == 0.0

    def test_single_frame_returns_zero(self) -> None:
        frames = np.zeros((1, 4, 4, 3), dtype=np.uint8)
        assert max_interframe_diff(frames) == 0.0

    def test_empty_returns_zero(self) -> None:
        frames = np.zeros((0, 4, 4, 3), dtype=np.uint8)
        assert max_interframe_diff(frames) == 0.0

    def test_constant_shift_reports_shift(self) -> None:
        # Frame 0 all 10, frame 1 all 20 → mean abs diff = 10 across
        # every channel and pixel.
        f0 = np.full((4, 4, 3), 10, dtype=np.uint8)
        f1 = np.full((4, 4, 3), 20, dtype=np.uint8)
        frames = np.stack([f0, f1], axis=0)
        assert max_interframe_diff(frames) == pytest.approx(10.0)

    def test_takes_max_not_mean_across_pairs(self) -> None:
        # Three frames: pair (0,1) diff=0, pair (1,2) diff=50. ``max``
        # surfaces the glitch even when the rest of the clip is clean.
        f0 = np.full((4, 4, 3), 100, dtype=np.uint8)
        f1 = np.full((4, 4, 3), 100, dtype=np.uint8)
        f2 = np.full((4, 4, 3), 150, dtype=np.uint8)
        frames = np.stack([f0, f1, f2], axis=0)
        assert max_interframe_diff(frames) == pytest.approx(50.0)


class TestParseRational:
    """Coverage for the ffprobe rational parser."""

    def test_simple_fraction(self) -> None:
        assert _parse_rational("30/1") == 30.0

    def test_fractional_ratio(self) -> None:
        assert _parse_rational("30000/1001") == pytest.approx(29.97, abs=0.01)

    def test_bare_number(self) -> None:
        assert _parse_rational("24") == 24.0

    def test_zero_denominator_defaults_to_one(self) -> None:
        assert _parse_rational("15/0") == 15.0

    def test_garbage_returns_zero(self) -> None:
        assert _parse_rational("not-a-rational") == 0.0
