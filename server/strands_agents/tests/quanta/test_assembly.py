"""Direct-proof tests for :mod:`strands_agents.quanta.assembly`."""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.quanta import check_assembly_inputs


def _scene(sid: str, *, target: float = 5.0) -> dict[str, Any]:
    return {"id": sid, "target_duration_sec": target}


def _clip(sid: str, *, mp4_path: str = "/tmp/clip.mp4") -> dict[str, Any]:
    return {"scene_id": sid, "mp4_path": mp4_path}


_ALIGNMENT: dict[str, Any] = {"segments": [{"start": 0.0, "end": 5.0, "text": "hi"}]}


class TestCheckAssemblyInputs:
    def test_happy_path_returns_none(self) -> None:
        scenes = [_scene("s1"), _scene("s2")]
        clips = [_clip("s1"), _clip("s2")]
        assert check_assembly_inputs(scenes, clips, _ALIGNMENT, "/tmp/t.otio") is None

    def test_empty_scenes_raises(self) -> None:
        with pytest.raises(RuntimeError, match="scenes is empty"):
            check_assembly_inputs([], [_clip("s1")], _ALIGNMENT, "/tmp/t.otio")

    def test_missing_alignment_raises(self) -> None:
        with pytest.raises(RuntimeError, match="whisperx_alignment"):
            check_assembly_inputs([_scene("s1")], [_clip("s1")], {}, "/tmp/t.otio")

    def test_empty_timeline_path_raises(self) -> None:
        with pytest.raises(RuntimeError, match="timeline_path is empty"):
            check_assembly_inputs([_scene("s1")], [_clip("s1")], _ALIGNMENT, "")

    def test_scene_missing_id_raises(self) -> None:
        with pytest.raises(RuntimeError, match="missing id"):
            check_assembly_inputs(
                [{"target_duration_sec": 5.0}], [_clip("s1")], _ALIGNMENT, "/t.otio"
            )

    def test_scene_missing_target_duration_raises(self) -> None:
        with pytest.raises(RuntimeError, match="target_duration_sec"):
            check_assembly_inputs(
                [{"id": "s1"}], [_clip("s1")], _ALIGNMENT, "/t.otio"
            )

    def test_missing_clip_for_scene_raises(self) -> None:
        with pytest.raises(RuntimeError, match="missing clip artifacts"):
            check_assembly_inputs(
                [_scene("s1"), _scene("s2")], [_clip("s1")], _ALIGNMENT, "/t.otio"
            )

    def test_clip_missing_mp4_path_raises(self) -> None:
        with pytest.raises(RuntimeError, match="no mp4_path"):
            check_assembly_inputs(
                [_scene("s1")],
                [{"scene_id": "s1", "mp4_path": ""}],
                _ALIGNMENT,
                "/t.otio",
            )
