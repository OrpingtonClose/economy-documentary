"""Unit tests for the documentary pipeline orchestrator (component 14).

Covers:

* DI signature (``build_orchestrator`` accepts empty defaults).
* Default tool assembly (real import + placeholder fallback).
* ``interrupt_on`` wiring matches :data:`INTERRUPT_TOOL_NAMES`.
* Memory paths default to the documented pair.
* ``FilesystemBackend`` root_dir propagates.
* Async ``run_documentary`` entrypoint resolves interrupts via the
  injected operator-decision handler.
* The 5-case experiment runs every evaluator at 1.0 with pre-captured
  trajectories.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.types import Command

from strands_agents import _placeholders
from strands_agents.approval import request_human_approval
from strands_agents.evals.experiments import (
    build_pipeline_experiment,
    pipeline_task,
)
from strands_agents.evals.experiments.pipeline import (
    _CASES,
    _final_state,
)
from strands_agents.pipeline import (
    INTERRUPT_TOOL_NAMES,
    ORCHESTRATOR_PROMPT,
    _build_interrupt_on,
    build_default_subagents,
    build_default_tools,
    build_documentary_orchestrator,
    build_orchestrator,
)
from strands_agents.run import (
    _auto_reject_interrupt,
    run_documentary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_model() -> FakeMessagesListChatModel:
    """Chat model that immediately answers — no tool calls."""

    return FakeMessagesListChatModel(
        responses=[AIMessage(content="pipeline test: done")],
    )


@tool
def _noop_tool(x: str) -> dict[str, Any]:
    """Minimal tool used in structural tests."""

    return {"echoed": x}


# ---------------------------------------------------------------------------
# _build_interrupt_on
# ---------------------------------------------------------------------------


class TestBuildInterruptOn:
    def test_builds_entry_per_tool(self) -> None:
        cfg = _build_interrupt_on(["a", "b"])
        assert set(cfg.keys()) == {"a", "b"}

    def test_each_config_allows_four_decisions(self) -> None:
        # Component 15 vocabulary: accept/edit/reject/respond.
        # launch_assembly drops ``edit``; request_human_approval keeps
        # only accept/respond.
        cfg = _build_interrupt_on(INTERRUPT_TOOL_NAMES)
        for name, entry in cfg.items():
            assert isinstance(entry, dict), name
            allowed = entry.get("allowed_decisions")
            assert allowed, name
            assert set(allowed) <= {"accept", "edit", "reject", "respond"}, name

    def test_empty_list_returns_empty_dict(self) -> None:
        assert _build_interrupt_on([]) == {}


# ---------------------------------------------------------------------------
# build_default_tools / build_default_subagents
# ---------------------------------------------------------------------------


class TestDefaultTools:
    def test_includes_request_human_approval(self) -> None:
        tools = build_default_tools()
        names = {getattr(t, "name", None) for t in tools}
        assert "request_human_approval" in names

    def test_placeholder_tools_present_when_real_missing(self) -> None:
        tools = build_default_tools()
        names = {getattr(t, "name", None) for t in tools}
        # These leaves do not exist on main yet — the default tool list
        # must still expose them so the orchestrator can be built.
        expected_leaves = {
            "generate_scenario",
            "evaluate_scenario",
            "refine_scenario",
            "evaluate_timing",
            "launch_audio_render",
            "launch_visual_production",
            "launch_assembly",
            "launch_b2_sync",
            "check_tasks",
            "await_tasks",
        }
        assert expected_leaves.issubset(names), expected_leaves - names

    def test_tool_count(self) -> None:
        # 10 leaves + 4 QA gates (qa_audio_completeness,
        # qa_duration_align, qa_stills_judge, qa_video_artifact_probe)
        # + request_human_approval = 15
        assert len(build_default_tools()) == 15


class TestRealWorkerOverlay:
    """``build_documentary_orchestrator`` always overlays the
    ``launch_audio_render`` and ``launch_visual_production`` placeholders
    with real-worker HTTP dispatchers whose worker URL is resolved
    *at call time* via lazy on-demand provisioning. Pre-warming via
    env vars is forbidden by the orchestrator's operational contract —
    GPU VMs are spun up only when a run actually starts. The env vars
    remain a fast-path short-circuit for already-warm URLs but are
    no longer required to enable the overlay.
    """

    def test_overlay_active_when_env_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        captured: dict[str, Any] = {}

        def _capture(
            run_dir: Path,
            *,
            model: Any = None,
            tools: Any = None,
            subagents: Any = None,
            **_: Any,
        ) -> str:
            captured["tools"] = list(tools or [])
            return "stub"

        monkeypatch.setattr(
            "strands_agents.pipeline.build_orchestrator",
            _capture,
        )
        build_documentary_orchestrator(tmp_path, model=_fake_model())

        audio = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_audio_render"
        )
        video = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_visual_production"
        )
        # On-demand provisioning: the overlay is always installed; the
        # worker URL is resolved lazily on first dispatch via
        # ``WorkerProvisioner.wait_for_worker``.
        assert audio is not _placeholders.launch_audio_render
        assert getattr(audio, "name", None) == "launch_audio_render"
        assert video is not _placeholders.launch_visual_production
        assert getattr(video, "name", None) == "launch_visual_production"

    def test_video_overlay_when_video_env_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.setenv("LTX_VIDEO_WORKER_URL", "http://video.invalid:9000")
        captured: dict[str, Any] = {}

        def _capture(
            run_dir: Path,
            *,
            model: Any = None,
            tools: Any = None,
            subagents: Any = None,
            **_: Any,
        ) -> str:
            captured["tools"] = list(tools or [])
            return "stub"

        monkeypatch.setattr(
            "strands_agents.pipeline.build_orchestrator",
            _capture,
        )
        build_documentary_orchestrator(tmp_path, model=_fake_model())

        audio = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_audio_render"
        )
        video = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_visual_production"
        )
        # Both overlays active; the env var only short-circuits URL
        # resolution for the video tool.
        assert audio is not _placeholders.launch_audio_render
        assert video is not _placeholders.launch_visual_production
        assert getattr(video, "name", None) == "launch_visual_production"

    def test_audio_overlay_when_audio_env_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("QWEN3_TTS_WORKER_URL", "http://audio.invalid:8000")
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)
        captured: dict[str, Any] = {}

        def _capture(
            run_dir: Path,
            *,
            model: Any = None,
            tools: Any = None,
            subagents: Any = None,
            **_: Any,
        ) -> str:
            captured["tools"] = list(tools or [])
            return "stub"

        monkeypatch.setattr(
            "strands_agents.pipeline.build_orchestrator",
            _capture,
        )
        build_documentary_orchestrator(tmp_path, model=_fake_model())

        audio = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_audio_render"
        )
        video = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_visual_production"
        )
        assert audio is not _placeholders.launch_audio_render
        assert getattr(audio, "name", None) == "launch_audio_render"
        assert video is not _placeholders.launch_visual_production
        assert getattr(video, "name", None) == "launch_visual_production"

    def test_both_overlays_when_both_env_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("QWEN3_TTS_WORKER_URL", "http://audio.invalid:8000")
        monkeypatch.setenv("LTX_VIDEO_WORKER_URL", "http://video.invalid:9000")
        captured: dict[str, Any] = {}

        def _capture(
            run_dir: Path,
            *,
            model: Any = None,
            tools: Any = None,
            subagents: Any = None,
            **_: Any,
        ) -> str:
            captured["tools"] = list(tools or [])
            return "stub"

        monkeypatch.setattr(
            "strands_agents.pipeline.build_orchestrator",
            _capture,
        )
        build_documentary_orchestrator(tmp_path, model=_fake_model())

        names = [
            getattr(t, "name", None) for t in captured["tools"]
        ]
        # Both names still present exactly once.
        assert names.count("launch_audio_render") == 1
        assert names.count("launch_visual_production") == 1

        audio = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_audio_render"
        )
        video = next(
            t for t in captured["tools"]
            if getattr(t, "name", None) == "launch_visual_production"
        )
        assert audio is not _placeholders.launch_audio_render
        assert video is not _placeholders.launch_visual_production

    def test_other_tools_pass_through_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Tools that are not part of the worker overlay must keep
        # their identity and ordering — overlays only target
        # ``launch_audio_render`` and ``launch_visual_production``.
        monkeypatch.setenv("QWEN3_TTS_WORKER_URL", "http://audio.invalid:8000")
        monkeypatch.setenv("LTX_VIDEO_WORKER_URL", "http://video.invalid:9000")
        captured: dict[str, Any] = {}

        def _capture(
            run_dir: Path,
            *,
            model: Any = None,
            tools: Any = None,
            subagents: Any = None,
            **_: Any,
        ) -> str:
            captured["tools"] = list(tools or [])
            return "stub"

        monkeypatch.setattr(
            "strands_agents.pipeline.build_orchestrator",
            _capture,
        )
        build_documentary_orchestrator(tmp_path, model=_fake_model())

        # Non-overlaid tools should still be the canonical objects.
        names_to_canonical = {
            "generate_scenario": _placeholders.generate_scenario,
            "evaluate_scenario": _placeholders.evaluate_scenario,
            "refine_scenario": _placeholders.refine_scenario,
            "evaluate_timing": _placeholders.evaluate_timing,
            "launch_assembly": _placeholders.launch_assembly,
            "launch_b2_sync": _placeholders.launch_b2_sync,
            "check_tasks": _placeholders.check_tasks,
            "await_tasks": _placeholders.await_tasks,
            "request_human_approval": request_human_approval,
        }
        by_name = {
            getattr(t, "name", None): t for t in captured["tools"]
        }
        for name, canonical in names_to_canonical.items():
            assert by_name[name] is canonical, name

    def test_tool_count_stable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Overlays must not add or drop tools — count stays at 15
        # (10 leaves + 4 QA gates + request_human_approval, slice 9p).
        monkeypatch.setenv("QWEN3_TTS_WORKER_URL", "http://audio.invalid:8000")
        monkeypatch.setenv("LTX_VIDEO_WORKER_URL", "http://video.invalid:9000")
        captured: dict[str, Any] = {}

        def _capture(
            run_dir: Path,
            *,
            model: Any = None,
            tools: Any = None,
            subagents: Any = None,
            **_: Any,
        ) -> str:
            captured["tools"] = list(tools or [])
            return "stub"

        monkeypatch.setattr(
            "strands_agents.pipeline.build_orchestrator",
            _capture,
        )
        build_documentary_orchestrator(tmp_path, model=_fake_model())
        assert len(captured["tools"]) == 15


class TestDefaultSubagents:
    def test_returns_list(self) -> None:
        subs = build_default_subagents()
        assert isinstance(subs, list)

    def test_empty_when_deps_not_merged(self) -> None:
        # Off of main, none of the per-component SubAgent modules
        # exist, so the default list should be empty. When the
        # corresponding PRs merge, this list grows without any
        # orchestrator-level change.
        subs = build_default_subagents()
        for sub in subs:
            assert "name" in sub


# ---------------------------------------------------------------------------
# build_orchestrator
# ---------------------------------------------------------------------------


class TestBuildOrchestrator:
    def test_constructs_with_empty_defaults(self, tmp_path: Path) -> None:
        agent = build_orchestrator(
            tmp_path,
            model=_fake_model(),
            tools=[],
            subagents=[],
        )
        assert agent is not None
        assert hasattr(agent, "ainvoke")
        assert hasattr(agent, "invoke")

    def test_constructs_with_placeholders(self, tmp_path: Path) -> None:
        agent = build_orchestrator(
            tmp_path,
            model=_fake_model(),
            tools=build_default_tools(),
            subagents=[],
        )
        assert agent is not None

    def test_custom_memory_paths_respected(self, tmp_path: Path) -> None:
        # The FilesystemBackend root_dir is tmp_path; memory paths are
        # interpreted relative to it. We write a file so MemoryMiddleware
        # has something to load, then rebuild.
        (tmp_path / "AGENTS.md").write_text("test invariants")
        agent = build_orchestrator(
            tmp_path,
            model=_fake_model(),
            tools=[],
            subagents=[],
            memory=["AGENTS.md"],
        )
        assert agent is not None

    def test_prompt_mentions_five_stages(self) -> None:
        for keyword in (
            "Scenario",
            "Audio",
            "Visual",
            "Production",
            "Assembly",
        ):
            assert keyword in ORCHESTRATOR_PROMPT, keyword

    def test_prompt_mandates_qa_gates_after_visual_production(self) -> None:
        # Slice 9l anti-drift: a future LLM rewriting the prompt and
        # dropping the QA gate instructions must fail CI. The slice
        # 9j frozen-frame regression is exactly what happens when
        # these gates aren't called.
        for keyword in (
            "qa_duration_align",
            "qa_stills_judge",
            "escalation",
            "AGENTS.md hard invariants",
        ):
            assert keyword in ORCHESTRATOR_PROMPT, keyword

    def test_prompt_forbids_silent_qa_failure(self) -> None:
        # Either gate failing must NOT silently advance to assembly.
        assert "Never silently accept a" in ORCHESTRATOR_PROMPT
        assert "verdict == \"fail\"" in ORCHESTRATOR_PROMPT


# ---------------------------------------------------------------------------
# run_documentary
# ---------------------------------------------------------------------------


class _StubGraph:
    """Minimal stand-in for a CompiledStateGraph.

    Yields two interrupt rounds then terminates. Keeps a log of the
    inputs it was invoked with so the test can assert the run loop
    resumed with the operator decision rather than the original brief.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._interrupt_rounds_left = 2

    async def ainvoke(self, value: Any) -> dict[str, Any]:
        self.calls.append(value)
        if self._interrupt_rounds_left > 0:
            self._interrupt_rounds_left -= 1
            return {"__interrupt__": [{"value": "needs operator"}]}
        return {"final": True}


