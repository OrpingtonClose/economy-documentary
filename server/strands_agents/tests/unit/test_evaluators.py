"""Unit tests for the deterministic evaluators in PR 1/3.

These evaluators wrap existing quality systems; the tests verify the
wrapping contract, not the inner logic (which has its own test suites
under ``tests/unit/`` and ``server/tools/``).

No LLM calls, no network, no GPU workers.
"""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path
from typing import Any

import pytest
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from unittest.mock import MagicMock, patch

from contracts import SCENARIO_CONTRACT, AUDIO_CONTRACT, StageContract
from critique.record import ArtifactCritiqueRecord, QaVerdict
from critique.store import ArtifactCritiqueStore
from strands_agents.evals.evaluators import (
    ApprovalGateTrajectoryEvaluator,
    AudioInvariantEvaluator,
    AudioWorkerInvariantEvaluator,
    ContractComplianceEvaluator,
    CritiqueStoreEvaluator,
    EscalationDecisionEvaluator,
    MemoryHonoringEvaluator,
    ParallelLaunchEvaluator,
    PipelineTrajectoryEvaluator,
    ScenarioQualityEvaluator,
    TimelineComplianceEvaluator,
    VisualCoherenceEvaluator,
)
from strands_agents.evals.evaluators.escalation_decision import EscalationDecisionRating
from strands_agents.evals.evaluators.visual_coherence import VisualCoherenceRating


# ---------------------------------------------------------------------------
# ScenarioQualityEvaluator
# ---------------------------------------------------------------------------


def _make_scenario(
    *,
    num_scenes: int = 5,
    with_hooks: bool = True,
    with_style_lock: bool = True,
    narration_words: int = 130,
) -> dict[str, Any]:
    narration = " ".join(["monetary"] * narration_words)
    scenes: list[dict[str, Any]] = []
    for i in range(num_scenes):
        scene: dict[str, Any] = {
            "scene_num": i + 1,
            "narration": narration,
            "visual_direction": "A wide cinematic shot of a trading floor.",
            "duration_sec": 45.0,
            "pronunciation_hints": {},
        }
        if with_hooks and i == 0:
            scene["hook_spec"] = {
                "topic_specific_motif": "a neon-lit stock ticker running real-time inflation prints",
                "motion_description": "slow push-in on the ticker over 2 seconds",
                "narrative_pull": "because the numbers decide whether rent goes up next month",
            }
        if i == num_scenes - 1:
            scene["outro_spec"] = {
                "closing_shot": "wide shot of an empty trading floor at dusk",
                "recap_sentence": "Inflation is a tax paid by everyone.",
                "cta": "subscribe for more economics deep dives",
                "brand_card": "The Economy Explained",
            }
        scenes.append(scene)

    scenario: dict[str, Any] = {"scenes": scenes, "pronunciation_hints": {}}
    if with_style_lock:
        scenario["style_lock"] = {
            "dominant_style": "cinematic_documentary",
            "positive_fragment": "natural color grade, handheld motion, documentary realism",
        }
    return scenario


def test_scenario_quality_returns_one_output_per_check() -> None:
    evaluator = ScenarioQualityEvaluator()
    case = EvaluationData[str, dict[str, Any]](
        input="inflation basics",
        actual_output=_make_scenario(),
        metadata={"target_duration_sec": 225.0, "wpm": 150},
    )

    outputs = evaluator.evaluate(case)

    # run_all_structural_checks emits exactly 10 check categories today
    # (duration, scene_count, word_count, hook, outro, style_lock,
    # style_consistency, pronunciation_hints, rhetorical, topic_fidelity).
    assert len(outputs) == 10
    assert all(isinstance(o, EvaluationOutput) for o in outputs)
    labels = {o.label for o in outputs}
    for required in (
        "duration_compliance",
        "scene_count",
        "word_count",
        "hook_spec_present",
        "outro_spec_present",
        "style_lock_present",
        "style_consistency",
        "pronunciation_hints_coverage",
        "no_rhetorical_questions",
        "topic_fidelity",
    ):
        assert required in labels, f"missing check {required!r}"


def test_scenario_quality_flags_missing_style_lock_as_hard_fail() -> None:
    evaluator = ScenarioQualityEvaluator()
    case = EvaluationData[str, dict[str, Any]](
        input="inflation basics",
        actual_output=_make_scenario(with_style_lock=False),
        metadata={"target_duration_sec": 225.0},
    )

    outputs = evaluator.evaluate(case)
    style_lock_outputs = [o for o in outputs if o.label == "style_lock_present"]
    assert len(style_lock_outputs) == 1
    # style_lock missing caps at POOR → hard-fail the case.
    assert style_lock_outputs[0].test_pass is False
    assert style_lock_outputs[0].score == 0.0


