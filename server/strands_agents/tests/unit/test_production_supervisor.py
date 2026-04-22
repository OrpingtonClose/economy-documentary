"""Unit tests for component 10 — production-supervisor SubAgent.

The component has four test surfaces:

1. The SubAgent spec (``build_production_subagent``).
2. The four ``@tool``-decorated payload / dispatch helpers in
   :mod:`strands_agents.task_tools`, :mod:`strands_agents.artifact_qa`,
   and :mod:`strands_agents.recovery`.
3. The :class:`ProductionSupervisorTrajectoryEvaluator` and its seven
   hard-gate outputs.
4. The six canonical experiment cases plus their evaluator stack and
   regression thresholds.

Tool callables are invoked via ``__wrapped__`` so the assertions run
against the raw Python function, not the Strands tool-use envelope.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from strands_evals.types.evaluation import EvaluationData

from strands_agents.artifact_qa import (
    ALLOWED_CODECS,
    BLACK_FRAME_CEILING,
    DURATION_TOLERANCE_SEC,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARN,
    evaluate_visual_artifact_quality,
)
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    ProductionSupervisorTrajectoryEvaluator,
)
from strands_agents.evals.experiments.production import (
    PRODUCTION_EVALUATOR_THRESHOLDS,
    build_production_experiment,
    production_cases,
    production_evaluators,
)
from strands_agents.recovery import (
    FIX_BUDGET,
    RETRY_BUDGET,
    RecoveryBudgetExhausted,
    _RecoveryLedger,
    fix_scene,
    get_recovery_ledger,
    request_escalation,
    retry_scene,
    set_recovery_ledger,
    skip_scene,
)
from strands_agents.subagents import (
    PRODUCTION_BOOTSTRAP_TOOLS,
    PRODUCTION_DISPATCH_TOOLS,
    PRODUCTION_FIX_BUDGET,
    PRODUCTION_RECOVERY_TOOLS,
    PRODUCTION_RETRY_BUDGET,
    PRODUCTION_SUBAGENT_DEFAULT_MODEL,
    PRODUCTION_SUBAGENT_MODEL_ENV,
    PRODUCTION_SUBAGENT_PROMPT,
    PRODUCTION_SUBAGENT_TOOL_NAMES,
    PRODUCTION_SUBAGENT_TOOLS,
    build_production_subagent,
)
from strands_agents.task_tools import (
    ProductionHelpersNotConfigured,
    await_tasks,
    check_tasks,
    check_worker_health,
    clear_production_helpers,
    launch_visual_production,
    set_production_helpers,
)
from strands_agents.tools.task_pool import AsyncTaskPool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_recovery_ledger() -> Any:
    """Ensure each test starts with a clean recovery ledger."""
    set_recovery_ledger(None)
    yield
    set_recovery_ledger(None)


@pytest.fixture
def _fake_pool() -> AsyncTaskPool:
    """AsyncTaskPool dedicated to a single test."""
    return AsyncTaskPool(max_workers=2)


@pytest.fixture
def _production_helpers(_fake_pool: AsyncTaskPool) -> Any:
    """Register production helpers with trivial fakes; clear after."""
    dispatches: list[dict[str, Any]] = []

    def dispatch(**kwargs: Any) -> dict[str, Any]:
        dispatches.append(dict(kwargs))
        return {
            "artifact_path": f"b2://video/{kwargs['scene_id']}.mp4",
            "frames": int(round(kwargs["duration_sec"] * 24)),
            "duration_sec": kwargs["duration_sec"],
            "codec": "h264",
            "black_frame_fraction": 0.0,
        }

    def health() -> dict[str, Any]:
        return {
            "workers_total": 2,
            "workers_available": 2,
            "queue_depth": 0,
            "per_worker": [],
        }

    set_production_helpers(pool=_fake_pool, dispatch=dispatch, health_check=health)
    yield {"pool": _fake_pool, "dispatches": dispatches}
    clear_production_helpers()


def _valid_launch_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scene_id": "s1",
        "concept_id": "s1_c0",
        "prompt": "ltx prompt for s1",
        "style_lock": {"dominant_style": "cinematic_documentary"},
        "duration_sec": 12.0,
        "seed": 42,
        "audio_artifact_url": "b2://documentary/audio/s1.wav",
        "revision": 1,
    }
    base.update(overrides)
    return base


def _case(trajectory: list[dict[str, Any]], **metadata: Any) -> EvaluationData[Any, Any]:
    return EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata=metadata,
    )


def _get(outputs: list[Any], label: str) -> Any:
    return next(o for o in outputs if o.label == label)


# ---------------------------------------------------------------------------
# SubAgent spec invariants
# ---------------------------------------------------------------------------


class TestSubAgentSpec:
    def test_spec_has_required_keys(self) -> None:
        spec = build_production_subagent()
        assert set(spec.keys()) >= {"name", "description", "system_prompt", "tools", "model"}

    def test_spec_name_and_description(self) -> None:
        spec = build_production_subagent()
        assert spec["name"] == "production"
        assert "gpu dispatch" in spec["description"].lower()

    def test_spec_prompt_is_constant(self) -> None:
        spec = build_production_subagent()
        assert spec["system_prompt"] == PRODUCTION_SUBAGENT_PROMPT

    def test_spec_tools_match_declared_surface(self) -> None:
        spec = build_production_subagent()
        assert tuple(spec["tools"]) == tuple(PRODUCTION_SUBAGENT_TOOLS)

    def test_spec_model_defaults_to_vision_tier(self, monkeypatch: Any) -> None:
        monkeypatch.delenv(PRODUCTION_SUBAGENT_MODEL_ENV, raising=False)
        spec = build_production_subagent()
        assert spec["model"] == PRODUCTION_SUBAGENT_DEFAULT_MODEL

    def test_spec_model_env_override(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(PRODUCTION_SUBAGENT_MODEL_ENV, "openai/gpt-5-preview")
        spec = build_production_subagent()
        assert spec["model"] == "openai/gpt-5-preview"

    def test_spec_model_explicit_override(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(PRODUCTION_SUBAGENT_MODEL_ENV, "anthropic/claude-opus-4")
        spec = build_production_subagent(model="anthropic/claude-sonnet-4")
        assert spec["model"] == "anthropic/claude-sonnet-4"

    def test_spec_extra_tools_appended(self) -> None:
        def sentinel() -> dict[str, Any]:
            return {}

        spec = build_production_subagent(extra_tools=(sentinel,))
        assert spec["tools"][-1] is sentinel
        assert len(spec["tools"]) == len(PRODUCTION_SUBAGENT_TOOLS) + 1

    def test_tool_names_match_declared_tools(self) -> None:
        declared = tuple(t.tool_name for t in PRODUCTION_SUBAGENT_TOOLS)
        assert declared == PRODUCTION_SUBAGENT_TOOL_NAMES

    def test_bootstrap_tools_are_subset_of_declared(self) -> None:
        assert PRODUCTION_BOOTSTRAP_TOOLS <= set(PRODUCTION_SUBAGENT_TOOL_NAMES)

    def test_dispatch_tools_are_subset_of_declared(self) -> None:
        assert PRODUCTION_DISPATCH_TOOLS <= set(PRODUCTION_SUBAGENT_TOOL_NAMES)

    def test_recovery_tools_are_subset_of_declared(self) -> None:
        assert PRODUCTION_RECOVERY_TOOLS <= set(PRODUCTION_SUBAGENT_TOOL_NAMES)

    def test_prompt_enforces_audio_precondition(self) -> None:
        text = PRODUCTION_SUBAGENT_PROMPT.lower()
        assert "audio" in text
        assert "audio_artifact_url" in PRODUCTION_SUBAGENT_PROMPT

    def test_prompt_enforces_no_pending_finish(self) -> None:
        assert "rendered" in PRODUCTION_SUBAGENT_PROMPT
        assert "skipped" in PRODUCTION_SUBAGENT_PROMPT
        assert "escalated" in PRODUCTION_SUBAGENT_PROMPT

    def test_prompt_declares_budgets(self) -> None:
        assert str(PRODUCTION_RETRY_BUDGET) in PRODUCTION_SUBAGENT_PROMPT
        assert str(PRODUCTION_FIX_BUDGET) in PRODUCTION_SUBAGENT_PROMPT

    def test_prompt_references_rolling_batches(self) -> None:
        assert "rolling batches" in PRODUCTION_SUBAGENT_PROMPT.lower()

    def test_prompt_mentions_every_active_tool(self) -> None:
        # ``check_tasks`` and ``score_visual_coherence`` are available but
        # the prompt does not walk through them step-by-step; the other
        # eight tools are explicitly referenced in the decision tree.
        active_tools = set(PRODUCTION_SUBAGENT_TOOL_NAMES) - {
            "check_tasks",
            "score_visual_coherence",
        }
        for name in active_tools:
            assert name in PRODUCTION_SUBAGENT_PROMPT, f"prompt omits {name}"

    def test_budget_constants_match_recovery(self) -> None:
        assert PRODUCTION_RETRY_BUDGET == RETRY_BUDGET
        assert PRODUCTION_FIX_BUDGET == FIX_BUDGET


# ---------------------------------------------------------------------------
# task_tools.launch_visual_production
# ---------------------------------------------------------------------------


class TestLaunchVisualProduction:
    def test_happy_path_returns_task_payload(self, _production_helpers: Any) -> None:
        payload = launch_visual_production.__wrapped__(**_valid_launch_kwargs())
        assert payload["scene_id"] == "s1"
        assert payload["identity"] == "s1-rev1"
        assert payload["task_id"]
        assert payload["status"] in {"pending", "running", "complete"}

    def test_idempotent_on_scene_and_revision(self, _production_helpers: Any) -> None:
        first = launch_visual_production.__wrapped__(**_valid_launch_kwargs())
        second = launch_visual_production.__wrapped__(**_valid_launch_kwargs())
        assert first["task_id"] == second["task_id"]
        assert first["identity"] == second["identity"]

    def test_new_revision_creates_distinct_task(self, _production_helpers: Any) -> None:
        first = launch_visual_production.__wrapped__(**_valid_launch_kwargs())
        second = launch_visual_production.__wrapped__(
            **_valid_launch_kwargs(revision=2)
        )
        assert first["identity"] != second["identity"]
        assert first["task_id"] != second["task_id"]

    def test_empty_scene_id_rejected(self, _production_helpers: Any) -> None:
        with pytest.raises(ValueError, match="non-empty scene_id"):
            launch_visual_production.__wrapped__(**_valid_launch_kwargs(scene_id=""))

    def test_missing_audio_rejected(self, _production_helpers: Any) -> None:
        with pytest.raises(ValueError, match="audio_artifact_url"):
            launch_visual_production.__wrapped__(
                **_valid_launch_kwargs(audio_artifact_url="")
            )

    def test_non_positive_duration_rejected(self, _production_helpers: Any) -> None:
        with pytest.raises(ValueError, match="duration_sec"):
            launch_visual_production.__wrapped__(**_valid_launch_kwargs(duration_sec=0))

    def test_negative_revision_rejected(self, _production_helpers: Any) -> None:
        with pytest.raises(ValueError, match="revision"):
            launch_visual_production.__wrapped__(**_valid_launch_kwargs(revision=0))

    def test_helpers_not_configured_raises(self) -> None:
        clear_production_helpers()
        with pytest.raises(ProductionHelpersNotConfigured):
            launch_visual_production.__wrapped__(**_valid_launch_kwargs())


class TestCheckWorkerHealth:
    def test_returns_health_snapshot(self, _production_helpers: Any) -> None:
        snapshot = check_worker_health.__wrapped__()
        assert snapshot["workers_total"] == 2
        assert snapshot["workers_available"] == 2

    def test_helpers_not_configured_raises(self) -> None:
        clear_production_helpers()
        with pytest.raises(ProductionHelpersNotConfigured):
            check_worker_health.__wrapped__()

    def test_non_dict_response_raises(self, _fake_pool: AsyncTaskPool) -> None:
        def dispatch(**_: Any) -> dict[str, Any]:
            return {}

        def health() -> dict[str, Any]:
            return "not a dict"  # type: ignore[return-value]

        set_production_helpers(pool=_fake_pool, dispatch=dispatch, health_check=health)
        try:
            with pytest.raises(RuntimeError, match="non-dict"):
                check_worker_health.__wrapped__()
        finally:
            clear_production_helpers()


class TestCheckTasksAndAwait:
    def test_check_tasks_returns_status_dicts(self, _production_helpers: Any) -> None:
        payload = launch_visual_production.__wrapped__(**_valid_launch_kwargs())
        statuses = check_tasks.__wrapped__([payload["task_id"]])
        assert len(statuses) == 1
        assert statuses[0]["task_id"] == payload["task_id"]

    def test_check_tasks_marks_unknown_ids_not_found(self, _production_helpers: Any) -> None:
        statuses = check_tasks.__wrapped__(["ghost-id"])
        assert len(statuses) == 1
        assert statuses[0]["status"] == "not_found"

    def test_await_tasks_blocks_until_terminal(self, _production_helpers: Any) -> None:
        payload = launch_visual_production.__wrapped__(**_valid_launch_kwargs())
        statuses = await_tasks.__wrapped__([payload["task_id"]], timeout=5.0)
        assert len(statuses) == 1
        assert statuses[0]["status"] in {"complete", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# artifact_qa.evaluate_visual_artifact_quality
# ---------------------------------------------------------------------------


def _valid_artifact(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_path": "b2://video/s1.mp4",
        "frames": 288,  # 12s × 24fps
        "duration_sec": 12.0,
        "codec": "h264",
        "black_frame_fraction": 0.0,
    }
    base.update(overrides)
    return base


class TestArtifactQa:
    def test_happy_path_passes(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(), target_duration_sec=12.0
        )
        assert result["verdict"] == VERDICT_PASS
        assert result["passed"] is True
        assert result["issues"] == []

    def test_frame_count_off_by_one_passes(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(frames=287), target_duration_sec=12.0
        )
        assert result["verdict"] == VERDICT_PASS

    def test_frame_count_mismatch_fails(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(frames=200), target_duration_sec=12.0
        )
        assert result["verdict"] == VERDICT_FAIL
        codes = {issue["code"] for issue in result["issues"]}
        assert "frame_count_mismatch" in codes

    def test_duration_within_tolerance_passes(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(duration_sec=12.15, frames=288),
            target_duration_sec=12.0,
        )
        assert result["verdict"] == VERDICT_PASS

    def test_duration_outside_tolerance_fails(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(duration_sec=12.0 + 2 * DURATION_TOLERANCE_SEC),
            target_duration_sec=12.0,
        )
        assert result["verdict"] == VERDICT_FAIL
        codes = {issue["code"] for issue in result["issues"]}
        assert "duration_mismatch" in codes

    def test_disallowed_codec_fails(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(codec="vp9"), target_duration_sec=12.0
        )
        assert result["verdict"] == VERDICT_FAIL
        codes = {issue["code"] for issue in result["issues"]}
        assert "codec_unsupported" in codes

    def test_allowed_codecs_include_h264_h265_hevc(self) -> None:
        assert {"h264", "h265", "hevc"} == set(ALLOWED_CODECS)

    def test_black_frames_below_warn_passes(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(black_frame_fraction=0.01), target_duration_sec=12.0
        )
        assert result["verdict"] == VERDICT_PASS

    def test_black_frames_in_warn_band_yields_warn(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(black_frame_fraction=0.04), target_duration_sec=12.0
        )
        assert result["verdict"] == VERDICT_WARN

    def test_black_frames_above_ceiling_fails(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(black_frame_fraction=BLACK_FRAME_CEILING + 0.1),
            target_duration_sec=12.0,
        )
        assert result["verdict"] == VERDICT_FAIL
        codes = {issue["code"] for issue in result["issues"]}
        assert "black_frame_ceiling_exceeded" in codes

    def test_non_positive_target_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_duration_sec"):
            evaluate_visual_artifact_quality.__wrapped__(
                _valid_artifact(), target_duration_sec=0.0
            )

    def test_non_dict_artifact_rejected(self) -> None:
        with pytest.raises(ValueError, match="artifact must be dict"):
            evaluate_visual_artifact_quality.__wrapped__(
                [], target_duration_sec=12.0  # type: ignore[arg-type]
            )

    def test_missing_artifact_path_rejected(self) -> None:
        bad = _valid_artifact()
        bad.pop("artifact_path")
        with pytest.raises(ValueError, match="artifact_path"):
            evaluate_visual_artifact_quality.__wrapped__(bad, target_duration_sec=12.0)

    def test_returns_check_breakdown(self) -> None:
        result = evaluate_visual_artifact_quality.__wrapped__(
            _valid_artifact(), target_duration_sec=12.0
        )
        assert set(result["checks"].keys()) >= {
            "frame_count",
            "duration",
            "codec",
            "black_frames",
        }


# ---------------------------------------------------------------------------
# recovery.retry_scene / fix_scene / skip_scene / request_escalation
# ---------------------------------------------------------------------------


class TestRetryScene:
    def test_first_retry_succeeds(self) -> None:
        result = retry_scene.__wrapped__(scene_id="s1", reason="worker_500")
        assert result["action"] == "retry"
        assert result["retry_count"] == 1
        assert result["next_revision"] == 2
        assert result["budget"] == RETRY_BUDGET

    def test_retry_budget_enforced(self) -> None:
        for _ in range(RETRY_BUDGET):
            retry_scene.__wrapped__(scene_id="s1", reason="worker_500")
        with pytest.raises(RecoveryBudgetExhausted):
            retry_scene.__wrapped__(scene_id="s1", reason="worker_500")

    def test_empty_scene_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="scene_id"):
            retry_scene.__wrapped__(scene_id="", reason="x")

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            retry_scene.__wrapped__(scene_id="s1", reason="")

    def test_per_scene_ledger_isolation(self) -> None:
        retry_scene.__wrapped__(scene_id="s1", reason="x")
        retry_scene.__wrapped__(scene_id="s2", reason="y")
        snapshot = get_recovery_ledger().snapshot()
        assert snapshot["retries"]["s1"] == 1
        assert snapshot["retries"]["s2"] == 1

    def test_next_revision_includes_fix_count(self) -> None:
        # Interleaved fix then retry must not collide on revision number.
        # (scene_id, revision) is the task pool's idempotency key — a
        # collision would short-circuit to the old failed task instead
        # of launching a fresh dispatch.
        fix_result = fix_scene.__wrapped__(scene_id="s1", reason="style_drift")
        retry_result = retry_scene.__wrapped__(scene_id="s1", reason="worker_500")
        # fix: retry_count=0 + fix_count=1 + 1 = 2
        # retry: retry_count=1 + fix_count=1 + 1 = 3 (not 2)
        assert fix_result["next_revision"] == 2
        assert retry_result["next_revision"] == 3


class TestFixScene:
    def test_first_fix_succeeds(self) -> None:
        result = fix_scene.__wrapped__(scene_id="s1", reason="style_drift")
        assert result["action"] == "fix"
        assert result["fix_count"] == 1
        assert result["next_revision"] == 2
        assert result["budget"] == FIX_BUDGET

    def test_next_revision_includes_retry_count(self) -> None:
        retry_scene.__wrapped__(scene_id="s1", reason="worker_500")
        retry_scene.__wrapped__(scene_id="s1", reason="worker_500")
        result = fix_scene.__wrapped__(scene_id="s1", reason="style_drift")
        # retry_count (2) + fix_count (1) + 1 = 4
        assert result["next_revision"] == RETRY_BUDGET + FIX_BUDGET + 1

    def test_fix_budget_enforced(self) -> None:
        for _ in range(FIX_BUDGET):
            fix_scene.__wrapped__(scene_id="s1", reason="style_drift")
        with pytest.raises(RecoveryBudgetExhausted):
            fix_scene.__wrapped__(scene_id="s1", reason="style_drift")

    def test_empty_scene_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="scene_id"):
            fix_scene.__wrapped__(scene_id="", reason="x")


class TestSkipAndEscalate:
    def test_skip_scene_records_entry(self) -> None:
        result = skip_scene.__wrapped__(scene_id="s1", reason="localised_failure")
        assert result["action"] == "skip"
        assert result["scene_id"] == "s1"
        snapshot = get_recovery_ledger().snapshot()
        assert "s1" in snapshot["skips"]

    def test_skip_scene_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="scene_id"):
            skip_scene.__wrapped__(scene_id="", reason="x")

    def test_request_escalation_builds_payload(self) -> None:
        retry_scene.__wrapped__(scene_id="s1", reason="worker_500")
        payload = request_escalation.__wrapped__(
            scene_id="s1",
            reason="retry_budget_exhausted",
            evidence={"last_error": "worker_500"},
        )
        assert payload["action"] == "escalate"
        assert payload["scene_id"] == "s1"
        assert payload["reason"] == "retry_budget_exhausted"
        assert payload["evidence"] == {"last_error": "worker_500"}
        assert payload["ledger"]["retries"]["s1"] == 1
        assert "requested_at" in payload

    def test_request_escalation_global_scene(self) -> None:
        payload = request_escalation.__wrapped__(
            scene_id="_global", reason="worker_pool_degraded"
        )
        assert payload["scene_id"] == "_global"
        assert payload["evidence"] == {}

    def test_request_escalation_empty_scene_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="scene_id"):
            request_escalation.__wrapped__(scene_id="", reason="x")

    def test_request_escalation_empty_reason_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            request_escalation.__wrapped__(scene_id="s1", reason="")


class TestRecoveryLedger:
    def test_ledger_reset(self) -> None:
        retry_scene.__wrapped__(scene_id="s1", reason="x")
        set_recovery_ledger(None)
        snapshot = get_recovery_ledger().snapshot()
        assert snapshot["retries"] == {}
        assert snapshot["fixes"] == {}
        assert snapshot["skips"] == {}

    def test_ledger_concurrent_increments_safe(self) -> None:
        ledger = _RecoveryLedger()
        set_recovery_ledger(ledger)

        def worker() -> None:
            for _ in range(50):
                ledger.increment_retry("s1")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snapshot = ledger.snapshot()
        assert snapshot["retries"]["s1"] == 200

    def test_set_recovery_ledger_accepts_instance(self) -> None:
        custom = _RecoveryLedger()
        set_recovery_ledger(custom)
        assert get_recovery_ledger() is custom

    def test_try_increment_retry_respects_budget_under_contention(self) -> None:
        # Hammer try_increment_retry from many threads and assert the
        # final counter never exceeds the budget, no matter how many
        # threads raced through the check-and-increment.
        ledger = _RecoveryLedger()
        set_recovery_ledger(ledger)
        budget = RETRY_BUDGET
        exhausted_count = 0
        exhausted_lock = threading.Lock()

        def worker() -> None:
            nonlocal exhausted_count
            for _ in range(25):
                try:
                    ledger.try_increment_retry("s1", budget)
                except RecoveryBudgetExhausted:
                    with exhausted_lock:
                        exhausted_count += 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snapshot = ledger.snapshot()
        assert snapshot["retries"]["s1"] == budget
        # 8 × 25 = 200 total calls; exactly ``budget`` succeed, the rest
        # must have raised RecoveryBudgetExhausted.
        assert exhausted_count == 200 - budget

    def test_try_increment_fix_respects_budget_under_contention(self) -> None:
        ledger = _RecoveryLedger()
        set_recovery_ledger(ledger)
        budget = FIX_BUDGET
        exhausted_count = 0
        exhausted_lock = threading.Lock()

        def worker() -> None:
            nonlocal exhausted_count
            for _ in range(25):
                try:
                    ledger.try_increment_fix("s1", budget)
                except RecoveryBudgetExhausted:
                    with exhausted_lock:
                        exhausted_count += 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snapshot = ledger.snapshot()
        assert snapshot["fixes"]["s1"] == budget
        assert exhausted_count == 200 - budget


# ---------------------------------------------------------------------------
# Trajectory evaluator — synthetic trajectories per hard gate
# ---------------------------------------------------------------------------


def _health_call(turn: int) -> dict[str, Any]:
    return {"name": "check_worker_health", "at_turn": turn, "args": {}}


def _launch_call(
    scene_id: str, turn: int, *, revision: int = 1, audio: str | None = None
) -> dict[str, Any]:
    audio_url = f"b2://audio/{scene_id}.wav" if audio is None else audio
    return {
        "name": "launch_visual_production",
        "at_turn": turn,
        "args": {
            "scene_id": scene_id,
            "revision": revision,
            "audio_artifact_url": audio_url,
        },
    }


def _await_call(task_ids: list[str], turn: int) -> dict[str, Any]:
    return {"name": "await_tasks", "at_turn": turn, "args": {"task_ids": task_ids}}


def _qa_call(scene_id: str, turn: int, *, verdict: str = "pass") -> dict[str, Any]:
    return {
        "name": "evaluate_visual_artifact_quality",
        "at_turn": turn,
        "args": {"artifact": {"scene_id": scene_id, "verdict": verdict}},
    }


def _retry_call(scene_id: str, turn: int) -> dict[str, Any]:
    return {
        "name": "retry_scene",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": "worker_500"},
    }


def _fix_call(scene_id: str, turn: int) -> dict[str, Any]:
    return {
        "name": "fix_scene",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": "style_drift"},
    }


def _skip_call(scene_id: str, turn: int) -> dict[str, Any]:
    return {
        "name": "skip_scene",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": "retry_and_fix_exhausted"},
    }


def _escalate_call(scene_id: str, turn: int) -> dict[str, Any]:
    return {
        "name": "request_escalation",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": "retry_budget_exhausted"},
    }


def _happy_trajectory(n: int = 3) -> list[dict[str, Any]]:
    scenes = [f"s{i + 1}" for i in range(n)]
    traj = [_health_call(1)]
    traj.extend(_launch_call(s, 2) for s in scenes)
    traj.append(_await_call([f"{s}-task" for s in scenes], 3))
    traj.extend(_qa_call(s, 4) for s in scenes)
    return traj


def _happy_metadata(n: int = 3, **overrides: Any) -> dict[str, Any]:
    scenes = [f"s{i + 1}" for i in range(n)]
    meta: dict[str, Any] = {
        "scenes": scenes,
        "expected_terminal_per_scene": {s: "rendered" for s in scenes},
        "expected_retry_count_per_scene": {s: 0 for s in scenes},
        "expected_fix_count_per_scene": {s: 0 for s in scenes},
        "expected_batches": 1,
        "expects_escalation": False,
    }
    meta.update(overrides)
    return meta


class TestTrajectoryEvaluatorConfig:
    def test_missing_scenes_flags_config(self) -> None:
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(_happy_trajectory(3))
        )
        assert len(outputs) == 1
        assert outputs[0].label == "production.missing_config"

    def test_bad_terminal_state_flags_config(self) -> None:
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(
                _happy_trajectory(1),
                **_happy_metadata(
                    n=1, expected_terminal_per_scene={"s1": "done"}
                ),
            )
        )
        assert outputs[0].label == "production.missing_config"

    def test_non_list_trajectory_flags_missing_actual(self) -> None:
        class _Stub:
            actual_trajectory: Any = "not a list"
            metadata: dict[str, Any] = _happy_metadata(3)

        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(_Stub())  # type: ignore[arg-type]
        assert outputs[0].label == "production.missing_actual"

    def test_bad_expected_batches_flags_config(self) -> None:
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(_happy_trajectory(3), **_happy_metadata(3, expected_batches=0))
        )
        assert outputs[0].label == "production.missing_config"


class TestTrajectoryEvaluatorGates:
    def test_happy_path_passes_every_gate(self) -> None:
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(_happy_trajectory(3), **_happy_metadata(3))
        )
        for output in outputs:
            assert output.test_pass is True, f"{output.label}: {output.reason}"

    def test_missing_health_fails_bootstrap(self) -> None:
        traj = _happy_trajectory(3)
        traj = [c for c in traj if c["name"] != "check_worker_health"]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **_happy_metadata(3))
        )
        assert _get(outputs, "production.bootstrap").test_pass is False

    def test_health_after_launch_fails_bootstrap(self) -> None:
        traj = [
            _launch_call("s1", 1),
            _health_call(2),
            _await_call(["s1-task"], 3),
            _qa_call("s1", 4),
        ]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **_happy_metadata(1))
        )
        assert _get(outputs, "production.bootstrap").test_pass is False

    def test_missing_launch_fails_dispatch_coverage(self) -> None:
        traj = _happy_trajectory(3)
        traj = [c for c in traj if c.get("args", {}).get("scene_id") != "s2"]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **_happy_metadata(3))
        )
        assert _get(outputs, "production.dispatch_coverage").test_pass is False

    def test_launch_without_audio_fails_dispatch_coverage(self) -> None:
        traj = [
            _health_call(1),
            _launch_call("s1", 2, audio=""),
            _await_call(["s1-task"], 3),
            _qa_call("s1", 4),
        ]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **_happy_metadata(1))
        )
        assert _get(outputs, "production.dispatch_coverage").test_pass is False

    def test_retry_count_mismatch_fails_retry_budget(self) -> None:
        traj = _happy_trajectory(3) + [
            _retry_call("s2", 5),
            _launch_call("s2", 6, revision=2),
            _await_call(["s2-task-2"], 7),
            _qa_call("s2", 8),
        ]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(
                traj,
                **_happy_metadata(
                    3,
                    expected_retry_count_per_scene={"s1": 0, "s2": 0, "s3": 0},
                    expected_batches=2,
                ),
            )
        )
        assert _get(outputs, "production.retry_budget").test_pass is False

    def test_retry_budget_exceeded_fails(self) -> None:
        traj = [_health_call(1), _launch_call("s1", 2), _await_call(["t"], 3), _qa_call("s1", 4)]
        for rev in range(RETRY_BUDGET + 1):
            traj.extend(
                [
                    _retry_call("s1", 5 + rev * 4),
                    _launch_call("s1", 6 + rev * 4, revision=rev + 2),
                    _await_call([f"t{rev}"], 7 + rev * 4),
                    _qa_call("s1", 8 + rev * 4),
                ]
            )
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(
                traj,
                **_happy_metadata(
                    1,
                    expected_retry_count_per_scene={"s1": RETRY_BUDGET + 1},
                    expected_batches=1 + RETRY_BUDGET + 1,
                ),
            )
        )
        assert _get(outputs, "production.retry_budget").test_pass is False

    def test_fix_count_mismatch_fails_fix_budget(self) -> None:
        traj = _happy_trajectory(3) + [
            _fix_call("s1", 5),
            _launch_call("s1", 6, revision=2),
            _await_call(["s1-task-2"], 7),
            _qa_call("s1", 8),
        ]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(
                traj,
                **_happy_metadata(
                    3,
                    expected_fix_count_per_scene={"s1": 0, "s2": 0, "s3": 0},
                    expected_batches=2,
                ),
            )
        )
        assert _get(outputs, "production.fix_budget").test_pass is False

    def test_wrong_batch_count_fails_rolling_batches(self) -> None:
        traj = _happy_trajectory(3)
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **_happy_metadata(3, expected_batches=2))
        )
        assert _get(outputs, "production.rolling_batches").test_pass is False

    def test_scene_left_pending_fails_no_pending_at_finish(self) -> None:
        # Metadata declares four scenes but only three are dispatched —
        # s4 is pending at finish, which must trip the hard gate.
        traj = _happy_trajectory(3)
        meta = _happy_metadata(3)
        meta["scenes"] = ["s1", "s2", "s3", "s4"]
        meta["expected_terminal_per_scene"] = {
            "s1": "rendered",
            "s2": "rendered",
            "s3": "rendered",
            "s4": "rendered",
        }
        meta["expected_retry_count_per_scene"] = {
            "s1": 0,
            "s2": 0,
            "s3": 0,
            "s4": 0,
        }
        meta["expected_fix_count_per_scene"] = {
            "s1": 0,
            "s2": 0,
            "s3": 0,
            "s4": 0,
        }
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **meta)
        )
        assert _get(outputs, "production.no_pending_at_finish").test_pass is False

    def test_escalation_missing_when_expected_fails(self) -> None:
        traj = _happy_trajectory(3)
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(
                traj,
                **_happy_metadata(
                    3,
                    expects_escalation=True,
                    expected_terminal_per_scene={
                        "s1": "rendered",
                        "s2": "rendered",
                        "s3": "rendered",
                    },
                ),
            )
        )
        assert _get(outputs, "production.escalation_appropriateness").test_pass is False

    def test_escalation_present_when_not_expected_fails(self) -> None:
        traj = _happy_trajectory(3) + [_escalate_call("s1", 5)]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(traj, **_happy_metadata(3, expects_escalation=False))
        )
        assert _get(outputs, "production.escalation_appropriateness").test_pass is False

    def test_skip_then_launch_fails_escalation_appropriateness(self) -> None:
        traj = _happy_trajectory(3) + [
            _skip_call("s2", 5),
            _launch_call("s2", 6, revision=2),
        ]
        outputs = ProductionSupervisorTrajectoryEvaluator().evaluate(
            _case(
                traj,
                **_happy_metadata(
                    3,
                    expected_terminal_per_scene={
                        "s1": "rendered",
                        "s2": "skipped",
                        "s3": "rendered",
                    },
                    expected_batches=2,
                ),
            )
        )
        assert (
            _get(outputs, "production.escalation_appropriateness").test_pass is False
        )


# ---------------------------------------------------------------------------
# Experiment factory — six canonical cases
# ---------------------------------------------------------------------------


class TestExperimentFactory:
    def test_production_cases_returns_six(self) -> None:
        cases = production_cases()
        assert len(cases) == 6
        names = {c.name for c in cases}
        assert names == {
            "one_shot_success",
            "transient_worker_error",
            "prompt_issue",
            "persistent_failure",
            "worker_starved",
            "budget_exhausted",
        }

    def test_production_evaluators_stack(self) -> None:
        evaluators = production_evaluators()
        assert len(evaluators) == 2
        assert isinstance(evaluators[0], ProductionSupervisorTrajectoryEvaluator)
        assert isinstance(evaluators[1], ContractComplianceEvaluator)

    def test_build_production_experiment_is_well_formed(self) -> None:
        experiment = build_production_experiment()
        assert len(experiment.cases) == 6
        assert len(experiment.evaluators) == 2

    def test_thresholds_cover_every_evaluator(self) -> None:
        evaluator_names = {type(e).__name__ for e in production_evaluators()}
        assert set(PRODUCTION_EVALUATOR_THRESHOLDS.keys()) == evaluator_names
        for score_min, hard_gate in PRODUCTION_EVALUATOR_THRESHOLDS.values():
            assert 0.0 <= score_min <= 1.0
            assert isinstance(hard_gate, bool)

    def test_every_case_has_trajectory_metadata(self) -> None:
        for case in production_cases():
            assert case.expected_trajectory
            meta = case.metadata or {}
            assert meta["scenes"]
            assert meta["expected_terminal_per_scene"]
            assert "expected_retry_count_per_scene" in meta
            assert "expected_fix_count_per_scene" in meta
            assert meta["expected_batches"] >= 1

    def test_trajectory_evaluator_accepts_every_case(self) -> None:
        evaluator = ProductionSupervisorTrajectoryEvaluator()
        for case in production_cases():
            data = EvaluationData[Any, Any](
                input=case.input,
                actual_trajectory=case.expected_trajectory,
                metadata=case.metadata,
            )
            outputs = evaluator.evaluate(data)
            for output in outputs:
                assert output.test_pass is True, (
                    f"case={case.name} label={output.label}: {output.reason}"
                )

    def test_one_shot_success_expected_no_escalation(self) -> None:
        case = next(c for c in production_cases() if c.name == "one_shot_success")
        assert case.metadata["expects_escalation"] is False

    def test_budget_exhausted_expects_escalation(self) -> None:
        case = next(c for c in production_cases() if c.name == "budget_exhausted")
        assert case.metadata["expects_escalation"] is True
        assert case.metadata["expected_terminal_per_scene"]["s1"] == "escalated"

    def test_persistent_failure_records_skip(self) -> None:
        case = next(c for c in production_cases() if c.name == "persistent_failure")
        assert case.metadata["expected_terminal_per_scene"]["s3"] == "skipped"
        assert (
            case.metadata["expected_retry_count_per_scene"]["s3"]
            == PRODUCTION_RETRY_BUDGET
        )
        assert (
            case.metadata["expected_fix_count_per_scene"]["s3"]
            == PRODUCTION_FIX_BUDGET
        )

    def test_worker_starved_uses_multiple_batches(self) -> None:
        case = next(c for c in production_cases() if c.name == "worker_starved")
        assert case.metadata["expected_batches"] >= 2
