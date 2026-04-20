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

from contracts import SCENARIO_CONTRACT, AUDIO_CONTRACT, StageContract
from critique.record import ArtifactCritiqueRecord, QaVerdict
from critique.store import ArtifactCritiqueStore
from strands_agents.evals.evaluators import (
    AudioInvariantEvaluator,
    ContractComplianceEvaluator,
    CritiqueStoreEvaluator,
    ScenarioQualityEvaluator,
    TimelineComplianceEvaluator,
)


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