def test_scenario_quality_empty_scenario_fails_multiple_checks() -> None:
    evaluator = ScenarioQualityEvaluator()
    case = EvaluationData[str, dict[str, Any]](
        input="",
        actual_output={"scenes": []},
        metadata={"target_duration_sec": 225.0},
    )

    outputs = evaluator.evaluate(case)
    # At least duration_compliance, scene_count, word_count, hook,
    # outro, style_lock should fail on an empty scenario.
    failures = [o for o in outputs if not o.test_pass]
    assert len(failures) >= 5


# ---------------------------------------------------------------------------
# AudioInvariantEvaluator
# ---------------------------------------------------------------------------


def _write_silent_wav(path: Path, *, duration_sec: float = 1.0) -> None:
    sample_rate = 24000
    n_frames = int(duration_sec * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<" + "h" * n_frames, *([0] * n_frames)))


def test_audio_invariant_empty_blocks_hard_fails(tmp_path: Path) -> None:
    evaluator = AudioInvariantEvaluator()
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={"narration_blocks": []},
    )

    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "audio_invariants.empty"


def test_audio_invariant_emits_outputs_for_each_block(tmp_path: Path) -> None:
    wav1 = tmp_path / "scene_001_V1_EN.wav"
    wav2 = tmp_path / "scene_002_V1_EN.wav"
    _write_silent_wav(wav1)
    _write_silent_wav(wav2)

    evaluator = AudioInvariantEvaluator()
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={
            "narration_blocks": [
                {
                    "block_id": "scene_001_V1_EN",
                    "wav_path": str(wav1),
                    "scene_num": 1,
                    "voice_role": "V1",
                    "language": "en",
                    "voice_id": "qwen3-tts:male_01",
                },
                {
                    "block_id": "scene_002_V1_EN",
                    "wav_path": str(wav2),
                    "scene_num": 2,
                    "voice_role": "V1",
                    "language": "en",
                    "voice_id": "qwen3-tts:male_01",
                },
            ]
        },
    )

    outputs = evaluator.evaluate(case)
    # Per-block checks run 4 invariants × 2 blocks = 8, plus cross-block
    # checks (voice_continuity + hiss_floor × 1 pair = 2), plus per-role
    # character-voice-consistency = 1 → at least 11 outputs.
    assert len(outputs) >= 11
    assert all(isinstance(o, EvaluationOutput) for o in outputs)

    # Silent WAV will LUFS-fail (too quiet), but every InvariantResult
    # must still produce an output with a non-empty reason.
    assert all(o.reason for o in outputs)


def test_audio_invariant_rejects_malformed_block() -> None:
    evaluator = AudioInvariantEvaluator()
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={"narration_blocks": ["not-a-dict"]},
    )
    with pytest.raises(TypeError):
        evaluator.evaluate(case)


# ---------------------------------------------------------------------------
# TimelineComplianceEvaluator
# ---------------------------------------------------------------------------


def test_timeline_compliance_missing_path_fails() -> None:
    evaluator = TimelineComplianceEvaluator()
    case = EvaluationData[str, dict[str, Any]](input="")
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "timeline_loaded"


def test_timeline_compliance_nonexistent_file_fails(tmp_path: Path) -> None:
    evaluator = TimelineComplianceEvaluator()
    case = EvaluationData[str, dict[str, Any]](
        input=str(tmp_path / "does_not_exist.otio"),
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert "not found" in (outputs[0].reason or "")


def test_timeline_compliance_valid_timeline_passes(tmp_path: Path) -> None:
    opentimelineio = pytest.importorskip("opentimelineio")

    tl = opentimelineio.schema.Timeline(name="documentary")
    narration = opentimelineio.schema.Track(name="narration")
    video = opentimelineio.schema.Track(name="video")
    clip_range = opentimelineio.opentime.TimeRange(
        start_time=opentimelineio.opentime.RationalTime(0, 24),
        duration=opentimelineio.opentime.RationalTime(240, 24),  # 10 sec
    )
    # Use gaps (no media references) to avoid touching the filesystem.
    narration.append(opentimelineio.schema.Gap(source_range=clip_range))
    video.append(opentimelineio.schema.Gap(source_range=clip_range))
    tl.tracks.append(narration)
    tl.tracks.append(video)

    path = tmp_path / "ok.otio"
    opentimelineio.adapters.write_to_file(tl, str(path))

    evaluator = TimelineComplianceEvaluator()
    case = EvaluationData[str, dict[str, Any]](input=str(path))
    outputs = evaluator.evaluate(case)

    labels = {o.label: o for o in outputs}
    assert labels["timeline_loaded"].test_pass is True
    assert labels["no_negative_duration"].test_pass is True
    assert labels["media_references"].test_pass is True
    # Gaps are not Clips — consistency check is trivially true here.
    assert labels["track_consistency"].test_pass is True


# ---------------------------------------------------------------------------
# ContractComplianceEvaluator
# ---------------------------------------------------------------------------


def test_contract_compliance_passing_scenario() -> None:
    evaluator = ContractComplianceEvaluator(SCENARIO_CONTRACT)
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={"scenes": [{"scene_num": 1}]},  # non-placeholder
    )
    outputs = evaluator.evaluate(case)
    # SCENARIO_CONTRACT has 0 required_state, 1 produced_state, 0 artifacts.
    assert len(outputs) == 1
    assert outputs[0].test_pass is True
    assert outputs[0].label == "produced_state.scenes"