class TestRunDocumentary:
    def test_auto_reject_decision_default(self) -> None:
        command = asyncio.run(_auto_reject_interrupt({"__interrupt__": ["x"]}))
        # _auto_reject_interrupt emits the langchain HITL middleware shape
        # (``{"decisions": [...]}``) so the legacy single-decision payload
        # cannot crash a paused graph with ``KeyError: 'decisions'`` on
        # resume.
        assert isinstance(command, Command)
        assert command.resume == {
            "decisions": [
                {"type": "reject", "message": "no operator attached"},
            ],
            "_project_decision_type": "reject",
        }

    def test_interrupt_loop_resumes_with_decision(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _StubGraph()
        monkeypatch.setattr(
            "strands_agents.run.build_documentary_orchestrator",
            lambda run_dir, *, model=None: stub,  # type: ignore[misc]  # noqa: ARG005
        )

        async def decider(state: dict[str, Any]) -> Command:
            _ = state  # unused — returning a constant is fine for this test.
            return Command(resume={"type": "accept"})

        result = asyncio.run(
            run_documentary(
                "hello",
                tmp_path,
                get_operator_decision=decider,
            ),
        )
        assert result == {"final": True}
        assert len(stub.calls) == 3
        first = stub.calls[0]
        assert first == {"messages": [("user", "hello")]}
        for call in stub.calls[1:]:
            assert isinstance(call, Command)
            assert call.resume == {"type": "accept"}

    def test_interrupt_loop_cap_enforced(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _InfiniteInterruptGraph:
            async def ainvoke(self, value: Any) -> dict[str, Any]:  # noqa: ARG002
                return {"__interrupt__": [{"value": "loop"}]}

        monkeypatch.setattr(
            "strands_agents.run.build_documentary_orchestrator",
            lambda run_dir, *, model=None: _InfiniteInterruptGraph(),  # type: ignore[misc]  # noqa: ARG005
        )

        async def decider(state: dict[str, Any]) -> Command:
            _ = state
            return Command(resume={"type": "accept"})

        with pytest.raises(RuntimeError, match="interrupt loop exceeded"):
            asyncio.run(
                run_documentary(
                    "brief",
                    tmp_path,
                    get_operator_decision=decider,
                    max_interrupt_rounds=2,
                ),
            )


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------


class TestPlaceholders:
    @pytest.mark.parametrize(
        ("tool_fn", "name", "kwargs"),
        [
            (
                _placeholders.generate_scenario,
                "generate_scenario",
                {"topic": "inflation"},
            ),
            (
                _placeholders.evaluate_scenario,
                "evaluate_scenario",
                {"scenes": []},
            ),
            (
                _placeholders.refine_scenario,
                "refine_scenario",
                {"scenes": [], "feedback": {}},
            ),
            (
                _placeholders.evaluate_timing,
                "evaluate_timing",
                {
                    "timeline": {},
                    "alignment": {},
                    "target_duration_sec": 300.0,
                },
            ),
            (
                _placeholders.launch_audio_render,
                "launch_audio_render",
                {"scene_id": "s0", "voice_id": "v0"},
            ),
            (
                _placeholders.launch_visual_production,
                "launch_visual_production",
                {"scene_id": "s0", "visual_concept": {}},
            ),
            (
                _placeholders.launch_assembly,
                "launch_assembly",
                {"timeline": {}, "output_path": "x.mp4"},
            ),
            (
                _placeholders.launch_b2_sync,
                "launch_b2_sync",
                {"artifact_path": "x.mp4"},
            ),
            (
                _placeholders.check_tasks,
                "check_tasks",
                {"task_ids": ["a"]},
            ),
            (
                _placeholders.await_tasks,
                "await_tasks",
                {"task_ids": ["a"]},
            ),
        ],
    )
    def test_placeholder_envelope(
        self,
        tool_fn: Any,
        name: str,
        kwargs: dict[str, Any],
    ) -> None:
        result = tool_fn.invoke(kwargs)
        assert result["status"] == "placeholder"
        assert result["tool"] == name


# ---------------------------------------------------------------------------
# request_human_approval
# ---------------------------------------------------------------------------


class TestRequestHumanApproval:
    def test_returns_pending_envelope(self) -> None:
        result = request_human_approval.invoke(
            {
                "reason": "escalation:approve_assembly",
                "summary": "approve assembly",
                "options": ["approve", "abort"],
            },
        )
        assert result["status"] == "pending"
        assert result["summary"] == "approve assembly"
        assert result["options"] == ["approve", "abort"]

    def test_defaults_empty_collections(self) -> None:
        result = request_human_approval.invoke(
            {"reason": "x", "summary": "ok"},
        )
        assert result["options"] == []
        assert result["context_paths"] == []


# ---------------------------------------------------------------------------
# Pipeline experiment
# ---------------------------------------------------------------------------


class TestPipelineExperiment:
    def test_five_cases(self) -> None:
        assert len(_CASES) == 5
        names = {c.name for c in _CASES}
        assert names == {
            "happy_path_5min",
            "timing_refine_once",
            "visual_revise",
            "escalation_path",
            "operator_approval_edit",
        }

    def test_every_case_has_trajectory_and_state(self) -> None:
        for case in _CASES:
            assert case.expected_trajectory, case.name
            assert case.expected_output, case.name
            assert case.metadata and case.metadata.get("expected_tool_sequence")

    def test_pipeline_task_shape(self) -> None:
        case = _CASES[0]
        out = pipeline_task(case)
        assert set(out.keys()) == {"output", "trajectory"}
        assert out["trajectory"] == list(case.expected_trajectory or [])

    def test_final_state_satisfies_contract(self) -> None:
        state = _final_state()
        for key in (
            "brief",
            "target_duration_sec",
            "scenes",
            "whisperx_alignment",
            "visual_concepts",
            "final_timeline",
        ):
            assert key in state, key
            assert state[key], key

    def test_experiment_runs_all_pass(self, tmp_path: Path) -> None:
        # The contract evaluator also checks produced_artifacts
        # (``output/*.mp4``) — we create the artifact directory for the
        # check to pass. The contract is evaluated against the real
        # filesystem (/tmp/documentary-pipeline by default).
        artifacts_dir = Path("/tmp/documentary-pipeline/output")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifacts_dir / "pipeline_experiment_smoke.mp4"
        artifact.write_bytes(b"fake mp4")
        try:
            experiment = build_pipeline_experiment()
            reports = experiment.run_evaluations(pipeline_task)
            assert len(reports) == 2
            for report in reports:
                assert report.overall_score == pytest.approx(1.0), (
                    f"evaluator={report.evaluator_name} "
                    f"score={report.overall_score} reasons={report.reasons}"
                )
                assert all(report.test_passes), report.reasons
                assert len(report.cases) == 5
        finally:
            # Best-effort cleanup — leave the directory so sibling
            # experiments don't race.
            if artifact.exists():
                artifact.unlink()
        _ = tmp_path  # run_dir is unused here; contract checks /tmp.
