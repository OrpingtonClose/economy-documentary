"""Unit tests for slice 9h-b2-publish — wire real ``launch_b2_sync``.

Slice 9h-b2-publish honours AGENTS.md hard invariant 6 (*every
artifact to B2 immediately*) by uploading every artifact in a
``run_dir`` (per-scene wav + mp4, scenario JSON, master mp4) to the
B2 checkpoint store and returning a manifest the orchestrator hands
back to the user.

The slice spans three surfaces, each pinned here with anti-drift
assertions:

* The placeholder ``launch_b2_sync`` signature accepts the new
  ``master_mp4_path`` / ``clip_artifacts`` / ``scenario_path`` /
  ``run_id`` / ``revision_tag`` kwargs while remaining backward-
  compatible with the pre-9h ``artifact_path`` shape.
* The pure-Python core (:func:`sync_run_artifacts`) uploads in a
  fixed, observable order (scenario → per-scene wav + mp4 → master)
  and handles missing-artifact / malformed-entry / no-such-file
  branches.
* The env-gated overlay (:func:`build_real_b2_tools`) returns an
  empty dict when ``ENABLE_REAL_B2`` is off, a real tool when on,
  and tolerates the optional store factory (in-memory by default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from strands_agents import _placeholders
from strands_agents._real_b2_tools import (
    _resolve_clip_paths,
    _resolve_enabled_flag,
    apply_real_b2_overrides,
    build_real_b2_tools,
    make_real_b2_sync_tool,
    sync_run_artifacts,
)
from strands_agents.b2_checkpoint import InMemoryB2CheckpointStore
from strands_agents.b2_checkpoint.errors import StaleRevisionError
from strands_agents.playground import pipeline_live_demo as demo
from strands_agents.playground.pipeline_live_real_workers import (
    build_real_worker_tools,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Return a fresh run-dir with an ``artifacts/`` subdir."""
    (tmp_path / "artifacts").mkdir()
    return tmp_path


@pytest.fixture
def store() -> InMemoryB2CheckpointStore:
    """Return a fresh in-memory checkpoint store."""
    return InMemoryB2CheckpointStore()


def _write_artifact(run_dir: Path, name: str, payload: bytes) -> Path:
    """Write ``payload`` to ``run_dir/artifacts/<name>`` and return the path."""
    p = run_dir / "artifacts" / name
    p.write_bytes(payload)
    return p


# ---------------------------------------------------------------------------
# 1. Placeholder signature — backward compat + new args
# ---------------------------------------------------------------------------


class TestPlaceholderLaunchB2SyncSignature:
    """The placeholder must accept legacy + slice-9h kwargs both ways."""

    def test_placeholder_accepts_legacy_artifact_path_only(self) -> None:
        """Pre-9h callers passing only ``artifact_path`` keep working."""
        envelope = _placeholders.launch_b2_sync.invoke(
            {"artifact_path": "/runs/r1/master.mp4"}
        )
        assert envelope["tool"] == "launch_b2_sync"
        assert envelope["args"]["artifact_path"] == "/runs/r1/master.mp4"
        assert envelope["args"]["clip_artifacts"] == []
        assert envelope["args"]["master_mp4_path"] is None

    def test_placeholder_accepts_full_slice_9h_payload(self) -> None:
        """Slice 9h callers passing every kwarg get them echoed back."""
        clips = [{"scene_id": "scene-0", "duration_sec": 5.0}]
        envelope = _placeholders.launch_b2_sync.invoke(
            {
                "artifact_path": "/runs/r1/master.mp4",
                "master_mp4_path": "/runs/r1/master.mp4",
                "clip_artifacts": clips,
                "scenario_path": "/runs/r1/scenario.json",
                "run_id": "r1",
                "revision_tag": "r0007",
            }
        )
        args = envelope["args"]
        assert args["master_mp4_path"] == "/runs/r1/master.mp4"
        assert args["clip_artifacts"] == clips
        assert args["scenario_path"] == "/runs/r1/scenario.json"
        assert args["run_id"] == "r1"
        assert args["revision_tag"] == "r0007"

    def test_placeholder_works_with_no_args_at_all(self) -> None:
        """A no-arg call still returns the standard envelope shape."""
        envelope = _placeholders.launch_b2_sync.invoke({})
        assert envelope["tool"] == "launch_b2_sync"
        assert envelope["args"]["artifact_path"] == ""
        assert envelope["args"]["clip_artifacts"] == []