def test_contract_compliance_placeholder_value_fails() -> None:
    evaluator = ContractComplianceEvaluator(SCENARIO_CONTRACT)
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={"scenes": "(not yet generated)"},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False


def test_contract_compliance_artifact_glob_matches(tmp_path: Path) -> None:
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "scene_001.wav").write_bytes(b"RIFF")

    evaluator = ContractComplianceEvaluator(AUDIO_CONTRACT)
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={
            "scenes": [{"scene_num": 1}],
            "whisperx_alignment": {"segments": []},
        },
        metadata={"artifact_root": str(tmp_path)},
    )
    outputs = evaluator.evaluate(case)
    artifact_checks = [o for o in outputs if o.label.startswith("produced_artifacts.")]
    assert len(artifact_checks) == 1
    assert artifact_checks[0].test_pass is True


def test_contract_compliance_empty_artifact_fails(tmp_path: Path) -> None:
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "scene_001.wav").write_bytes(b"")

    evaluator = ContractComplianceEvaluator(AUDIO_CONTRACT)
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={
            "scenes": [{"scene_num": 1}],
            "whisperx_alignment": {"segments": []},
        },
        metadata={"artifact_root": str(tmp_path)},
    )
    outputs = evaluator.evaluate(case)
    artifact_checks = [o for o in outputs if o.label.startswith("produced_artifacts.")]
    assert len(artifact_checks) == 1
    assert artifact_checks[0].test_pass is False
    assert "empty" in artifact_checks[0].reason.lower()


def test_contract_compliance_empty_contract_passes() -> None:
    empty = StageContract(name="empty")
    evaluator = ContractComplianceEvaluator(empty)
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is True
    assert outputs[0].label.endswith(".empty")


# ---------------------------------------------------------------------------
# CritiqueStoreEvaluator
# ---------------------------------------------------------------------------


def _write_record(
    store_root: Path,
    *,
    artifact_type: str,
    artifact_id: str,
    verdict: str,
) -> None:
    record = ArtifactCritiqueRecord(
        artifact_type=artifact_type,  # type: ignore[arg-type]
        artifact_id=artifact_id,
    )
    if verdict:
        record.qa_results.append(
            QaVerdict(
                source="test",
                check_name="unit_test",
                verdict=verdict,  # type: ignore[arg-type]
            )
        )
    # Match ArtifactCritiqueStore.write layout: <root>/critiques/<type>/<id>.json
    critiques_dir = store_root / "critiques" / artifact_type
    critiques_dir.mkdir(parents=True, exist_ok=True)
    (critiques_dir / f"{artifact_id}.json").write_text(
        json.dumps(record.to_dict()), encoding="utf-8"
    )


def _store_at(path: Path) -> ArtifactCritiqueStore:
    return ArtifactCritiqueStore(root=path)


def test_critique_store_missing_metadata_hard_fails(tmp_path: Path) -> None:
    evaluator = CritiqueStoreEvaluator(store=_store_at(tmp_path))
    case = EvaluationData[str, dict[str, Any]](input="scene_001")
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert "artifact_type" in (outputs[0].reason or "")


