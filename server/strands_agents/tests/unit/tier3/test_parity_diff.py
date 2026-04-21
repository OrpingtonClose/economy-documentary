"""Hermetic unit tests for the OTIO parity diff module.

Every test here builds two OTIO timelines in-memory via
``opentimelineio``'s Python API, writes them to a temporary directory,
and runs :func:`compare_timelines` against them.  No external services,
no network, no GPU.

Rationale for round-trip via disk
---------------------------------
``compare_timelines`` takes two paths, not two in-memory timeline
objects, because that's the contract the tier-3 harness uses (both
pipelines write their output OTIO to disk independently).  Exercising
the on-disk path in tests catches load/serialisation bugs that a
pure-in-memory test would miss.
"""

from __future__ import annotations

import pathlib

import opentimelineio as otio
import pytest

from strands_agents.tier3.parity_diff import (
    CLIP_COUNT_TOLERANCE,
    DURATION_TOLERANCE_SEC,
    ParityDiff,
    ParityFinding,
    ParitySeverity,
    compare_timelines,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _clip(name: str, duration_sec: float, rate: float = 24.0) -> otio.schema.Clip:
    """Build a one-clip range with the requested duration."""
    return otio.schema.Clip(
        name=name,
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, rate),
            duration=otio.opentime.RationalTime(
                round(duration_sec * rate), rate
            ),
        ),
    )


def _timeline_from_durations(
    narration_durations: list[float] | None,
    video_durations: list[float] | None,
    name: str = "tl",
) -> otio.schema.Timeline:
    """Assemble an OTIO timeline from two lists of per-clip durations."""
    tl = otio.schema.Timeline(name=name)
    if narration_durations is not None:
        narration = otio.schema.Track(
            name="narration",
            kind=otio.schema.TrackKind.Audio,
        )
        for i, d in enumerate(narration_durations):
            narration.append(_clip(f"narr_{i}", d))
        tl.tracks.append(narration)
    if video_durations is not None:
        video = otio.schema.Track(
            name="video",
            kind=otio.schema.TrackKind.Video,
        )
        for i, d in enumerate(video_durations):
            video.append(_clip(f"vid_{i}", d))
        tl.tracks.append(video)
    return tl


def _write(tl: otio.schema.Timeline, path: pathlib.Path) -> pathlib.Path:
    """Serialise ``tl`` to ``path`` in the JSON OTIO format."""
    otio.adapters.write_to_file(tl, str(path))
    return path


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """Isolated tmp dir per test for OTIO files."""
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path — identical inputs → green diff.
# ---------------------------------------------------------------------------


