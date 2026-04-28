"""Unit tests for slice 9g-assembly — wire real ``launch_assembly``.

Slice 9g-assembly composes a single master ``.mp4`` from per-scene
audio (Qwen3-TTS ``.wav``) + video (LTX-2.3 ``.mp4``) artifacts that
the slice-9d-wire dispatchers persist under ``run_dir/artifacts/``.

The slice spans three surfaces:

* The placeholder ``launch_assembly`` signature must accept the new
  ``clip_artifacts`` + ``target_duration_sec`` kwargs the orchestrator
  passes (slice 9g) while staying backward-compatible with the legacy
  ``timeline`` / ``output_path`` shape.
* The real overlay (:func:`strands_agents._real_assembly_tools
  .build_real_assembly_tools`) is env-gated on ``ENABLE_REAL_ASSEMBLY``
  and replaces the placeholder by ``.name`` match through
  :func:`strands_agents.playground.pipeline_live_real_workers
  .build_real_worker_tools`.
* The pure-Python core (:func:`compose_master_mp4`) implements the
  two-stage ffmpeg pipeline (per-scene mux + concat) by delegating to
  injectable helpers — so unit tests can stub ffmpeg without a real
  binary on the box.

These tests pin down all three surfaces with anti-drift assertions so
a future placeholder swap, env-gate flip, or helper signature change
is caught at CI time, not at render time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from strands_agents import _placeholders, _real_assembly_tools
from strands_agents._real_assembly_tools import (
    apply_real_assembly_overrides,
    build_real_assembly_tools,
    compose_master_mp4,
    make_real_assembly_tool,
)
from strands_agents._real_assembly_tools import (
    _parse_helper_payload,
    _resolve_one,
)
from strands_agents.playground import pipeline_live_demo as demo
from strands_agents.playground.pipeline_live_real_workers import (
    build_real_worker_tools,
)


# ---------------------------------------------------------------------------
# Helper stubs — record calls and return synthesised JSON envelopes
# ---------------------------------------------------------------------------


class _MuxRecorder:
    """Stub for ``mux_audio_video`` that records calls + writes the output."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail_on = fail_on

    def __call__(self, audio: str, video: str, output: str) -> str:
        self.calls.append((audio, video, output))
        if self._fail_on and self._fail_on in (audio, video):
            return json.dumps({"error": "synthetic mux failure"})
        Path(output).write_bytes(b"MUXED")
        return json.dumps({"output_path": output, "duration_sec": 4.0})