def test_critique_store_missing_record_hard_fails(tmp_path: Path) -> None:
    evaluator = CritiqueStoreEvaluator(store=_store_at(tmp_path))
    case = EvaluationData[str, dict[str, Any]](
        input="scene_001",
        metadata={"artifact_type": "scene"},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "critique.missing"


@pytest.mark.parametrize(
    ("verdict", "expected_score", "expected_pass"),
    [
        ("pass", 1.0, True),
        ("warn", 0.75, True),
        ("escalate", 0.5, False),
        ("fail", 0.0, False),
    ],
)
def test_critique_store_maps_verdict_to_score(
    tmp_path: Path,
    verdict: str,
    expected_score: float,
    expected_pass: bool,
) -> None:
    _write_record(tmp_path, artifact_type="scene", artifact_id="scene_001", verdict=verdict)
    evaluator = CritiqueStoreEvaluator(store=_store_at(tmp_path))
    case = EvaluationData[str, dict[str, Any]](
        input="scene_001",
        metadata={"artifact_type": "scene"},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].score == expected_score
    assert outputs[0].test_pass is expected_pass
    assert verdict in (outputs[0].label or "")


def test_critique_store_empty_record_defaults_to_pass(tmp_path: Path) -> None:
    """A record with zero QaVerdicts is benign — worst_status([]) == 'pass'."""
    _write_record(tmp_path, artifact_type="scene", artifact_id="scene_002", verdict="")
    evaluator = CritiqueStoreEvaluator(store=_store_at(tmp_path))
    case = EvaluationData[str, dict[str, Any]](
        input="scene_002",
        metadata={"artifact_type": "scene"},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].score == 1.0
    assert outputs[0].test_pass is True


# ---------------------------------------------------------------------------
# VisualCoherenceEvaluator (LLM-as-judge, mocked)
# ---------------------------------------------------------------------------


def _mock_agent_factory(rating_obj: Any):
    """Build a patcher that makes `Agent(...)(prompt, ...)` return rating_obj."""

    class _FakeResult:
        def __init__(self, structured: Any) -> None:
            self.structured_output = structured

    agent_instance = MagicMock()
    agent_instance.return_value = _FakeResult(rating_obj)
    factory = MagicMock(return_value=agent_instance)
    return factory, agent_instance


def test_visual_coherence_no_concepts_hard_fails() -> None:
    evaluator = VisualCoherenceEvaluator()
    case = EvaluationData[str, dict[str, Any]](
        input="inflation",
        actual_output={"visual_concepts": []},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "UNKNOWN"


@pytest.mark.parametrize(
    ("rating_label", "expected_score", "expected_pass"),
    [
        ("EXCELLENT", 1.0, True),
        ("GOOD", 0.75, True),
        ("FAIR", 0.5, True),
        ("POOR", 0.25, False),
        ("UNKNOWN", 0.0, False),
    ],
)
def test_visual_coherence_maps_rating_to_score(
    rating_label: str,
    expected_score: float,
    expected_pass: bool,
) -> None:
    rating = VisualCoherenceRating(reasoning="test rationale", rating=rating_label)
    factory, _ = _mock_agent_factory(rating)

    with patch(
        "strands_agents.evals.evaluators.visual_coherence.Agent",
        factory,
    ):
        evaluator = VisualCoherenceEvaluator()
        case = EvaluationData[str, dict[str, Any]](
            input="inflation",
            actual_output={
                "visual_concepts": [
                    {"scene_num": 1, "visual_direction": "a", "camera": "wide"},
                    {"scene_num": 2, "visual_direction": "b", "camera": "close"},
                ],
                "style_lock": {"dominant_style": "cinematic"},
            },
        )
        outputs = evaluator.evaluate(case)

    assert len(outputs) == 1
    assert outputs[0].score == expected_score
    assert outputs[0].test_pass is expected_pass
    assert outputs[0].label == rating_label
    assert outputs[0].reason == "test rationale"


def test_visual_coherence_unknown_label_clamps_to_unknown() -> None:
    rating = VisualCoherenceRating(reasoning="junk", rating="SPLENDID")
    factory, _ = _mock_agent_factory(rating)

    with patch(
        "strands_agents.evals.evaluators.visual_coherence.Agent",
        factory,
    ):
        evaluator = VisualCoherenceEvaluator()
        case = EvaluationData[str, dict[str, Any]](
            input="topic",
            actual_output={
                "visual_concepts": [{"scene_num": 1, "visual_direction": "x"}],
            },
        )
        outputs = evaluator.evaluate(case)

    assert outputs[0].label == "UNKNOWN"
    assert outputs[0].test_pass is False


def test_visual_coherence_judge_error_fails_soft() -> None:
    agent_instance = MagicMock(side_effect=RuntimeError("boom"))
    factory = MagicMock(return_value=agent_instance)

    with patch(
        "strands_agents.evals.evaluators.visual_coherence.Agent",
        factory,
    ):
        evaluator = VisualCoherenceEvaluator()
        case = EvaluationData[str, dict[str, Any]](
            input="topic",
            actual_output={
                "visual_concepts": [{"scene_num": 1, "visual_direction": "x"}],
            },
        )
        outputs = evaluator.evaluate(case)

    assert outputs[0].test_pass is False
    assert outputs[0].label == "UNKNOWN"
    assert "boom" in (outputs[0].reason or "")


# ---------------------------------------------------------------------------
# EscalationDecisionEvaluator (LLM-as-judge, mocked)
# ---------------------------------------------------------------------------


def test_escalation_missing_action_hard_fails() -> None:
    evaluator = EscalationDecisionEvaluator()
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={},
        metadata={"diagnostic": {"error": "timeout"}},
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "HARMFUL"


def test_escalation_unknown_action_hard_fails() -> None:
    evaluator = EscalationDecisionEvaluator()
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={"action": "teleport"},
        metadata={"diagnostic": {"error": "timeout"}},
    )
    outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert "teleport" in (outputs[0].reason or "")


def test_escalation_missing_diagnostic_hard_fails() -> None:
    evaluator = EscalationDecisionEvaluator()
    case = EvaluationData[dict[str, Any], dict[str, Any]](
        input={},
        actual_output={"action": "retry"},
    )
    outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert "diagnostic" in (outputs[0].reason or "")


@pytest.mark.parametrize(
    ("verdict", "expected_score", "expected_pass"),
    [
        ("CORRECT", 1.0, True),
        ("REASONABLE", 0.5, True),
        ("HARMFUL", 0.0, False),
    ],
)
def test_escalation_maps_verdict_to_score(
    verdict: str,
    expected_score: float,
    expected_pass: bool,
) -> None:
    rating = EscalationDecisionRating(reasoning="test", verdict=verdict)
    factory, _ = _mock_agent_factory(rating)
    with patch(
        "strands_agents.evals.evaluators.escalation_decision.Agent",
        factory,
    ):
        evaluator = EscalationDecisionEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={},
            actual_output={
                "action": "retry",
                "reasoning": "transient",
                "state_patches": {"attempts": 1},
            },
            metadata={"diagnostic": {"error": "network timeout", "attempt": 1}},
        )
        outputs = evaluator.evaluate(case)

    assert outputs[0].score == expected_score
    assert outputs[0].test_pass is expected_pass
    assert outputs[0].label == verdict


