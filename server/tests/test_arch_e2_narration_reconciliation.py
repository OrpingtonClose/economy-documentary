"""Narration reconciliation tests (ARCH-E2, issue #148).

For every narration block, the reconciliation loop compares the
WhisperX-measured duration against the scripted pacing declared by
the scenario. Blocks outside the tolerance band raise
:class:`NarrationReconciliationFailure`, which the audio ladder
converts into a re-entry with the timing delta as the failure signal.

Each test synthesises a fake pipeline blackboard with
``_voice_budgets`` (scripted pacing) and ``whisperx_alignment``
(measured duration) to exercise the kernel without touching the
audio callback or the TTS worker.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from callbacks.narration_reconciliation import (
    DEFAULT_ABS_TOLERANCE_SEC,
    DEFAULT_TOLERANCE_RATIO,
    NARRATION_RECONCILIATION_OPERATION,
    NARRATION_RECONCILIATION_PASSED_KEY,
    NARRATION_RECONCILIATION_STATE_KEY,
    NarrationReconciliationFailure,
    NarrationTimingVerdict,
    build_narration_reconciliation_agent,
    collect_failures,
    narration_reconciliation_after_agent_callback,
    reconcile_block,
    run_narration_reconciliation,
)


# ---------------------------------------------------------------------------
# Blackboard helpers
# ---------------------------------------------------------------------------


def _build_state(
    *,
    scripted: dict[str, float],
    measured: dict[str, float],
    blocks: list[dict[str, Any]],
    phase: str = "audio",
) -> dict:
    """Build a minimal blackboard that exercises the reconciliation loop."""
    alignment = {
        block_id: {"total_duration": dur}
        for block_id, dur in measured.items()
    }
    return {
        "pipeline_phase": phase,
        "_voice_budgets": json.dumps(scripted),
        "whisperx_alignment": json.dumps(alignment),
        "_stylistic_qa_blocks": json.dumps(blocks),
    }


def _single_block_state(
    *, scripted_sec: float, measured_sec: float,
) -> dict:
    """State with a single block ``scene_001_V1``."""
    return _build_state(
        scripted={"scene_001_V1": scripted_sec},
        measured={"scene_001_V1": measured_sec},
        blocks=[{
            "block_id": "scene_001_V1",
            "scene_num": 1,
            "voice_role": "V1",
            "language": "",
        }],
    )


# ---------------------------------------------------------------------------
# Kernel — reconcile_block
# ---------------------------------------------------------------------------


class TestReconcileBlockKernel:

    def test_within_ratio_passes(self):
        result = reconcile_block(
            block_id="scene_001_V1",
            scene_num=1,
            voice_role="V1",
            language="",
            scripted_sec=10.0,
            measured_sec=10.5,  # +5 % — inside ±15 % ratio
        )
        assert result.verdict is NarrationTimingVerdict.PASS
        assert abs(result.delta_sec - 0.5) < 1e-9
        assert abs(result.ratio - 0.05) < 1e-9

    def test_outside_ratio_fails(self):
        result = reconcile_block(
            block_id="scene_001_V1",
            scene_num=1,
            voice_role="V1",
            language="",
            scripted_sec=10.0,
            measured_sec=6.0,  # -40 % — well outside ±15 %
        )
        assert result.verdict is NarrationTimingVerdict.FAIL
        assert result.delta_sec == pytest.approx(-4.0)
        assert "OUT OF TOLERANCE" in result.message

    def test_short_block_respects_absolute_tolerance_floor(self):
        # 1 s scripted at 15 % ratio = 0.15 s band (below 0.25 s floor).
        # A 0.2 s drift must still PASS because the abs floor wins.
        result = reconcile_block(
            block_id="scene_001_V1",
            scene_num=1,
            voice_role="V1",
            language="",
            scripted_sec=1.0,
            measured_sec=1.2,
        )
        assert result.tolerance_sec == pytest.approx(
            DEFAULT_ABS_TOLERANCE_SEC,
        )
        assert result.verdict is NarrationTimingVerdict.PASS

    def test_short_block_beyond_absolute_tolerance_fails(self):
        result = reconcile_block(
            block_id="scene_001_V1",
            scene_num=1,
            voice_role="V1",
            language="",
            scripted_sec=1.0,
            measured_sec=1.6,  # +0.6 s > 0.25 s floor
        )
        assert result.verdict is NarrationTimingVerdict.FAIL

    def test_missing_measurement_returns_skip(self):
        result = reconcile_block(
            block_id="scene_001_V1",
            scene_num=1,
            voice_role="V1",
            language="",
            scripted_sec=10.0,
            measured_sec=None,
        )
        assert result.verdict is NarrationTimingVerdict.SKIP
        assert "no WhisperX measurement" in result.message

    def test_missing_budget_returns_skip(self):
        result = reconcile_block(
            block_id="scene_001_V1",
            scene_num=1,
            voice_role="V1",
            language="",
            scripted_sec=None,
            measured_sec=10.0,
        )
        assert result.verdict is NarrationTimingVerdict.SKIP
        assert "no scripted pacing" in result.message

    def test_custom_tolerance_ratio_tightens_check(self):
        # 2 % drift; default 15 % passes, a tight 1 % budget fails.
        passed = reconcile_block(
            block_id="scene_001_V1", scene_num=1,
            voice_role="V1", language="",
            scripted_sec=10.0, measured_sec=10.2,
        )
        assert passed.verdict is NarrationTimingVerdict.PASS

        tight = reconcile_block(
            block_id="scene_001_V1", scene_num=1,
            voice_role="V1", language="",
            scripted_sec=10.0, measured_sec=10.2,
            tolerance_ratio=0.01,
            abs_tolerance_sec=0.05,  # force ratio to apply
        )
        assert tight.verdict is NarrationTimingVerdict.FAIL

    def test_result_to_dict_roundtrip(self):
        r = reconcile_block(
            block_id="scene_001_V1", scene_num=1,
            voice_role="V1", language="ru",
            scripted_sec=5.0, measured_sec=5.3,
        )
        d = r.to_dict()
        assert d["block_id"] == "scene_001_V1"
        assert d["verdict"] == "pass"
        assert d["scripted_sec"] == 5.0
        assert d["measured_sec"] == 5.3


# ---------------------------------------------------------------------------
# Aggregate — run_narration_reconciliation
# ---------------------------------------------------------------------------


class TestRunNarrationReconciliation:

    def test_empty_blocks_passes_and_persists_empty_report(self):
        state: dict = {}
        results = run_narration_reconciliation(
            state,
            blocks=[],
            raise_on_failure=True,
        )
        assert results == []
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is True
        assert json.loads(
            state[NARRATION_RECONCILIATION_STATE_KEY]
        ) == []

    def test_happy_path_sets_passed_and_persists_report(self):
        state = _single_block_state(scripted_sec=10.0, measured_sec=10.2)
        results = run_narration_reconciliation(state)
        assert len(results) == 1
        assert results[0].verdict is NarrationTimingVerdict.PASS
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is True
        report = json.loads(state[NARRATION_RECONCILIATION_STATE_KEY])
        assert report[0]["verdict"] == "pass"

    def test_raises_on_failure_carries_structured_payload(self):
        state = _single_block_state(scripted_sec=10.0, measured_sec=4.0)
        with pytest.raises(NarrationReconciliationFailure) as excinfo:
            run_narration_reconciliation(state)
        payload = excinfo.value.diagnostic_data()
        assert payload["operation"] == NARRATION_RECONCILIATION_OPERATION
        assert "scene_001_V1" in payload["affected_blocks"]
        assert len(payload["timing_violations"]) == 1
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is False
        # Report is persisted even on failure — the dashboard needs
        # to show the drift values after a recovery re-entry.
        report = json.loads(state[NARRATION_RECONCILIATION_STATE_KEY])
        assert report[0]["verdict"] == "fail"

    def test_suppress_raise_returns_results_list(self):
        state = _single_block_state(scripted_sec=10.0, measured_sec=4.0)
        results = run_narration_reconciliation(
            state, raise_on_failure=False,
        )
        assert len(results) == 1
        assert results[0].is_failure()
        assert collect_failures(results) == results
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is False

    def test_multi_block_per_block_verdicts(self):
        state = _build_state(
            scripted={
                "scene_001_V1": 10.0,
                "scene_001_V2": 8.0,
                "scene_002_V1": 12.0,
            },
            measured={
                "scene_001_V1": 10.5,   # pass
                "scene_001_V2": 3.0,    # FAIL (-5 s)
                "scene_002_V1": 12.0,   # pass
            },
            blocks=[
                {"block_id": "scene_001_V1", "scene_num": 1,
                 "voice_role": "V1", "language": ""},
                {"block_id": "scene_001_V2", "scene_num": 1,
                 "voice_role": "V2", "language": ""},
                {"block_id": "scene_002_V1", "scene_num": 2,
                 "voice_role": "V1", "language": ""},
            ],
        )
        with pytest.raises(NarrationReconciliationFailure) as excinfo:
            run_narration_reconciliation(state)
        payload = excinfo.value.diagnostic_data()
        assert payload["affected_blocks"] == ["scene_001_V2"]

    def test_dual_lang_block_id_strips_language_suffix(self):
        # Dual-lang pipeline emits block_id "scene_001_V1_RU" but the
        # alignment key is often "scene_001_V1_RU" too; the fallback
        # only triggers when the suffix diverges. Happy path first.
        state = _build_state(
            scripted={"scene_001_V1": 10.0},
            measured={"scene_001_V1_RU": 10.3},
            blocks=[{
                "block_id": "scene_001_V1_RU",
                "scene_num": 1,
                "voice_role": "V1",
                "language": "ru",
            }],
        )
        results = run_narration_reconciliation(
            state, raise_on_failure=False,
        )
        assert len(results) == 1
        assert results[0].verdict is NarrationTimingVerdict.PASS

    def test_missing_alignment_for_block_is_skip_not_fail(self):
        # The block is in the QA list but WhisperX didn't align it.
        # Reconciliation should mark it SKIP (cannot judge) — not
        # FAIL, because regenerating the block won't create an
        # alignment that never existed.
        state = _build_state(
            scripted={"scene_001_V1": 10.0},
            measured={},  # no alignment entry
            blocks=[{
                "block_id": "scene_001_V1",
                "scene_num": 1,
                "voice_role": "V1",
                "language": "",
            }],
        )
        results = run_narration_reconciliation(state)
        assert results[0].verdict is NarrationTimingVerdict.SKIP
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is True

    def test_malformed_alignment_json_fails_loud(self):
        state = {
            "_voice_budgets": "{}",
            "whisperx_alignment": "{not json",
            "_stylistic_qa_blocks": "[]",
        }
        with pytest.raises(ValueError):
            run_narration_reconciliation(
                state,
                blocks=[{
                    "block_id": "scene_001_V1", "scene_num": 1,
                    "voice_role": "V1", "language": "",
                }],
            )

    def test_malformed_budget_value_fails_loud(self):
        state = {
            "_voice_budgets": json.dumps({"scene_001_V1": "not-a-number"}),
            "whisperx_alignment": json.dumps({
                "scene_001_V1": {"total_duration": 10.0}
            }),
            "_stylistic_qa_blocks": "[]",
        }
        with pytest.raises(ValueError):
            run_narration_reconciliation(
                state,
                blocks=[{
                    "block_id": "scene_001_V1", "scene_num": 1,
                    "voice_role": "V1", "language": "",
                }],
            )


# ---------------------------------------------------------------------------
# Stage-boundary after_agent_callback
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, state):
        self.state = state


class TestAfterAgentCallback:

    def test_non_audio_phase_is_noop(self):
        state = _single_block_state(scripted_sec=10.0, measured_sec=4.0)
        state["pipeline_phase"] = "visual_direction"
        # Would otherwise raise, but phase gate suppresses.
        assert (
            narration_reconciliation_after_agent_callback(_Ctx(state))
            is None
        )
        assert NARRATION_RECONCILIATION_STATE_KEY not in state

    def test_audio_phase_happy_path(self):
        state = _single_block_state(scripted_sec=10.0, measured_sec=10.2)
        assert (
            narration_reconciliation_after_agent_callback(_Ctx(state))
            is None
        )
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is True

    def test_audio_phase_raises_on_fail(self):
        state = _single_block_state(scripted_sec=10.0, measured_sec=4.0)
        with pytest.raises(NarrationReconciliationFailure):
            narration_reconciliation_after_agent_callback(_Ctx(state))
        assert state[NARRATION_RECONCILIATION_PASSED_KEY] is False

    def test_none_state_is_noop(self):
        class _NullCtx:
            state = None

        assert (
            narration_reconciliation_after_agent_callback(_NullCtx())
            is None
        )


# ---------------------------------------------------------------------------
# ADK Agent factory smoke test
# ---------------------------------------------------------------------------


def test_build_narration_reconciliation_agent():
    agent = build_narration_reconciliation_agent()
    assert agent is not None
    assert getattr(agent, "name", None) == "narration_reconciliation_agent"
    tools = list(getattr(agent, "tools", []) or [])
    tool_names = {getattr(t, "__name__", "") for t in tools}
    assert "reconcile_block" in tool_names
    assert "run_narration_reconciliation" in tool_names
    cb = getattr(agent, "after_agent_callback", None)
    assert cb is not None
    assert getattr(agent, "output_key", None) == NARRATION_RECONCILIATION_STATE_KEY


def test_recovery_operation_name_is_stable():
    # The audio ladder matches on this name; changing it breaks
    # downstream recovery. Pin the value with a test.
    assert NARRATION_RECONCILIATION_OPERATION == "audio_narration_reconciliation"


def test_default_tolerance_constants_are_stable():
    # Tolerances are a contract with the audio ladder — pin the
    # values so an accidental tweak surfaces in CI, not in prod.
    assert DEFAULT_TOLERANCE_RATIO == 0.15
    assert DEFAULT_ABS_TOLERANCE_SEC == 0.25