class TestGreen:
    """Scenarios that must produce a clean parity report."""

    def test_identical_timelines_are_green(self, workspace: pathlib.Path) -> None:
        durations = [6.0, 8.0, 10.0, 12.0]
        s = _write(
            _timeline_from_durations(durations, durations, name="strands"),
            workspace / "strands.otio",
        )
        a = _write(
            _timeline_from_durations(durations, durations, name="adk"),
            workspace / "adk.otio",
        )
        diff = compare_timelines(s, a)
        assert isinstance(diff, ParityDiff)
        assert diff.findings == ()
        assert not diff.is_red
        assert not diff.is_yellow
        assert "all checks green" in diff.format()

    def test_tiny_duration_drift_within_tolerance_is_green(
        self, workspace: pathlib.Path
    ) -> None:
        # 1.5s drift per scene is inside the default 2.0s tolerance.
        s = _write(
            _timeline_from_durations([6.0, 8.0, 10.0], [6.0, 8.0, 10.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([7.5, 9.5, 11.5], [7.5, 9.5, 11.5]),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        # Total drift is 4.5s, which exceeds the per-scene tolerance
        # when summed → yellow total_duration finding is expected, but
        # no RED.  This is the "parity holds per-scene, bulk duration
        # wanders" case.
        assert not diff.is_red
        # Total-duration yellow only fires when no per-scene red was
        # raised; here we expect exactly that.
        assert diff.is_yellow
        cats = {f.category for f in diff.findings}
        assert cats == {"total_duration"}

    def test_video_clip_count_within_tolerance_is_green(
        self, workspace: pathlib.Path
    ) -> None:
        # Default tolerance is 1 extra video clip (framing card edge case).
        s = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0, 2.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0]),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        # Narration matches; video differs by 1 (equals tolerance).
        assert not diff.is_red
        assert not diff.is_yellow


# ---------------------------------------------------------------------------
# Red findings — structural divergence.
# ---------------------------------------------------------------------------


class TestRed:
    """Scenarios that must produce at least one RED finding."""

    def test_scene_count_mismatch_is_red(self, workspace: pathlib.Path) -> None:
        s = _write(
            _timeline_from_durations([6.0, 8.0, 10.0], [6.0, 8.0, 10.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0]),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        assert diff.is_red
        cats = [f.category for f in diff.findings]
        assert "scene_count" in cats
        scene_count_finding = next(
            f for f in diff.findings if f.category == "scene_count"
        )
        assert scene_count_finding.strands_value == 3
        assert scene_count_finding.adk_value == 2

    def test_per_scene_duration_out_of_tolerance_is_red(
        self, workspace: pathlib.Path
    ) -> None:
        # Scene index 1 diverges by 5s, far above the 2s tolerance.
        s = _write(
            _timeline_from_durations([6.0, 8.0, 10.0], [6.0, 8.0, 10.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 13.0, 10.0], [6.0, 13.0, 10.0]),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        assert diff.is_red
        duration_findings = [
            f for f in diff.findings if f.category == "scene_duration"
        ]
        assert len(duration_findings) == 1
        assert "scene[1]" in duration_findings[0].detail
        assert duration_findings[0].strands_value == 8.0
        assert duration_findings[0].adk_value == 13.0

    def test_missing_narration_track_is_red(self, workspace: pathlib.Path) -> None:
        s = _write(
            _timeline_from_durations(None, [6.0, 8.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0]),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        assert diff.is_red
        topology = [f for f in diff.findings if f.category == "topology"]
        assert any(
            "strands timeline missing narration track" in f.detail
            for f in topology
        )

    def test_missing_video_track_on_both_sides_is_red_twice(
        self, workspace: pathlib.Path
    ) -> None:
        s = _write(
            _timeline_from_durations([6.0, 8.0], None),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], None),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        assert diff.is_red
        topology = [f for f in diff.findings if f.category == "topology"]
        # Both timelines flagged for missing video.
        assert len(topology) == 2
        assert all("missing video track" in f.detail for f in topology)


# ---------------------------------------------------------------------------
# Yellow findings — divergence below the red threshold.
# ---------------------------------------------------------------------------


class TestYellow:
    """Scenarios that produce YELLOW (informational) findings only."""

    def test_video_clip_count_exceeds_tolerance_is_yellow(
        self, workspace: pathlib.Path
    ) -> None:
        s = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0, 2.0, 1.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0]),
            workspace / "a.otio",
        )
        diff = compare_timelines(s, a)
        # Narration matches → no RED.  Video clip count differs by 2,
        # above the default tolerance of 1 → YELLOW.
        assert not diff.is_red
        assert diff.is_yellow
        video_finding = next(
            f for f in diff.findings if f.category == "video_clip_count"
        )
        assert video_finding.severity is ParitySeverity.YELLOW


# ---------------------------------------------------------------------------
# Custom tolerance knobs.
# ---------------------------------------------------------------------------


class TestTolerance:
    """Callers can tighten or loosen the parity knobs per-invocation."""

    def test_tightening_duration_tolerance_promotes_to_red(
        self, workspace: pathlib.Path
    ) -> None:
        s = _write(
            _timeline_from_durations([6.0, 8.0, 10.0], [6.0, 8.0, 10.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.5, 8.5, 10.5], [6.5, 8.5, 10.5]),
            workspace / "a.otio",
        )
        # Default tolerance (2.0s) → these are green per-scene (0.5s
        # each) with a yellow total.
        diff_loose = compare_timelines(s, a)
        assert not diff_loose.is_red
        # Tight tolerance (0.1s) → every scene becomes a red finding.
        diff_tight = compare_timelines(s, a, duration_tolerance_sec=0.1)
        assert diff_tight.is_red
        scene_findings = [
            f for f in diff_tight.findings if f.category == "scene_duration"
        ]
        assert len(scene_findings) == 3

    def test_tightening_clip_count_tolerance_promotes_to_yellow(
        self, workspace: pathlib.Path
    ) -> None:
        s = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0, 2.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0]),
            workspace / "a.otio",
        )
        # Default tolerance=1 → diff-of-1 is green.
        assert not compare_timelines(s, a).is_yellow
        # Zero tolerance → diff-of-1 becomes yellow.
        diff = compare_timelines(s, a, clip_count_tolerance=0)
        assert diff.is_yellow