def test_escalation_judge_error_hard_fails() -> None:
    agent_instance = MagicMock(side_effect=RuntimeError("boom"))
    factory = MagicMock(return_value=agent_instance)
    with patch(
        "strands_agents.evals.evaluators.escalation_decision.Agent",
        factory,
    ):
        evaluator = EscalationDecisionEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={},
            actual_output={"action": "retry"},
            metadata={"diagnostic": {"error": "x"}},
        )
        outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert outputs[0].label == "HARMFUL"


# ---------------------------------------------------------------------------
# PipelineTrajectoryEvaluator
# ---------------------------------------------------------------------------


def test_trajectory_requires_expected_sequence() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=["a", "b"],
    )
    outputs = evaluator.evaluate(case)
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "trajectory.missing_expected"


def test_trajectory_subsequence_passes() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=["scenario", "audio", "timing", "assemble"],
        metadata={
            "expected_tool_sequence": ["scenario", "timing", "assemble"],
        },
    )
    outputs = evaluator.evaluate(case)
    coverage = next(o for o in outputs if o.label == "trajectory.coverage")
    order = next(o for o in outputs if o.label == "trajectory.order")
    assert coverage.test_pass is True
    assert coverage.score == 1.0
    assert order.test_pass is True


def test_trajectory_out_of_order_fails_order_check() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=["assemble", "scenario", "timing"],
        metadata={
            "expected_tool_sequence": ["scenario", "timing", "assemble"],
        },
    )
    outputs = evaluator.evaluate(case)
    coverage = next(o for o in outputs if o.label == "trajectory.coverage")
    order = next(o for o in outputs if o.label == "trajectory.order")
    assert coverage.test_pass is True  # all tools present
    assert order.test_pass is False  # but not in order


def test_trajectory_missing_tool_fails_coverage() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=["scenario", "timing"],
        metadata={
            "expected_tool_sequence": ["scenario", "timing", "assemble"],
        },
    )
    outputs = evaluator.evaluate(case)
    coverage = next(o for o in outputs if o.label == "trajectory.coverage")
    assert coverage.test_pass is False
    assert coverage.score == pytest.approx(2 / 3)


def test_trajectory_accepts_dict_shape() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        metadata={"expected_tool_sequence": ["a", "c"]},
    )
    outputs = evaluator.evaluate(case)
    order = next(o for o in outputs if o.label == "trajectory.order")
    assert order.test_pass is True


def test_trajectory_non_strict_skips_order_check() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=["c", "b", "a"],
        metadata={
            "expected_tool_sequence": ["a", "b", "c"],
            "strict_order": False,
        },
    )
    outputs = evaluator.evaluate(case)
    labels = {o.label for o in outputs}
    assert "trajectory.order" not in labels
    coverage = next(o for o in outputs if o.label == "trajectory.coverage")
    assert coverage.test_pass is True


def test_trajectory_duplicate_expected_requires_duplicate_actual() -> None:
    evaluator = PipelineTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=["scenario", "audio"],
        metadata={
            "expected_tool_sequence": ["scenario", "audio", "scenario"],
            "strict_order": False,
        },
    )
    outputs = evaluator.evaluate(case)
    coverage = next(o for o in outputs if o.label == "trajectory.coverage")
    assert coverage.test_pass is False
    assert "scenario" in (coverage.reason or "")


# ---------------------------------------------------------------------------
# ParallelLaunchEvaluator
# ---------------------------------------------------------------------------


def test_parallel_requires_config() -> None:
    evaluator = ParallelLaunchEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[{"name": "launch_tts", "at_turn": 1}],
    )
    outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert outputs[0].label == "parallel.missing_config"