# ---------------------------------------------------------------------------
# 2. Path resolver — explicit / canonical / glob / missing
# ---------------------------------------------------------------------------


class TestResolveClipPaths:
    """``_resolve_clip_paths`` mirrors slice-9g's three-tier resolution."""

    def test_resolver_prefers_explicit_absolute_path(self, run_dir: Path) -> None:
        explicit_mp4 = _write_artifact(run_dir, "elsewhere.mp4", b"x")
        explicit_wav = _write_artifact(run_dir, "elsewhere.wav", b"y")
        mp4, wav = _resolve_clip_paths(
            run_dir / "artifacts",
            "scene-0",
            str(explicit_mp4),
            str(explicit_wav),
        )
        assert mp4 == explicit_mp4
        assert wav == explicit_wav

    def test_resolver_falls_back_to_canonical_layout(self, run_dir: Path) -> None:
        mp4 = _write_artifact(run_dir, "scene-0.mp4", b"video")
        wav = _write_artifact(run_dir, "scene-0.wav", b"audio")
        got_mp4, got_wav = _resolve_clip_paths(
            run_dir / "artifacts", "scene-0", None, None
        )
        assert got_mp4 == mp4
        assert got_wav == wav

    def test_resolver_falls_back_to_glob_with_mtime(self, run_dir: Path) -> None:
        """Most recent ``{scene_id}-*.{suffix}`` wins (retry beats failure)."""
        old = _write_artifact(run_dir, "scene-0-aaaaaaaa.mp4", b"old")
        # Bump mtime on the new file so the test is deterministic.
        import os
        import time

        time.sleep(0.01)
        new = _write_artifact(run_dir, "scene-0-bbbbbbbb.mp4", b"new")
        os.utime(new, None)
        mp4, _ = _resolve_clip_paths(run_dir / "artifacts", "scene-0", None, None)
        assert mp4 == new
        assert mp4 != old

    def test_resolver_returns_none_when_nothing_matches(self, run_dir: Path) -> None:
        mp4, wav = _resolve_clip_paths(
            run_dir / "artifacts", "scene-missing", None, None
        )
        assert mp4 is None
        assert wav is None


# ---------------------------------------------------------------------------
# 3. ``sync_run_artifacts`` core — happy paths + error branches
# ---------------------------------------------------------------------------