# ---------------------------------------------------------------------------
# Structural errors.
# ---------------------------------------------------------------------------


class TestErrors:
    """Structural errors raise rather than silently passing."""

    def test_missing_strands_file_raises(self, workspace: pathlib.Path) -> None:
        a = _write(
            _timeline_from_durations([6.0], [6.0]),
            workspace / "a.otio",
        )
        with pytest.raises(FileNotFoundError, match="strands timeline not found"):
            compare_timelines(workspace / "nope.otio", a)

    def test_missing_adk_file_raises(self, workspace: pathlib.Path) -> None:
        s = _write(
            _timeline_from_durations([6.0], [6.0]),
            workspace / "s.otio",
        )
        with pytest.raises(FileNotFoundError, match="adk timeline not found"):
            compare_timelines(s, workspace / "nope.otio")

    def test_malformed_otio_raises_value_error(
        self, workspace: pathlib.Path
    ) -> None:
        bad = workspace / "bad.otio"
        bad.write_text("{not valid otio json")
        good = _write(
            _timeline_from_durations([6.0], [6.0]),
            workspace / "good.otio",
        )
        with pytest.raises(ValueError, match="failed to parse timeline"):
            compare_timelines(bad, good)


# ---------------------------------------------------------------------------
# ParityFinding / ParityDiff formatting.
# ---------------------------------------------------------------------------


class TestFormatting:
    """The report must be human-readable and grep-friendly."""

    def test_green_format_mentions_both_paths(self, workspace: pathlib.Path) -> None:
        s = _write(
            _timeline_from_durations([6.0], [6.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0], [6.0]),
            workspace / "a.otio",
        )
        text = compare_timelines(s, a).format()
        assert str(s) in text
        assert str(a) in text
        assert "green" in text.lower()

    def test_red_format_lists_findings(self, workspace: pathlib.Path) -> None:
        s = _write(
            _timeline_from_durations([6.0, 8.0, 10.0], [6.0, 8.0, 10.0]),
            workspace / "s.otio",
        )
        a = _write(
            _timeline_from_durations([6.0, 8.0], [6.0, 8.0]),
            workspace / "a.otio",
        )
        text = compare_timelines(s, a).format()
        assert "RED" in text
        assert "scene_count" in text


# ---------------------------------------------------------------------------
# Constants exposed for external use (workflow step summaries, docs).
# ---------------------------------------------------------------------------


class TestConstants:
    """Guard the public knobs against accidental drift."""

    def test_duration_tolerance_matches_assembly_gate(self) -> None:
        # Assembly-tool's DURATION_TOLERANCE_SEC is the cutover-parity
        # gate; parity_diff inherits the same value.  A drift here would
        # silently decouple the two gates.
        from strands_agents.tools.assembly_tool import (
            DURATION_TOLERANCE_SEC as ASSEMBLY_TOLERANCE,
        )

        assert DURATION_TOLERANCE_SEC == ASSEMBLY_TOLERANCE

    def test_clip_count_tolerance_is_one(self) -> None:
        # Framing-card edge case is the only justification for tolerance>0.
        assert CLIP_COUNT_TOLERANCE == 1

    def test_parity_finding_is_frozen(self) -> None:
        f = ParityFinding(
            category="x",
            severity=ParitySeverity.RED,
            detail="...",
        )
        with pytest.raises(Exception):  # noqa: B017
            f.category = "y"  # type: ignore[misc]