class _ConcatRecorder:
    """Stub for ``concat_clips`` that records calls + writes the output."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def __call__(self, paths_csv: str, output: str) -> str:
        self.calls.append((paths_csv, output))
        if self._fail:
            return json.dumps({"error": "synthetic concat failure"})
        Path(output).write_bytes(b"MASTER")
        return json.dumps({"output_path": output, "duration_sec": 12.0})


def _seed_scene(
    artifacts: Path,
    scene_id: str,
    *,
    with_audio: bool = True,
    token: str | None = None,
) -> tuple[Path, Path | None]:
    """Write fake mp4 (+ optional wav) files for ``scene_id``.

    When ``token`` is set, mimics the dispatcher layout
    (``{scene_id}-{token}.{suffix}``) so the resolver's glob branch is
    exercised. Otherwise writes the canonical ``{scene_id}.{suffix}``
    layout.
    """
    artifacts.mkdir(parents=True, exist_ok=True)
    suffix_mp4 = f"{scene_id}-{token}.mp4" if token else f"{scene_id}.mp4"
    mp4 = artifacts / suffix_mp4
    mp4.write_bytes(b"VIDEO")
    wav: Path | None = None
    if with_audio:
        suffix_wav = f"{scene_id}-{token}.wav" if token else f"{scene_id}.wav"
        wav = artifacts / suffix_wav
        wav.write_bytes(b"AUDIO")
    return mp4, wav


# ---------------------------------------------------------------------------
# Placeholder backward-compat (anti-drift on signature)
# ---------------------------------------------------------------------------


class TestPlaceholderLaunchAssemblySignature:
    """The placeholder must accept the new slice-9g shape AND legacy args."""

    def test_accepts_clip_artifacts_and_target_duration(self) -> None:
        out = _placeholders.launch_assembly.invoke(
            {
                "clip_artifacts": [{"scene_id": "s1", "duration_sec": 4.0}],
                "target_duration_sec": 60.0,
            }
        )
        assert out["tool"] == "launch_assembly"
        assert out["args"]["clip_artifacts"] == [
            {"scene_id": "s1", "duration_sec": 4.0}
        ]
        assert out["args"]["target_duration_sec"] == 60.0

    def test_accepts_legacy_timeline_output_path(self) -> None:
        out = _placeholders.launch_assembly.invoke(
            {"timeline": {"tracks": []}, "output_path": "/tmp/out.mp4"}
        )
        assert out["tool"] == "launch_assembly"
        assert out["args"]["timeline"] == {"tracks": []}
        assert out["args"]["output_path"] == "/tmp/out.mp4"

    def test_accepts_no_args_at_all(self) -> None:
        # All four args are optional — calling with nothing must not raise.
        out = _placeholders.launch_assembly.invoke({})
        assert out["tool"] == "launch_assembly"
        assert out["args"]["clip_artifacts"] == []


# ---------------------------------------------------------------------------
# _resolve_one — covers explicit / canonical / glob / missing branches
# ---------------------------------------------------------------------------


class TestResolveOne:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        explicit = tmp_path / "elsewhere" / "scene.mp4"
        explicit.parent.mkdir()
        explicit.write_bytes(b"X")
        # Even if a canonical file exists, explicit wins.
        (artifacts / "scene1.mp4").write_bytes(b"Y")
        result = _resolve_one(artifacts, "scene1", str(explicit), "mp4")
        assert result == explicit

    def test_canonical_layout(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        canonical = artifacts / "scene1.mp4"
        canonical.write_bytes(b"X")
        result = _resolve_one(artifacts, "scene1", None, "mp4")
        assert result == canonical

    def test_glob_layout_matches_dispatcher_token(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        # ``_persist_artifact`` writes ``{scene_id}-{8hex}.{suffix}``.
        tokened = artifacts / "scene1-deadbeef.mp4"
        tokened.write_bytes(b"X")
        result = _resolve_one(artifacts, "scene1", None, "mp4")
        assert result == tokened

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        assert _resolve_one(artifacts, "scene1", None, "mp4") is None


# ---------------------------------------------------------------------------
# _parse_helper_payload — JSON / dict / error branches
# ---------------------------------------------------------------------------


class TestParseHelperPayload:
    def test_dict_passthrough(self) -> None:
        assert _parse_helper_payload({"output_path": "/x"}) == {"output_path": "/x"}

    def test_json_string(self) -> None:
        assert _parse_helper_payload('{"a": 1}') == {"a": 1}

    def test_invalid_json_returns_error(self) -> None:
        result = _parse_helper_payload("not json")
        assert "error" in result

    def test_unexpected_type_returns_error(self) -> None:
        result = _parse_helper_payload(42)
        assert "error" in result


# ---------------------------------------------------------------------------
# compose_master_mp4 — happy path + every error branch
# ---------------------------------------------------------------------------


class TestComposeMasterMp4:
    def test_full_path_two_scenes(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        _seed_scene(artifacts, "scene1")
        _seed_scene(artifacts, "scene2")
        mux = _MuxRecorder()
        concat = _ConcatRecorder()
        result = compose_master_mp4(
            [
                {"scene_id": "scene1", "duration_sec": 4.0},
                {"scene_id": "scene2", "duration_sec": 6.0},
            ],
            artifacts,
            mux_audio_video_helper=mux,
            concat_clips_helper=concat,
        )
        assert result["scene_count"] == 2
        assert result["muxed_count"] == 2
        assert result["duration_sec_estimate"] == 10.0
        assert result["master_mp4_path"] == str(artifacts / "master.mp4")
        assert len(mux.calls) == 2
        assert len(concat.calls) == 1
        # concat input list must be the muxed clips (both ended in .muxed.mp4).
        for path in result["concat_inputs"]:
            assert path.endswith(".muxed.mp4")

    def test_video_only_passthrough_when_audio_missing(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        _seed_scene(artifacts, "scene1", with_audio=False)
        mux = _MuxRecorder()
        concat = _ConcatRecorder()
        result = compose_master_mp4(
            [{"scene_id": "scene1", "duration_sec": 4.0}],
            artifacts,
            mux_audio_video_helper=mux,
            concat_clips_helper=concat,
        )
        # mux helper must NOT be called when audio is absent.
        assert mux.calls == []
        assert result["muxed_count"] == 0
        # Concat input is the raw mp4, not a muxed file.
        assert result["concat_inputs"][0].endswith("scene1.mp4")

    def test_glob_matches_dispatcher_token_layout(self, tmp_path: Path) -> None:
        # Mimics the audio/video dispatchers' ``{scene_id}-{8hex}.{suffix}``
        # layout — proves the resolver finds them without explicit paths.
        artifacts = tmp_path / "artifacts"
        _seed_scene(artifacts, "scene1", token="abc12345")
        mux = _MuxRecorder()
        concat = _ConcatRecorder()
        result = compose_master_mp4(
            [{"scene_id": "scene1", "duration_sec": 4.0}],
            artifacts,
            mux_audio_video_helper=mux,
            concat_clips_helper=concat,
        )
        assert result["muxed_count"] == 1
        # mux saw the glob-matched paths.
        audio_arg, video_arg, _ = mux.calls[0]
        assert "scene1-abc12345.mp4" in video_arg
        assert "scene1-abc12345.wav" in audio_arg

    def test_missing_video_raises(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        # No mp4 written — must hard fail.
        with pytest.raises(ValueError, match="no resolvable mp4_path"):
            compose_master_mp4(
                [{"scene_id": "scene1"}],
                artifacts,
                mux_audio_video_helper=_MuxRecorder(),
                concat_clips_helper=_ConcatRecorder(),
            )

    def test_empty_clip_artifacts_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            compose_master_mp4(
                [],
                tmp_path,
                mux_audio_video_helper=_MuxRecorder(),
                concat_clips_helper=_ConcatRecorder(),
            )

    def test_entry_missing_scene_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="missing scene_id"):
            compose_master_mp4(
                [{"duration_sec": 4.0}],
                tmp_path,
                mux_audio_video_helper=_MuxRecorder(),
                concat_clips_helper=_ConcatRecorder(),
            )

    def test_entry_not_a_dict_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a dict"):
            compose_master_mp4(
                ["scene1"],  # type: ignore[list-item]
                tmp_path,
                mux_audio_video_helper=_MuxRecorder(),
                concat_clips_helper=_ConcatRecorder(),
            )

    def test_mux_helper_error_propagates(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        mp4, _ = _seed_scene(artifacts, "scene1")
        mux = _MuxRecorder(fail_on=str(mp4))
        with pytest.raises(ValueError, match="mux failed"):
            compose_master_mp4(
                [{"scene_id": "scene1"}],
                artifacts,
                mux_audio_video_helper=mux,
                concat_clips_helper=_ConcatRecorder(),
            )

    def test_concat_helper_error_propagates(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        _seed_scene(artifacts, "scene1")
        with pytest.raises(ValueError, match="concat failed"):
            compose_master_mp4(
                [{"scene_id": "scene1"}],
                artifacts,
                mux_audio_video_helper=_MuxRecorder(),
                concat_clips_helper=_ConcatRecorder(fail=True),
            )


# ---------------------------------------------------------------------------
# make_real_assembly_tool — name + signature + envelope
# ---------------------------------------------------------------------------


class TestMakeRealAssemblyTool:
    def test_tool_name_is_launch_assembly(self, tmp_path: Path) -> None:
        tool = make_real_assembly_tool(
            tmp_path,
            mux_audio_video_helper=_MuxRecorder(),
            concat_clips_helper=_ConcatRecorder(),
        )
        assert tool.name == "launch_assembly"

    def test_tool_args_match_placeholder(self, tmp_path: Path) -> None:
        # The real tool's arg names must match the placeholder so the
        # demo's scripted ``AIMessage`` works against either tool.
        tool = make_real_assembly_tool(
            tmp_path,
            mux_audio_video_helper=_MuxRecorder(),
            concat_clips_helper=_ConcatRecorder(),
        )
        real_args = set(tool.args.keys())
        placeholder_args = set(_placeholders.launch_assembly.args.keys())
        assert real_args == placeholder_args

    def test_returns_envelope_with_master_path(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        _seed_scene(artifacts, "scene1")
        tool = make_real_assembly_tool(
            tmp_path,
            mux_audio_video_helper=_MuxRecorder(),
            concat_clips_helper=_ConcatRecorder(),
        )
        out = tool.invoke(
            {
                "clip_artifacts": [{"scene_id": "scene1", "duration_sec": 4.0}],
                "target_duration_sec": 60.0,
            }
        )
        assert out["status"] == "ok"
        assert out["engine"] == "ffmpeg"
        assert out["args"]["master_mp4_path"] == str(artifacts / "master.mp4")
        assert out["args"]["scene_count"] == 1

    def test_empty_clip_artifacts_returns_envelope_not_raise(
        self, tmp_path: Path
    ) -> None:
        # The real tool fail-soft for the empty-list case so the
        # orchestrator's scripted brain (which can't introspect the
        # error) still gets a stable envelope shape.
        tool = make_real_assembly_tool(
            tmp_path,
            mux_audio_video_helper=_MuxRecorder(),
            concat_clips_helper=_ConcatRecorder(),
        )
        out = tool.invoke({"clip_artifacts": []})
        assert out["status"] == "ok"
        assert out["args"]["scene_count"] == 0
        assert out["args"]["master_mp4_path"] is None
        assert "error" in out["args"]

    def test_compose_failure_surfaces_in_envelope(self, tmp_path: Path) -> None:
        # Real failure (missing video) must surface as ``error`` in the
        # envelope — the demo's downstream observers expect a stable
        # dict shape, not an exception.
        tool = make_real_assembly_tool(
            tmp_path,
            mux_audio_video_helper=_MuxRecorder(),
            concat_clips_helper=_ConcatRecorder(),
        )
        out = tool.invoke({"clip_artifacts": [{"scene_id": "missing"}]})
        assert out["args"]["master_mp4_path"] is None
        assert "no resolvable mp4_path" in out["args"]["error"]


# ---------------------------------------------------------------------------
# build_real_assembly_tools — env gate
# ---------------------------------------------------------------------------


class TestBuildRealAssemblyTools:
    def test_returns_empty_when_gate_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ENABLE_REAL_ASSEMBLY", raising=False)
        assert build_real_assembly_tools(run_dir=tmp_path) == {}

    def test_returns_tool_when_gate_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_ASSEMBLY", "1")
        overrides = build_real_assembly_tools(run_dir=tmp_path)
        assert "launch_assembly" in overrides
        assert overrides["launch_assembly"].name == "launch_assembly"

    def test_explicit_enabled_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_ASSEMBLY", "1")
        # explicit ``enabled=False`` wins.
        assert build_real_assembly_tools(run_dir=tmp_path, enabled=False) == {}

    def test_falls_back_when_helpers_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate the ImportError branch: monkeypatch
        # ``_default_helpers`` to raise. Builder must return ``{}``,
        # not raise — keeps the placeholder fallback intact.
        monkeypatch.setenv("ENABLE_REAL_ASSEMBLY", "1")

        def _boom() -> Any:
            raise ImportError("synthetic")

        monkeypatch.setattr(_real_assembly_tools, "_default_helpers", _boom)
        assert build_real_assembly_tools(run_dir=tmp_path) == {}


# ---------------------------------------------------------------------------
# apply_real_assembly_overrides — by-name swap, order preservation
# ---------------------------------------------------------------------------


class TestApplyRealAssemblyOverrides:
    def test_empty_overrides_passthrough(self) -> None:
        base = [_placeholders.launch_assembly, _placeholders.launch_b2_sync]
        out = apply_real_assembly_overrides(base, {})
        assert out == base
        # Returns a new list, not the same object.
        assert out is not base

    def test_swap_by_name_preserves_order(self, tmp_path: Path) -> None:
        real = make_real_assembly_tool(
            tmp_path,
            mux_audio_video_helper=_MuxRecorder(),
            concat_clips_helper=_ConcatRecorder(),
        )
        base = [
            _placeholders.generate_scenario,
            _placeholders.launch_assembly,
            _placeholders.launch_b2_sync,
        ]
        out = apply_real_assembly_overrides(base, {"launch_assembly": real})
        assert out[0] is _placeholders.generate_scenario
        assert out[1] is real
        assert out[2] is _placeholders.launch_b2_sync


# ---------------------------------------------------------------------------
# build_real_worker_tools integration — assembly overlay merges with audio/video
# ---------------------------------------------------------------------------


class TestBuildRealWorkerToolsAssembly:
    """``build_real_worker_tools`` always installs the audio + video
    dispatch overlays (URL resolved lazily via on-demand worker
    provisioning). The assembly overlay is independently togglable via
    ``enable_real_assembly`` / ``ENABLE_REAL_ASSEMBLY``.
    """

    def test_assembly_appears_with_gate_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_ASSEMBLY", "1")
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        overrides = build_real_worker_tools(tmp_path, enable_real_b2=False)
        # Audio + video overlays are always installed (on-demand
        # provisioning); ``ENABLE_REAL_ASSEMBLY=1`` adds the assembly
        # overlay on top.
        assert "launch_assembly" in overrides
        assert "launch_audio_render" in overrides
        assert "launch_visual_production" in overrides

    def test_assembly_absent_when_gate_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ENABLE_REAL_ASSEMBLY", raising=False)
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        overrides = build_real_worker_tools(tmp_path, enable_real_b2=False)
        # Audio + video overlays still present (on-demand provisioning);
        # only the assembly overlay is gated off.
        assert "launch_assembly" not in overrides
        assert "launch_audio_render" in overrides
        assert "launch_visual_production" in overrides

    def test_assembly_merges_with_audio_video_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_ASSEMBLY", "1")
        overrides = build_real_worker_tools(
            tmp_path,
            audio_worker_url="http://tts.example/",
            video_worker_url="http://ltx.example/",
            enable_real_b2=False,
        )
        assert {
            "launch_audio_render",
            "launch_visual_production",
            "launch_assembly",
        }.issubset(overrides.keys())


# ---------------------------------------------------------------------------
# Demo orchestrator passes ``clip_artifacts`` (anti-drift on the demo script)
# ---------------------------------------------------------------------------


class TestDemoPassesClipArtifacts:
    def test_demo_script_emits_clip_artifacts_arg(self) -> None:
        # The scripted ``AIMessage`` for ``launch_assembly`` must
        # include ``clip_artifacts`` so the slice-9g real overlay has
        # the per-scene scene_ids it needs to resolve artifact paths.
        # Anti-drift: a future demo refactor that drops the kwarg
        # silently degrades the real assembly path back to the
        # placeholder envelope.
        script = demo._demo_chat_script(
            topic="Federal Reserve",
            target_duration_sec=60,
            language="en",
            num_scenes=3,
        )
        seen_assembly_call = False
        for msg in script:
            for tool_call in getattr(msg, "tool_calls", []) or []:
                if tool_call.get("name") == "launch_assembly":
                    seen_assembly_call = True
                    args = tool_call.get("args", {})
                    assert "clip_artifacts" in args
                    assert isinstance(args["clip_artifacts"], list)
                    assert len(args["clip_artifacts"]) == 3
                    for entry in args["clip_artifacts"]:
                        assert "scene_id" in entry
                        assert "duration_sec" in entry
                    assert "target_duration_sec" in args
                    assert args["target_duration_sec"] == 60.0
        assert seen_assembly_call, "demo script must emit a launch_assembly tool call"