class TestSyncRunArtifacts:
    """The pure-Python core uploads in fixed order and fails closed."""

    def test_happy_path_uploads_scenario_clips_master_in_order(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        _write_artifact(run_dir, "scene-0.wav", b"wav-0")
        _write_artifact(run_dir, "scene-0.mp4", b"mp4-0")
        _write_artifact(run_dir, "scene-1.wav", b"wav-1")
        _write_artifact(run_dir, "scene-1.mp4", b"mp4-1")
        master = _write_artifact(run_dir, "master.mp4", b"master")
        scenario = run_dir / "scenario.json"
        scenario.write_bytes(b'{"scenes":[]}')

        result = sync_run_artifacts(
            run_dir=run_dir,
            run_id="run-9h-1",
            revision_tag="r0001",
            store=store,
            master_mp4_path=str(master),
            clip_artifacts=[
                {"scene_id": "scene-0"},
                {"scene_id": "scene-1"},
            ],
            scenario_path=str(scenario),
        )

        kinds_in_order = [entry["kind"] for entry in result["manifest"]]
        # Scenario first, per-scene wav+mp4, master last.
        assert kinds_in_order == [
            "scene_json",
            "audio_wav",
            "video_mp4",
            "audio_wav",
            "video_mp4",
            "master_mp4",
        ]
        assert result["uploaded_count"] == 6
        assert result["kinds"]["audio_wav"] == 2
        assert result["kinds"]["video_mp4"] == 2
        assert result["kinds"]["master_mp4"] == 1
        assert result["run_id"] == "run-9h-1"
        assert result["revision_tag"] == "r0001"

    def test_skips_scenes_with_no_resolvable_artifacts(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        _write_artifact(run_dir, "scene-0.mp4", b"mp4-only")
        # No wav for scene-0 — should silently skip the audio upload.
        result = sync_run_artifacts(
            run_dir=run_dir,
            run_id="run-9h-2",
            revision_tag="r0001",
            store=store,
            clip_artifacts=[{"scene_id": "scene-0"}, {"scene_id": "scene-missing"}],
        )
        assert result["uploaded_count"] == 1
        assert result["kinds"] == {"video_mp4": 1}

    def test_master_only_works_without_clips_or_scenario(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        result = sync_run_artifacts(
            run_dir=run_dir,
            run_id="run-9h-3",
            revision_tag="r0001",
            store=store,
            master_mp4_path=str(master),
        )
        assert result["uploaded_count"] == 1
        assert result["manifest"][0]["kind"] == "master_mp4"

    def test_explicit_paths_in_clip_artifacts_resolve(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        wav = _write_artifact(run_dir, "elsewhere-0.wav", b"wav")
        mp4 = _write_artifact(run_dir, "elsewhere-0.mp4", b"mp4")
        result = sync_run_artifacts(
            run_dir=run_dir,
            run_id="run-9h-4",
            revision_tag="r0001",
            store=store,
            clip_artifacts=[
                {
                    "scene_id": "scene-0",
                    "wav_path": str(wav),
                    "mp4_path": str(mp4),
                }
            ],
        )
        assert result["uploaded_count"] == 2

    def test_missing_master_raises(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        with pytest.raises(ValueError, match="master_mp4_path"):
            sync_run_artifacts(
                run_dir=run_dir,
                run_id="run-9h-5",
                revision_tag="r0001",
                store=store,
                master_mp4_path=str(run_dir / "no-such.mp4"),
            )

    def test_missing_scenario_raises(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        with pytest.raises(ValueError, match="scenario_path"):
            sync_run_artifacts(
                run_dir=run_dir,
                run_id="run-9h-6",
                revision_tag="r0001",
                store=store,
                scenario_path=str(run_dir / "no-such.json"),
            )

    def test_non_dict_clip_entry_raises(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        with pytest.raises(ValueError, match="not a dict"):
            sync_run_artifacts(
                run_dir=run_dir,
                run_id="run-9h-7",
                revision_tag="r0001",
                store=store,
                clip_artifacts=["scene-0"],  # type: ignore[list-item]
            )

    def test_clip_entry_missing_scene_id_raises(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        with pytest.raises(ValueError, match="missing scene_id"):
            sync_run_artifacts(
                run_dir=run_dir,
                run_id="run-9h-8",
                revision_tag="r0001",
                store=store,
                clip_artifacts=[{"duration_sec": 5.0}],
            )

    def test_stale_revision_propagates_from_store(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        sync_run_artifacts(
            run_dir=run_dir,
            run_id="run-9h-9",
            revision_tag="r0005",
            store=store,
            master_mp4_path=str(master),
        )
        with pytest.raises(StaleRevisionError):
            sync_run_artifacts(
                run_dir=run_dir,
                run_id="run-9h-9",
                revision_tag="r0001",  # older — must fail
                store=store,
                master_mp4_path=str(master),
            )

    def test_returned_manifest_entries_carry_b2_key_and_sha256(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        result = sync_run_artifacts(
            run_dir=run_dir,
            run_id="run-9h-10",
            revision_tag="r0001",
            store=store,
            master_mp4_path=str(master),
        )
        entry = result["manifest"][0]
        assert "b2_key" in entry and entry["b2_key"].startswith("runs/run-9h-10/")
        assert "sha256" in entry and len(entry["sha256"]) == 64
        assert entry["size_bytes"] == 6  # b"master"


# ---------------------------------------------------------------------------
# 4. ``make_real_b2_sync_tool`` — LangChain @tool wrapper
# ---------------------------------------------------------------------------


class TestMakeRealB2SyncTool:
    """The factory returns a tool the orchestrator can swap in by name."""

    def test_tool_name_matches_placeholder(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        tool = make_real_b2_sync_tool(run_dir, store=store)
        assert tool.name == "launch_b2_sync"
        # Same name as the placeholder so apply_real_*_overrides
        # swaps cleanly by name.
        assert tool.name == _placeholders.launch_b2_sync.name

    def test_tool_returns_ok_envelope_on_success(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        tool = make_real_b2_sync_tool(run_dir, store=store)
        envelope = tool.invoke(
            {
                "master_mp4_path": str(master),
                "run_id": "run-9h-tool-1",
                "revision_tag": "r0001",
            }
        )
        assert envelope["status"] == "ok"
        assert envelope["engine"] == "b2-checkpoint"
        assert envelope["args"]["uploaded_count"] == 1
        assert envelope["args"]["manifest"][0]["kind"] == "master_mp4"

    def test_tool_returns_error_envelope_on_failure(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        tool = make_real_b2_sync_tool(run_dir, store=store)
        envelope = tool.invoke(
            {
                "master_mp4_path": "/no/such/master.mp4",
                "run_id": "run-9h-tool-2",
                "revision_tag": "r0001",
            }
        )
        assert envelope["status"] == "error"
        assert envelope["engine"] == "b2-checkpoint"
        assert "master_mp4_path" in envelope["args"]["error"]
        assert envelope["args"]["uploaded_count"] == 0

    def test_tool_uses_legacy_artifact_path_when_master_unset(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        tool = make_real_b2_sync_tool(run_dir, store=store)
        envelope = tool.invoke(
            {
                "artifact_path": str(master),
                "run_id": "run-9h-tool-3",
                "revision_tag": "r0001",
            }
        )
        assert envelope["status"] == "ok"
        assert envelope["args"]["uploaded_count"] == 1

    def test_tool_falls_back_to_default_run_id(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        tool = make_real_b2_sync_tool(
            run_dir, store=store, default_run_id="default-run"
        )
        envelope = tool.invoke({"master_mp4_path": str(master)})
        assert envelope["args"]["run_id"] == "default-run"

    def test_tool_falls_back_to_run_dir_name_when_no_default_run_id(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        tool = make_real_b2_sync_tool(run_dir, store=store)
        envelope = tool.invoke({"master_mp4_path": str(master)})
        assert envelope["args"]["run_id"] == run_dir.name


# ---------------------------------------------------------------------------
# 5. ``build_real_b2_tools`` — env gate
# ---------------------------------------------------------------------------


class TestBuildRealB2Tools:
    """The overlay builder honours the ``ENABLE_REAL_B2`` env gate."""

    def test_gate_off_returns_empty(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ENABLE_REAL_B2", raising=False)
        assert build_real_b2_tools(run_dir=run_dir) == {}

    def test_gate_on_returns_launch_b2_sync(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_B2", "1")
        # B2_BACKEND defaults to "memory" so no live creds needed.
        monkeypatch.delenv("B2_BACKEND", raising=False)
        overrides = build_real_b2_tools(run_dir=run_dir)
        assert set(overrides.keys()) == {"launch_b2_sync"}
        assert overrides["launch_b2_sync"].name == "launch_b2_sync"

    def test_explicit_enabled_overrides_env(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_B2", "1")
        assert build_real_b2_tools(run_dir=run_dir, enabled=False) == {}
        monkeypatch.setenv("ENABLE_REAL_B2", "0")
        overrides = build_real_b2_tools(run_dir=run_dir, enabled=True)
        assert "launch_b2_sync" in overrides

    def test_injected_store_used_when_provided(
        self,
        run_dir: Path,
        store: InMemoryB2CheckpointStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        master = _write_artifact(run_dir, "master.mp4", b"master")
        overrides = build_real_b2_tools(run_dir=run_dir, enabled=True, store=store)
        envelope = overrides["launch_b2_sync"].invoke(
            {
                "master_mp4_path": str(master),
                "run_id": "run-injected",
                "revision_tag": "r0001",
            }
        )
        assert envelope["status"] == "ok"
        # Same store instance — manifest should now have one entry.
        assert len(store.list_for_run("run-injected").entries) == 1


# ---------------------------------------------------------------------------
# 6. ``_resolve_enabled_flag`` — truthy values only
# ---------------------------------------------------------------------------


class TestResolveEnabledFlag:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy_values(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_REAL_B2", value)
        assert _resolve_enabled_flag(None) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_falsy_values(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_REAL_B2", value)
        assert _resolve_enabled_flag(None) is False

    def test_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_REAL_B2", "1")
        assert _resolve_enabled_flag(False) is False
        assert _resolve_enabled_flag(True) is True


# ---------------------------------------------------------------------------
# 7. ``apply_real_b2_overrides`` — swap by ``.name`` match
# ---------------------------------------------------------------------------


class TestApplyRealB2Overrides:
    def test_empty_overrides_returns_input_copy(self) -> None:
        base = [_placeholders.launch_b2_sync, _placeholders.launch_assembly]
        out = apply_real_b2_overrides(base, {})
        assert out == base
        assert out is not base  # new list

    def test_swap_by_name_preserves_order(
        self, run_dir: Path, store: InMemoryB2CheckpointStore
    ) -> None:
        real = make_real_b2_sync_tool(run_dir, store=store)
        base = [
            _placeholders.launch_assembly,
            _placeholders.launch_b2_sync,
            _placeholders.await_tasks,
        ]
        out = apply_real_b2_overrides(base, {"launch_b2_sync": real})
        assert out[0] is _placeholders.launch_assembly
        assert out[1] is real
        assert out[2] is _placeholders.await_tasks

    def test_unmatched_overrides_pass_through(self) -> None:
        base = [_placeholders.launch_b2_sync]
        # Override under a name that doesn't exist in base — base is
        # returned with no swap.
        out = apply_real_b2_overrides(base, {"some-other-tool": object()})
        assert out == base


# ---------------------------------------------------------------------------
# 8. ``build_real_worker_tools`` integration — slice 9h overlay merges
# ---------------------------------------------------------------------------


class TestBuildRealWorkerToolsB2Integration:
    def test_b2_overlay_merges_when_gate_on(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        monkeypatch.delenv("ENABLE_REAL_ASSEMBLY", raising=False)
        monkeypatch.setenv("ENABLE_REAL_B2", "1")
        overrides = build_real_worker_tools(run_dir=run_dir)
        assert "launch_b2_sync" in overrides

    def test_b2_overlay_skipped_when_gate_off(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        monkeypatch.delenv("ENABLE_REAL_ASSEMBLY", raising=False)
        monkeypatch.delenv("ENABLE_REAL_B2", raising=False)
        overrides = build_real_worker_tools(run_dir=run_dir)
        assert "launch_b2_sync" not in overrides

    def test_b2_overlay_coexists_with_assembly_overlay(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_ASSEMBLY", "1")
        monkeypatch.setenv("ENABLE_REAL_B2", "1")
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        overrides = build_real_worker_tools(run_dir=run_dir)
        assert {"launch_assembly", "launch_b2_sync"} <= set(overrides.keys())

    def test_explicit_enable_real_b2_arg_overrides_env(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_REAL_B2", "1")
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        monkeypatch.delenv("ENABLE_REAL_ASSEMBLY", raising=False)
        overrides = build_real_worker_tools(run_dir=run_dir, enable_real_b2=False)
        assert "launch_b2_sync" not in overrides


# ---------------------------------------------------------------------------
# 9. Demo script anti-drift — keep emitting slice-9h kwargs
# ---------------------------------------------------------------------------


class TestDemoScriptB2Args:
    """The demo's scripted ``AIMessage`` must keep emitting slice 9h kwargs."""

    def _find_b2_call(self, script: list[Any]) -> dict[str, Any]:
        """Return the args dict of the ``launch_b2_sync`` tool call."""
        for msg in script:
            tool_calls = getattr(msg, "tool_calls", None) or []
            for call in tool_calls:
                if call.get("name") == "launch_b2_sync":
                    return call["args"]
        raise AssertionError("launch_b2_sync not found in demo script")

    def test_demo_emits_master_mp4_path(self) -> None:
        script = demo._demo_chat_script(
            topic="Test", target_duration_sec=15, language="en"
        )
        args = self._find_b2_call(script)
        assert "master_mp4_path" in args
        assert args["master_mp4_path"]

    def test_demo_emits_clip_artifacts(self) -> None:
        script = demo._demo_chat_script(
            topic="Test", target_duration_sec=15, language="en"
        )
        args = self._find_b2_call(script)
        assert isinstance(args["clip_artifacts"], list)
        assert len(args["clip_artifacts"]) >= 1
        assert all("scene_id" in c for c in args["clip_artifacts"])

    def test_demo_keeps_artifact_path_for_back_compat(self) -> None:
        script = demo._demo_chat_script(
            topic="Test", target_duration_sec=15, language="en"
        )
        args = self._find_b2_call(script)
        # Backward-compat: pre-9h placeholder path stays present.
        assert args.get("artifact_path")

    def test_demo_emits_revision_tag(self) -> None:
        script = demo._demo_chat_script(
            topic="Test", target_duration_sec=15, language="en"
        )
        args = self._find_b2_call(script)
        assert args.get("revision_tag") == "r0001"

    def test_demo_clip_artifacts_match_assembly_clip_artifacts(self) -> None:
        """Both ``launch_assembly`` and ``launch_b2_sync`` see the same clips."""
        script = demo._demo_chat_script(
            topic="Test", target_duration_sec=20, language="en"
        )
        b2_args = self._find_b2_call(script)
        for msg in script:
            for call in getattr(msg, "tool_calls", None) or []:
                if call.get("name") == "launch_assembly":
                    assembly_clips = call["args"]["clip_artifacts"]
                    assert b2_args["clip_artifacts"] == assembly_clips
                    return
        raise AssertionError("launch_assembly call not found")