def test_parallel_all_batched_passes() -> None:
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "plan", "at_turn": 0},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "await_tasks", "at_turn": 2},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={
            "tool_name": "launch_tts",
            "expected_count": 3,
            "completion_tool": "await_tasks",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.count"].test_pass is True
    assert by_label["parallel.batched"].test_pass is True
    assert by_label["parallel.awaited"].test_pass is True


def test_parallel_wrong_count_fails() -> None:
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={"tool_name": "launch_tts", "expected_count": 3},
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.count"].test_pass is False
    # Batched cares only about at_turn markers, not per-batch size, so it
    # still passes here — the count check is the one that surfaces the
    # under-dispatched batch.
    assert by_label["parallel.batched"].test_pass is True


def test_parallel_spread_across_turns_fails_count() -> None:
    # Serialising 3 launches across 3 turns forms 3 batches of 1 launch
    # each — every batch fails the per-batch expected_count check.
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 2},
        {"name": "launch_tts", "at_turn": 3},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={"tool_name": "launch_tts", "expected_count": 3},
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.count"].test_pass is False
    # Batched passes — each launch carries an at_turn marker; the
    # problem is per-batch size, not missing markers.
    assert by_label["parallel.batched"].test_pass is True


def test_parallel_multi_batch_iterations_pass() -> None:
    # Two iterations of a 3-scene loop: 2 batches × 3 launches each.
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "await_tasks", "at_turn": 2},
        {"name": "launch_tts", "at_turn": 3},
        {"name": "launch_tts", "at_turn": 3},
        {"name": "launch_tts", "at_turn": 3},
        {"name": "await_tasks", "at_turn": 4},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={
            "tool_name": "launch_tts",
            "expected_count": 3,
            "completion_tool": "await_tasks",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.count"].test_pass is True
    assert by_label["parallel.batched"].test_pass is True
    assert by_label["parallel.awaited"].test_pass is True


def test_parallel_multi_batch_one_short_batch_fails_count() -> None:
    # Second iteration only dispatched 2 of 3 — every batch is inspected.
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "await_tasks", "at_turn": 2},
        {"name": "launch_tts", "at_turn": 3},
        {"name": "launch_tts", "at_turn": 3},
        {"name": "await_tasks", "at_turn": 4},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={
            "tool_name": "launch_tts",
            "expected_count": 3,
            "completion_tool": "await_tasks",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.count"].test_pass is False
    # Both iterations were awaited, so await ordering is still OK.
    assert by_label["parallel.awaited"].test_pass is True


def test_parallel_multi_batch_missing_completion_for_second_iteration() -> None:
    # Second batch launched at turn 3 but no completion_tool call appears
    # at a strictly greater turn.
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "await_tasks", "at_turn": 2},
        {"name": "launch_tts", "at_turn": 3},
        {"name": "launch_tts", "at_turn": 3},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={
            "tool_name": "launch_tts",
            "expected_count": 2,
            "completion_tool": "await_tasks",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.count"].test_pass is True
    assert by_label["parallel.awaited"].test_pass is False


def test_parallel_missing_at_turn_on_some_launches_fails_batched() -> None:
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts"},  # missing at_turn
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={"tool_name": "launch_tts", "expected_count": 3},
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    # With per-batch semantics a missing at_turn can't be placed in a
    # batch, so both count and batched fail and both reasons cite the
    # missing marker.
    assert by_label["parallel.count"].test_pass is False
    assert "at_turn" in (by_label["parallel.count"].reason or "")
    assert by_label["parallel.batched"].test_pass is False
    assert "at_turn" in (by_label["parallel.batched"].reason or "")


def test_parallel_awaited_before_launch_fails() -> None:
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "await_tasks", "at_turn": 0},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={
            "tool_name": "launch_tts",
            "expected_count": 2,
            "completion_tool": "await_tasks",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.awaited"].test_pass is False


def test_parallel_awaited_missing_at_turn_fails() -> None:
    evaluator = ParallelLaunchEvaluator()
    trajectory = [
        {"name": "launch_tts", "at_turn": 1},
        {"name": "launch_tts", "at_turn": 1},
        {"name": "await_tasks"},  # missing at_turn - no ordering evidence
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={
            "tool_name": "launch_tts",
            "expected_count": 2,
            "completion_tool": "await_tasks",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["parallel.awaited"].test_pass is False


# ---------------------------------------------------------------------------
# MemoryHonoringEvaluator
# ---------------------------------------------------------------------------


def test_memory_requires_before_seed() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](input=None, actual_trajectory=[])
    outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert outputs[0].label == "memory.missing_seed"


def test_memory_ordering_honoured_passes() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[
            {"name": "launch_tts", "at_turn": 1},
            {"name": "launch_assembly", "at_turn": 5},
        ],
        metadata={
            "agents_md_before": "# AGENTS.md\n- Run TTS before assembly.\n",
            "agents_md_after": "# AGENTS.md\n- Run TTS before assembly.\n",
            "forbidden_sequences": [
                {"before": "launch_tts", "after": "launch_assembly"},
            ],
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["memory.order[launch_tts->launch_assembly]"].test_pass is True
    assert by_label["memory.integrity"].test_pass is True


def test_memory_ordering_violated_fails() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[
            {"name": "launch_assembly", "at_turn": 1},
            {"name": "launch_tts", "at_turn": 2},
        ],
        metadata={
            "agents_md_before": "seed",
            "forbidden_sequences": [
                {"before": "launch_tts", "after": "launch_assembly"},
            ],
        },
    )
    outputs = evaluator.evaluate(case)
    order_out = next(
        o for o in outputs
        if o.label == "memory.order[launch_tts->launch_assembly]"
    )
    assert order_out.test_pass is False


def test_memory_after_token_check() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[],
        metadata={
            "agents_md_before": "seed",
            "agents_md_after": "# AGENTS.md\n- Learned: LUFS floor is -23.\n",
            "required_tokens": ["Learned: LUFS floor"],
        },
    )
    outputs = evaluator.evaluate(case)
    token_outs = [o for o in outputs if o.label == "memory.required_token"]
    assert token_outs[0].test_pass is True


def test_memory_missing_required_token_fails() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[],
        metadata={
            "agents_md_before": "seed",
            "agents_md_after": "# AGENTS.md\n(no reflection written)\n",
            "required_tokens": ["Learned: LUFS floor"],
        },
    )
    outputs = evaluator.evaluate(case)
    token_out = next(o for o in outputs if o.label == "memory.required_token")
    assert token_out.test_pass is False


