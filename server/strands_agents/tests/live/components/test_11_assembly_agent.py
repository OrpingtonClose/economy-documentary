"""Hermetic proof of robustness for Component 11 (assembly-agent).

The assembly leaf is deterministic — no LLM — so its clear-cut
contracts are about refusing to assemble a broken pipeline.  The
invariants pinned here come directly from
``server/callbacks/strict_assembler.py`` and the module docstring:

1. Every scene has a matching clip artifact (1:1 by id).
2. Every clip carries a non-empty ``mp4_path``.
3. WhisperX alignment is required (empty dict = contract violation).
4. OTIO compliance violations abort the tool — no partial state.
5. Final-duration deviation > :data:`DURATION_TOLERANCE_SEC` aborts.
6. Empty B2 URL from uploader aborts.
7. Happy path: all invariants satisfied → returns full
   ``final_output`` payload with ``mp4_path``, ``b2_url``,
   ``duration_sec``, ``scene_count``, and ``otio_path``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from strands_agents.tools.assembly_tool import (
    DURATION_TOLERANCE_SEC,
    assemble_final_cut,
    reset_assembly_helpers,
    set_assembly_helpers,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _scene(sid: str, target_duration_sec: float = 5.0) -> dict[str, Any]:
    return {
        "id": sid,
        "scene_num": int(sid.split("-")[-1]) if "-" in sid else 1,
        "target_duration_sec": target_duration_sec,
    }


def _clip(scene_id: str, mp4_path: str = "/tmp/clip.mp4") -> dict[str, Any]:
    return {"scene_id": scene_id, "mp4_path": mp4_path}


def _alignment() -> dict[str, Any]:
    return {
        "segments": [
            {"scene_id": "s-1", "words": [{"word": "hello", "start": 0.0, "end": 1.0}]}
        ]
    }


def _pass_validator(_otio_path: str) -> tuple[bool, list[dict[str, Any]]]:
    return True, []


def _fail_validator(
    _otio_path: str,
) -> tuple[bool, list[dict[str, Any]]]:
    return False, [{"code": "gap_in_audio_track", "scene_id": "s-2"}]


def _compose_ok(**kwargs: Any) -> str:
    # Write an empty .otio placeholder to make the helper non-trivial.
    output_path = kwargs["output_path"]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("{}")
    return output_path


def _render_ok(*, otio_path: str, output_dir: str) -> str:
    # Produce no actual mp4 — the probe will short-circuit and the
    # tool treats the probe as a skip so a stubbed render is allowed.
    return os.path.join(output_dir, "final.mp4")


def _upload_ok(_local_path: str) -> str:
    return "b2://final-cut/documentary.mp4"


def _upload_empty(_local_path: str) -> str:
    return ""


@pytest.fixture()
def _wire_happy_helpers() -> None:
    set_assembly_helpers(
        compose_timeline=_compose_ok,
        validate_timeline=_pass_validator,
        render_final=_render_ok,
        upload_to_b2=_upload_ok,
    )
    yield
    reset_assembly_helpers()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_full_final_output(
    _wire_happy_helpers: None,
    tmp_path,
) -> None:
    scenes = [
        _scene("s-1", 5.0),
        _scene("s-2", 7.0),
        _scene("s-3", 3.0),
    ]
    clips = [
        _clip("s-1"),
        _clip("s-2"),
        _clip("s-3"),
    ]
    result = assemble_final_cut.__wrapped__(
        scenes=scenes,
        clip_artifacts=clips,
        whisperx_alignment=_alignment(),
        timeline_path=str(tmp_path / "timeline.otio"),
        output_dir=str(tmp_path / "out"),
    )
    assert result["scene_count"] == 3
    assert result["duration_sec"] == pytest.approx(15.0, abs=0.001)
    assert result["b2_url"] == "b2://final-cut/documentary.mp4"
    assert result["mp4_path"].endswith("final.mp4")
    assert result["otio_path"].endswith("final.otio")


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


def test_missing_clip_for_scene_aborts_assembly(
    _wire_happy_helpers: None,
    tmp_path,
) -> None:
    scenes = [_scene("s-1"), _scene("s-2"), _scene("s-3")]
    clips = [_clip("s-1"), _clip("s-3")]  # s-2 missing
    with pytest.raises(RuntimeError, match="missing clip artifacts"):
        assemble_final_cut.__wrapped__(
            scenes=scenes,
            clip_artifacts=clips,
            whisperx_alignment=_alignment(),
            timeline_path=str(tmp_path / "timeline.otio"),
            output_dir=str(tmp_path / "out"),
        )


def test_clip_without_mp4_path_aborts_assembly(
    _wire_happy_helpers: None,
    tmp_path,
) -> None:
    scenes = [_scene("s-1"), _scene("s-2")]
    clips = [_clip("s-1"), {"scene_id": "s-2", "mp4_path": ""}]
    with pytest.raises(RuntimeError, match="has no mp4_path"):
        assemble_final_cut.__wrapped__(
            scenes=scenes,
            clip_artifacts=clips,
            whisperx_alignment=_alignment(),
            timeline_path=str(tmp_path / "timeline.otio"),
            output_dir=str(tmp_path / "out"),
        )


def test_empty_whisperx_alignment_aborts_assembly(
    _wire_happy_helpers: None,
    tmp_path,
) -> None:
    with pytest.raises(RuntimeError, match="audio contract violated"):
        assemble_final_cut.__wrapped__(
            scenes=[_scene("s-1")],
            clip_artifacts=[_clip("s-1")],
            whisperx_alignment={},
            timeline_path=str(tmp_path / "timeline.otio"),
            output_dir=str(tmp_path / "out"),
        )


def test_otio_compliance_failure_aborts_assembly(tmp_path) -> None:
    set_assembly_helpers(
        compose_timeline=_compose_ok,
        validate_timeline=_fail_validator,
        render_final=_render_ok,
        upload_to_b2=_upload_ok,
    )
    try:
        with pytest.raises(RuntimeError, match="OTIO compliance failed"):
            assemble_final_cut.__wrapped__(
                scenes=[_scene("s-1"), _scene("s-2")],
                clip_artifacts=[_clip("s-1"), _clip("s-2")],
                whisperx_alignment=_alignment(),
                timeline_path=str(tmp_path / "timeline.otio"),
                output_dir=str(tmp_path / "out"),
            )
    finally:
        reset_assembly_helpers()


def test_empty_b2_url_aborts_assembly(tmp_path) -> None:
    set_assembly_helpers(
        compose_timeline=_compose_ok,
        validate_timeline=_pass_validator,
        render_final=_render_ok,
        upload_to_b2=_upload_empty,
    )
    try:
        with pytest.raises(RuntimeError, match="empty URL"):
            assemble_final_cut.__wrapped__(
                scenes=[_scene("s-1")],
                clip_artifacts=[_clip("s-1")],
                whisperx_alignment=_alignment(),
                timeline_path=str(tmp_path / "timeline.otio"),
                output_dir=str(tmp_path / "out"),
            )
    finally:
        reset_assembly_helpers()


def test_duration_outside_tolerance_aborts_assembly(tmp_path) -> None:
    """Probe an actual mp4 whose duration is far from the target."""
    # Produce a tiny real mp4 via ffmpeg so _probe_duration returns a
    # real value; if ffmpeg/ffprobe are missing, skip — this is the
    # only test that needs them.
    import shutil

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe required for duration-probe test")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    bogus_mp4 = out_dir / "final.mp4"
    # 1s of black video — far from the 30s target below.
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(bogus_mp4),
        ],
        check=True,
        capture_output=True,
    )

    def _render_bogus(*, otio_path: str, output_dir: str) -> str:
        return str(bogus_mp4)

    set_assembly_helpers(
        compose_timeline=_compose_ok,
        validate_timeline=_pass_validator,
        render_final=_render_bogus,
        upload_to_b2=_upload_ok,
    )
    try:
        # target = 30s, actual ~1s → far outside tolerance.
        scenes = [_scene("s-1", 10.0), _scene("s-2", 10.0), _scene("s-3", 10.0)]
        clips = [_clip("s-1"), _clip("s-2"), _clip("s-3")]
        with pytest.raises(RuntimeError, match="deviates from target"):
            assemble_final_cut.__wrapped__(
                scenes=scenes,
                clip_artifacts=clips,
                whisperx_alignment=_alignment(),
                timeline_path=str(tmp_path / "timeline.otio"),
                output_dir=str(out_dir),
            )
    finally:
        reset_assembly_helpers()


def test_duration_tolerance_constant_is_two_seconds() -> None:
    """Pin the tolerance constant so a silent loosening is caught."""
    assert DURATION_TOLERANCE_SEC == 2.0
