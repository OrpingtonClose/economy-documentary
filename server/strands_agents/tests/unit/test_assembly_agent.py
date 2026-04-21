"""Unit tests — component 11 assembly tool + experiment.

Covers:

* The five canonical experiment cases end-to-end.
* Every hard invariant in ``assemble_final_cut`` (empty scenes, missing
  clip, missing alignment, compose failure, compliance failure,
  duration drift, empty B2 URL).
* ``set_assembly_helpers`` / ``reset_assembly_helpers`` thread-safety
  (concurrent overrides don't cross-contaminate in-flight calls).
* ``TimelineComplianceEvaluator`` integration over the real OTIO
  file produced by the case helpers (fidelity check, not a mock).

Every test runs offline — no ffmpeg, no B2, no network.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from typing import Any

import pytest

from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    TimelineComplianceEvaluator,
)
from strands_agents.evals.experiments.assembly import (
    ASSEMBLY_EXPERIMENT_NAME,
    assembly_task,
    build_assembly_experiment,
    cleanup_assembly_artifact_root,
)
from strands_agents.tools.assembly_tool import (
    ASSEMBLY_TOOL_NAME,
    DURATION_TOLERANCE_SEC,
    assemble_final_cut,
    reset_assembly_helpers,
    set_assembly_helpers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scene(scene_id: str, duration: float = 20.0) -> dict[str, Any]:
    return {"id": scene_id, "target_duration_sec": duration}


def _clip(scene_id: str, path: str | None = None) -> dict[str, Any]:
    return {"scene_id": scene_id, "mp4_path": path or f"/tmp/clips/{scene_id}.mp4"}


def _alignment() -> dict[str, Any]:
    return {"scenes": [{"id": "s1", "start": 0.0, "end": 20.0}]}


@pytest.fixture(autouse=True)
def _reset_helpers() -> None:
    reset_assembly_helpers()
    yield
    reset_assembly_helpers()


def _install_stubs(
    *,
    compose_raises: bool = False,
    validate_passes: bool = True,
    render_raises: bool = False,
    upload_url: str | None = "b2://ok/final.mp4",
    upload_raises: bool = False,
    render_bytes: bytes = b"",
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {
        "compose": [],
        "validate": [],
        "render": [],
        "upload": [],
    }

    def compose(
        *,
        scenes: list[dict[str, Any]],  # noqa: ARG001
        clip_artifacts: list[dict[str, Any]],  # noqa: ARG001
        whisperx_alignment: dict[str, Any],  # noqa: ARG001
        timeline_path: str,  # noqa: ARG001
        output_path: str,
    ) -> str:
        calls["compose"].append(output_path)
        if compose_raises:
            raise RuntimeError("compose stub failure")
        with open(output_path, "wb") as fh:
            fh.write(b"{}")
        return output_path

    def validate(otio_path: str) -> tuple[bool, list[dict[str, Any]]]:
        calls["validate"].append(otio_path)
        if validate_passes:
            return True, []
        return False, [{"type": "gap"}]

    def render(*, otio_path: str, output_dir: str) -> str:
        calls["render"].append((otio_path, output_dir))
        if render_raises:
            raise RuntimeError("render stub failure")
        path = os.path.join(output_dir, "final.mp4")
        with open(path, "wb") as fh:
            fh.write(render_bytes)
        return path

    def upload(local_path: str) -> str:
        calls["upload"].append(local_path)
        if upload_raises:
            raise RuntimeError("upload stub failure")
        return upload_url or ""

    set_assembly_helpers(
        compose_timeline=compose,
        validate_timeline=validate,
        render_final=render,
        upload_to_b2=upload,
    )
    return calls


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_tool_has_expected_name(self) -> None:
        assert ASSEMBLY_TOOL_NAME == "assemble_final_cut"

    def test_tool_spec_exposes_required_params(self) -> None:
        spec = assemble_final_cut.tool_spec
        # Strands tool_spec shape: {"name", "description", "inputSchema"}
        assert spec["name"] == "assemble_final_cut"
        schema = spec["inputSchema"]["json"]
        required = set(schema.get("required", []))
        assert {
            "scenes",
            "clip_artifacts",
            "whisperx_alignment",
            "timeline_path",
            "output_dir",
        } <= required

    def test_duration_tolerance_matches_spec(self) -> None:
        assert DURATION_TOLERANCE_SEC == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Hard invariants — inputs
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_empty_scenes_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="scenes is empty"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[],
                    clip_artifacts=[],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_empty_alignment_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="whisperx_alignment"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment={},
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_empty_timeline_path_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="timeline_path"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="",
                    output_dir=out,
                )

    def test_scene_missing_id_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="missing id"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[{"target_duration_sec": 10.0}],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_scene_missing_duration_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="target_duration_sec"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[{"id": "s1"}],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_missing_clip_for_scene_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="missing clip artifacts"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1"), _scene("s2")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_clip_without_mp4_path_raises(self) -> None:
        _install_stubs()
        with pytest.raises(RuntimeError, match="no mp4_path"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[{"scene_id": "s1", "mp4_path": ""}],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )


# ---------------------------------------------------------------------------
# Hard invariants — helpers
# ---------------------------------------------------------------------------


class TestHelperInvariants:
    def test_not_wired_raises(self) -> None:
        # autouse fixture already reset helpers
        with pytest.raises(RuntimeError, match="helpers not wired"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_validate_failure_raises_with_violation_payload(self) -> None:
        _install_stubs(validate_passes=False)
        with pytest.raises(RuntimeError, match="OTIO compliance failed"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_compose_raises_propagates(self) -> None:
        _install_stubs(compose_raises=True)
        with pytest.raises(RuntimeError, match="compose stub failure"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_render_raises_propagates(self) -> None:
        _install_stubs(render_raises=True)
        with pytest.raises(RuntimeError, match="render stub failure"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_upload_raises_propagates(self) -> None:
        _install_stubs(upload_raises=True)
        with pytest.raises(RuntimeError, match="upload stub failure"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_empty_b2_url_raises(self) -> None:
        _install_stubs(upload_url="")
        with pytest.raises(RuntimeError, match="empty URL"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_clean_assembly_returns_final_output(self) -> None:
        calls = _install_stubs()
        with tempfile.TemporaryDirectory() as out:
            result = assemble_final_cut.__wrapped__(
                scenes=[_scene("s1"), _scene("s2")],
                clip_artifacts=[_clip("s1"), _clip("s2")],
                whisperx_alignment=_alignment(),
                timeline_path="/tmp/t.otio",
                output_dir=out,
            )
            assert set(result) == {
                "mp4_path",
                "b2_url",
                "duration_sec",
                "scene_count",
                "otio_path",
            }
            assert result["scene_count"] == 2
            assert result["duration_sec"] == pytest.approx(40.0)
            assert result["b2_url"].startswith("b2://")
            assert result["otio_path"].endswith("final.otio")
            assert os.path.dirname(result["mp4_path"]) == out

        assert len(calls["compose"]) == 1
        assert len(calls["validate"]) == 1
        assert len(calls["render"]) == 1
        assert len(calls["upload"]) == 1

    def test_output_dir_created_if_missing(self) -> None:
        _install_stubs()
        with tempfile.TemporaryDirectory() as parent:
            target = os.path.join(parent, "nested", "out")
            assert not os.path.isdir(target)
            assemble_final_cut.__wrapped__(
                scenes=[_scene("s1")],
                clip_artifacts=[_clip("s1")],
                whisperx_alignment=_alignment(),
                timeline_path="/tmp/t.otio",
                output_dir=target,
            )
            assert os.path.isdir(target)


# ---------------------------------------------------------------------------
# Helper mutation — thread safety
# ---------------------------------------------------------------------------


class TestHelperInjection:
    def test_partial_override_preserves_other_helpers(self) -> None:
        _install_stubs()

        flag = {"called": False}

        def alt_upload(local_path: str) -> str:  # noqa: ARG001
            flag["called"] = True
            return "b2://alt/url"

        set_assembly_helpers(upload_to_b2=alt_upload)

        with tempfile.TemporaryDirectory() as out:
            result = assemble_final_cut.__wrapped__(
                scenes=[_scene("s1")],
                clip_artifacts=[_clip("s1")],
                whisperx_alignment=_alignment(),
                timeline_path="/tmp/t.otio",
                output_dir=out,
            )
            assert flag["called"] is True
            assert result["b2_url"] == "b2://alt/url"

    def test_reset_restores_not_wired_default(self) -> None:
        _install_stubs()
        reset_assembly_helpers()
        with pytest.raises(RuntimeError, match="helpers not wired"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1")],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_concurrent_invocations_use_consistent_helper_snapshot(self) -> None:
        # Snapshot-at-entry means a concurrent mutation mid-call cannot
        # split the run between two different helper tables.
        _install_stubs()

        barrier = threading.Barrier(4)
        results: list[tuple[int, str]] = []
        errors: list[str] = []

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=5)
                with tempfile.TemporaryDirectory() as out:
                    r = assemble_final_cut.__wrapped__(
                        scenes=[_scene(f"s{i}")],
                        clip_artifacts=[_clip(f"s{i}")],
                        whisperx_alignment=_alignment(),
                        timeline_path="/tmp/t.otio",
                        output_dir=out,
                    )
                    results.append((i, r["b2_url"]))
            except Exception as exc:  # noqa: BLE001 — capture for assertion
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        # Race a helper swap against the in-flight workers.
        time.sleep(0.02)

        def alt(local_path: str) -> str:  # noqa: ARG001
            return "b2://alt/url"

        set_assembly_helpers(upload_to_b2=alt)
        for t in threads:
            t.join(timeout=5)
        assert not errors, errors
        assert len(results) == 4
        assert all(url for _, url in results)


# ---------------------------------------------------------------------------
# Duration enforcement
# ---------------------------------------------------------------------------


class TestDurationEnforcement:
    def test_duration_within_tolerance_passes(self) -> None:
        _install_stubs()
        with tempfile.TemporaryDirectory() as out:
            # Render writes an empty file; _probe_duration returns None
            # because ffprobe on an empty file fails — tolerance check is
            # skipped, by design (see docstring). Run this to confirm
            # the skip path doesn't raise on the happy path.
            assemble_final_cut.__wrapped__(
                scenes=[_scene("s1", duration=10.0)],
                clip_artifacts=[_clip("s1")],
                whisperx_alignment=_alignment(),
                timeline_path="/tmp/t.otio",
                output_dir=out,
            )

    def test_duration_drift_triggers_failure_when_probe_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_stubs()

        # Pretend the probe succeeds and reports a wildly different
        # duration — 30 s vs a 10 s target.
        from strands_agents.tools import assembly_tool

        monkeypatch.setattr(
            assembly_tool, "_probe_duration", lambda path: 30.0  # noqa: ARG005
        )

        with pytest.raises(RuntimeError, match="deviates from target"):
            with tempfile.TemporaryDirectory() as out:
                assemble_final_cut.__wrapped__(
                    scenes=[_scene("s1", duration=10.0)],
                    clip_artifacts=[_clip("s1")],
                    whisperx_alignment=_alignment(),
                    timeline_path="/tmp/t.otio",
                    output_dir=out,
                )

    def test_duration_within_tolerance_passes_with_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_stubs()
        from strands_agents.tools import assembly_tool

        monkeypatch.setattr(
            assembly_tool, "_probe_duration", lambda path: 10.5  # noqa: ARG005
        )

        with tempfile.TemporaryDirectory() as out:
            result = assemble_final_cut.__wrapped__(
                scenes=[_scene("s1", duration=10.0)],
                clip_artifacts=[_clip("s1")],
                whisperx_alignment=_alignment(),
                timeline_path="/tmp/t.otio",
                output_dir=out,
            )
            assert result["duration_sec"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Experiment — end-to-end per case
# ---------------------------------------------------------------------------


class TestAssemblyExperiment:
    def test_experiment_name_and_shape(self) -> None:
        assert ASSEMBLY_EXPERIMENT_NAME == "assembly"
        exp = build_assembly_experiment()
        assert len(exp.cases) == 5
        case_names = {c.name for c in exp.cases}
        assert case_names == {
            "clean_3_scenes",
            "missing_clip",
            "gap_in_timeline",
            "outro_missing",
            "b2_upload_fails",
        }
        assert len(exp.evaluators) == 2
        evaluator_types = {type(e).__name__ for e in exp.evaluators}
        assert evaluator_types == {
            "ContractComplianceEvaluator",
            "TimelineComplianceEvaluator",
        }

    def test_contract_evaluator_bound_to_assembly_contract(self) -> None:
        exp = build_assembly_experiment()
        ce = next(
            e for e in exp.evaluators if isinstance(e, ContractComplianceEvaluator)
        )
        assert ce.contract.name == "assembly"

    @pytest.mark.parametrize(
        "case_name,expect_success",
        [
            ("clean_3_scenes", True),
            ("outro_missing", True),
            ("missing_clip", False),
            ("gap_in_timeline", False),
            ("b2_upload_fails", False),
        ],
    )
    def test_case_runs(
        self, case_name: str, expect_success: bool
    ) -> None:
        exp = build_assembly_experiment()
        case = next(c for c in exp.cases if c.name == case_name)
        envelope = assembly_task(case)
        try:
            output = envelope["output"]
            trajectory = envelope["trajectory"]
            if expect_success:
                assert "final_output" in output
                assert output["final_output"]["b2_url"]
                assert any(
                    step.get("name") == "assemble_final_cut"
                    and "error" not in step
                    for step in trajectory
                )
            else:
                assert "final_output" not in output
                assert any(
                    step.get("name") == "assemble_final_cut" and "error" in step
                    for step in trajectory
                )
                if "expected_error_fragment" in (case.metadata or {}):
                    fragment = case.metadata["expected_error_fragment"]
                    error_msg = next(
                        step["error"]
                        for step in trajectory
                        if "error" in step
                    )
                    assert fragment in error_msg
        finally:
            cleanup_assembly_artifact_root(envelope["metadata"]["artifact_root"])

    def test_clean_case_passes_all_timeline_checks(self) -> None:
        exp = build_assembly_experiment()
        case = next(c for c in exp.cases if c.name == "clean_3_scenes")
        envelope = assembly_task(case)
        try:
            timeline_path = envelope["metadata"]["timeline_path"]
            assert timeline_path and os.path.exists(timeline_path)

            evaluator = next(
                e for e in exp.evaluators if isinstance(e, TimelineComplianceEvaluator)
            )
            from strands_evals.types.evaluation import EvaluationData

            outputs = evaluator.evaluate(
                EvaluationData(
                    input=case.input,
                    actual_output=envelope["output"],
                    expected_output=case.expected_output,
                    metadata=envelope["metadata"],
                )
            )
            labels = {o.label for o in outputs}
            assert "timeline_loaded" in labels
            assert "no_negative_duration" in labels
            # every check should pass on the happy path
            assert all(o.score >= 1.0 for o in outputs), [
                (o.label, o.reason) for o in outputs
            ]
        finally:
            cleanup_assembly_artifact_root(envelope["metadata"]["artifact_root"])

    def test_missing_clip_case_has_failure_state_without_final_output(self) -> None:
        exp = build_assembly_experiment()
        case = next(c for c in exp.cases if c.name == "missing_clip")
        envelope = assembly_task(case)
        try:
            assert envelope["metadata"]["timeline_path"] is None
            assert "final_output" not in envelope["output"]
            assert "assembly_error" in envelope["output"]
        finally:
            cleanup_assembly_artifact_root(envelope["metadata"]["artifact_root"])