def test_memory_corrupted_after_fails_integrity() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[],
        metadata={
            "agents_md_before": "seed",
            "agents_md_after": "   ",  # blank
        },
    )
    outputs = evaluator.evaluate(case)
    integrity = next(o for o in outputs if o.label == "memory.integrity")
    assert integrity.test_pass is False


def test_memory_null_bytes_flagged_as_corrupted() -> None:
    evaluator = MemoryHonoringEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[],
        metadata={
            "agents_md_before": "seed",
            "agents_md_after": "# AGENTS.md\n\x00\x00garbage\x00",
        },
    )
    outputs = evaluator.evaluate(case)
    integrity = next(o for o in outputs if o.label == "memory.integrity")
    assert integrity.test_pass is False


def test_memory_high_non_printable_flagged_as_corrupted() -> None:
    evaluator = MemoryHonoringEvaluator()
    # bytes 0-255 decoded via latin-1 is a classic "binary-via-str" smuggling
    # vector; `.encode("utf-8")` alone does not catch it.
    payload = bytes(range(256)).decode("latin-1")
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[],
        metadata={
            "agents_md_before": "seed",
            "agents_md_after": payload,
        },
    )
    outputs = evaluator.evaluate(case)
    integrity = next(o for o in outputs if o.label == "memory.integrity")
    assert integrity.test_pass is False


# ---------------------------------------------------------------------------
# ApprovalGateTrajectoryEvaluator
# ---------------------------------------------------------------------------


def test_approval_requires_config() -> None:
    evaluator = ApprovalGateTrajectoryEvaluator()
    case = EvaluationData[Any, Any](input=None, actual_trajectory=[])
    outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert outputs[0].label == "approval.missing_config"


def test_approval_no_interrupt_fails() -> None:
    evaluator = ApprovalGateTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[{"name": "launch_b2_sync", "kind": "tool_call"}],
        metadata={
            "gated_tool": "launch_b2_sync",
            "expected_decision": "approve",
        },
    )
    outputs = evaluator.evaluate(case)
    assert outputs[0].test_pass is False
    assert outputs[0].label == "approval.raised"


def test_approval_approved_and_followed_through_passes() -> None:
    evaluator = ApprovalGateTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[
            {"kind": "interrupt", "tool": "launch_b2_sync", "decision": "approve"},
            {"kind": "tool_call", "name": "launch_b2_sync", "at_turn": 3},
        ],
        metadata={
            "gated_tool": "launch_b2_sync",
            "expected_decision": "approve",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["approval.raised"].test_pass is True
    assert by_label["approval.decision"].test_pass is True
    assert by_label["approval.followthrough"].test_pass is True


def test_approval_rejected_and_not_leaked_passes() -> None:
    evaluator = ApprovalGateTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[
            {"kind": "interrupt", "tool": "launch_b2_sync", "decision": "reject"},
            {"kind": "tool_call", "name": "notify_user"},
        ],
        metadata={
            "gated_tool": "launch_b2_sync",
            "expected_decision": "reject",
            "forbidden_on_reject": ["publish_to_youtube"],
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["approval.raised"].test_pass is True
    assert by_label["approval.decision"].test_pass is True
    assert by_label["approval.no_leak"].test_pass is True


def test_approval_rejected_but_gated_tool_still_ran_fails() -> None:
    evaluator = ApprovalGateTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[
            {"kind": "interrupt", "tool": "launch_b2_sync", "decision": "reject"},
            {"kind": "tool_call", "name": "launch_b2_sync"},
        ],
        metadata={
            "gated_tool": "launch_b2_sync",
            "expected_decision": "reject",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["approval.no_leak"].test_pass is False


def test_approval_decision_mismatch_fails() -> None:
    evaluator = ApprovalGateTrajectoryEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[
            {"kind": "interrupt", "tool": "launch_b2_sync", "decision": "approve"},
            {"kind": "tool_call", "name": "launch_b2_sync"},
        ],
        metadata={
            "gated_tool": "launch_b2_sync",
            "expected_decision": "reject",
        },
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["approval.decision"].test_pass is False


# ---------------------------------------------------------------------------
# AudioWorkerInvariantEvaluator
# ---------------------------------------------------------------------------


def test_audio_worker_no_calls_fails_voice_gate() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=[{"name": "plan", "at_turn": 0, "args": {}}],
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.voice_id_present"].test_pass is False


def test_audio_worker_single_voice_batch_passes() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s1", "voice_id": "V1"}},
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s2", "voice_id": "V1"}},
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s3", "voice_id": "V1"}},
        {"name": "await_tasks", "at_turn": 2, "args": {}},
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.voice_id_present"].test_pass is True
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is True
    assert "audio_worker.no_pool_rebind" not in by_label


def test_audio_worker_cross_voice_in_one_batch_fails() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s1", "voice_id": "V1"}},
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s2", "voice_id": "V2"}},
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is False


def test_audio_worker_cross_voice_expected_passes() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s1", "voice_id": "V1"}},
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s2", "voice_id": "V2"}},
    ]
    case = EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata={"expect_cross_voice_race": True},
    )
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is True


def test_audio_worker_multi_voice_serialised_across_turns_passes() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s1", "voice_id": "V1"}},
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s2", "voice_id": "V1"}},
        {"name": "await_tasks", "at_turn": 2, "args": {}},
        {"name": "launch_audio_render", "at_turn": 3, "args": {"scene_id": "s3", "voice_id": "V2"}},
        {"name": "launch_audio_render", "at_turn": 3, "args": {"scene_id": "s4", "voice_id": "V2"}},
        {"name": "await_tasks", "at_turn": 4, "args": {}},
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is True


def test_audio_worker_pool_rebind_fails() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {
            "name": "launch_audio_render",
            "at_turn": 1,
            "args": {"scene_id": "s1", "voice_id": "V1", "worker_pool": "pool-a"},
        },
        {
            "name": "launch_audio_render",
            "at_turn": 3,
            "args": {"scene_id": "s3", "voice_id": "V2", "worker_pool": "pool-a"},
        },
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    # Single-voice per batch so the batch gate passes.
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is True
    # But pool-a gets rebound from V1 → V2 across turns — that's the race.
    assert by_label["audio_worker.no_pool_rebind"].test_pass is False


def test_audio_worker_distinct_pools_per_voice_passes() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {
            "name": "launch_audio_render",
            "at_turn": 1,
            "args": {"scene_id": "s1", "voice_id": "V1", "worker_pool": "pool-a"},
        },
        {
            "name": "launch_audio_render",
            "at_turn": 1,
            "args": {"scene_id": "s2", "voice_id": "V2", "worker_pool": "pool-b"},
        },
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    # A cross-voice parallel batch still fails the batch gate even with
    # distinct worker_pool values — because the parallel-launch
    # evaluator cannot prove the LangGraph tool runner actually honoured
    # the pool routing. Serialise across turns to be safe.
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is False
    assert by_label["audio_worker.no_pool_rebind"].test_pass is True


def test_audio_worker_voice_map_single_voice_extracted() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {
            "name": "launch_audio_render",
            "at_turn": 1,
            "args": {"scene_id": "s1", "voice_map": {"narrator": "V1"}},
        },
        {
            "name": "launch_audio_render",
            "at_turn": 1,
            "args": {"scene_id": "s2", "voice_map": {"narrator": "V1"}},
        },
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.voice_id_present"].test_pass is True
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is True


def test_audio_worker_mixed_voice_map_fails_batch_gate() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {
            "name": "launch_audio_render",
            "at_turn": 1,
            "args": {"scene_id": "s1", "voice_map": {"narrator": "V1", "expert": "V2"}},
        },
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    # A scene that binds two voice_ids is itself a race — one VM cannot
    # voice two characters.
    assert by_label["audio_worker.no_cross_voice_in_batch"].test_pass is False


def test_audio_worker_missing_voice_id_fails_voice_gate() -> None:
    evaluator = AudioWorkerInvariantEvaluator()
    trajectory = [
        {"name": "launch_audio_render", "at_turn": 1, "args": {"scene_id": "s1"}},
    ]
    case = EvaluationData[Any, Any](input=None, actual_trajectory=trajectory)
    outputs = evaluator.evaluate(case)
    by_label = {o.label: o for o in outputs}
    assert by_label["audio_worker.voice_id_present"].test_pass is False
