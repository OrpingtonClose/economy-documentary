"""Audio experiment factory for component 04 (``render_audio`` tool).

Assembles the :class:`Experiment` that the strands-evals runner consumes
for ``docs/strands-migration/components/04-audio-agent.md``.

Five cases:

1. ``basic_3_scenes`` — three short scenes, one voice each.
2. ``long_scene_45s`` — one 45 s scene with a single voice (tests the
   duration ceiling used by the timing loop).
3. ``multi_voice_blocks`` — one scene with V1 / V2 / V3 dialogue.
4. ``tts_transient_failure`` — first TTS call fails, second succeeds.
   The tool re-raises; the DeepAgent orchestrator is responsible for the
   retry (component 14). The runner must treat the raised
   :class:`RuntimeError` as the case outcome and surface it to the
   evaluators via ``actual_output = {"label": "runtime_error", ...}``.
5. ``tts_persistent_failure`` — every TTS call fails. Must raise
   :class:`RuntimeError` with no partial state written.

Three-evaluator stack:

- :class:`ContractComplianceEvaluator` (hard gate, 1.00) against
  :data:`AUDIO_CONTRACT`.
- :class:`AudioInvariantEvaluator` (hard gate, 1.00) — per-clip LUFS /
  peak / click / plosive / continuity / role-consistency / hiss-floor
  checks. Simulation runs read real fixture WAVs; unit tests stub the
  evaluator out.
- :class:`CritiqueStoreEvaluator` (soft gate, ≥0.75) — bridges the
  artifact critique ledger so scoped overrides register as ``SKIP``.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment

from contracts import AUDIO_CONTRACT
from critique.store import get_critique_store
from strands_agents.evals.evaluators import (
    AudioInvariantEvaluator,
    ContractComplianceEvaluator,
    CritiqueStoreEvaluator,
)


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second tuple position indicates a hard gate.
AUDIO_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "AudioInvariantEvaluator": (1.0, True),
    "CritiqueStoreEvaluator": (0.75, False),
}


def _scene(
    scene_id: int,
    *,
    voices: list[tuple[str, str]],
    target: float = 20.0,
) -> dict[str, Any]:
    """Build a scene dict of the shape ``render_audio`` expects."""
    return {
        "id": scene_id,
        "target_duration_sec": float(target),
        "voices": [
            {"voice_id": role, "text": text, "pronunciation_hints": {}}
            for role, text in voices
        ],
        "pronunciation_hints": {},
    }


def _block(
    scene_num: int,
    voice_role: str,
    *,
    wav_path: str,
    duration: float,
    voice_id: str | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Build a narration-block dict mirroring what ``render_audio`` emits."""
    return {
        "block_id": f"scene_{scene_num:03d}_{voice_role}",
        "wav_path": wav_path,
        "scene_num": scene_num,
        "voice_role": voice_role,
        "language": language,
        "voice_id": voice_id or voice_role,
        "b2_url": f"https://b2.example.com/audio/scene_{scene_num:03d}_{voice_role}.wav",
        "duration_sec": duration,
    }


def _basic_3_scenes() -> Case:
    scenes = [
        _scene(1, voices=[("V1", "Inflation erodes the purchasing power of money.")], target=18.0),
        _scene(2, voices=[("V1", "Central banks target a small positive inflation rate.")], target=19.0),
        _scene(3, voices=[("V1", "But runaway inflation destroys savings and trust.")], target=17.0),
    ]
    blocks = [
        _block(1, "V1", wav_path="/tmp/documentary-pipeline/audio/scene_001_V1.wav", duration=18.1),
        _block(2, "V1", wav_path="/tmp/documentary-pipeline/audio/scene_002_V1.wav", duration=18.9),
        _block(3, "V1", wav_path="/tmp/documentary-pipeline/audio/scene_003_V1.wav", duration=16.8),
    ]
    whisperx_alignment = {
        "total_duration_sec": 53.8,
        "per_clip": {b["block_id"]: {"total_duration": b["duration_sec"], "word_count": 8, "words": []} for b in blocks},
        "language": "en",
    }
    return Case(
        input={"scenes": scenes, "voice_map": None, "language": "en"},
        expected_output={
            "scenes": scenes,
            "whisperx_alignment": whisperx_alignment,
            "narration_blocks": blocks,
            "label": "success",
        },
        metadata={"case_name": "basic_3_scenes"},
    )


def _long_scene_45s() -> Case:
    scenes = [
        _scene(
            1,
            voices=[
                (
                    "V1",
                    "A forty-five second monologue on the velocity of money across a long-form "
                    "documentary scene with deliberate pacing for viewer comprehension.",
                )
            ],
            target=45.0,
        ),
    ]
    blocks = [
        _block(1, "V1", wav_path="/tmp/documentary-pipeline/audio/scene_001_V1.wav", duration=44.7),
    ]
    whisperx_alignment = {
        "total_duration_sec": 44.7,
        "per_clip": {
            "scene_001_V1": {
                "total_duration": 44.7,
                "word_count": 23,
                "words": [],
            }
        },
        "language": "en",
    }
    return Case(
        input={"scenes": scenes, "voice_map": None, "language": "en"},
        expected_output={
            "scenes": scenes,
            "whisperx_alignment": whisperx_alignment,
            "narration_blocks": blocks,
            "label": "success",
        },
        metadata={"case_name": "long_scene_45s"},
    )


def _multi_voice_blocks() -> Case:
    scenes = [
        _scene(
            1,
            voices=[
                ("V1", "Narrator sets the scene."),
                ("V2", "Expert commentary adds historical context."),
                ("V3", "A skeptic challenges the premise."),
            ],
            target=24.0,
        ),
    ]
    blocks = [
        _block(
            1,
            "V1",
            wav_path="/tmp/documentary-pipeline/audio/scene_001_V1.wav",
            duration=7.6,
            voice_id="qwen3-tts:male_01",
        ),
        _block(
            1,
            "V2",
            wav_path="/tmp/documentary-pipeline/audio/scene_001_V2.wav",
            duration=9.2,
            voice_id="qwen3-tts:female_01",
        ),
        _block(
            1,
            "V3",
            wav_path="/tmp/documentary-pipeline/audio/scene_001_V3.wav",
            duration=6.8,
            voice_id="qwen3-tts:male_02",
        ),
    ]
    voice_map = {"V1": "qwen3-tts:male_01", "V2": "qwen3-tts:female_01", "V3": "qwen3-tts:male_02"}
    whisperx_alignment = {
        "total_duration_sec": 23.6,
        "per_clip": {b["block_id"]: {"total_duration": b["duration_sec"], "word_count": 5, "words": []} for b in blocks},
        "language": "en",
    }
    return Case(
        input={"scenes": scenes, "voice_map": voice_map, "language": "en"},
        expected_output={
            "scenes": scenes,
            "whisperx_alignment": whisperx_alignment,
            "narration_blocks": blocks,
            "label": "success",
        },
        metadata={"case_name": "multi_voice_blocks"},
    )


def _tts_transient_failure() -> Case:
    """TTS fails the first attempt, succeeds on retry.

    ``render_audio`` itself re-raises on any helper failure — it is the
    orchestrator's job (component 14) to retry. The expected label is
    ``runtime_error`` so a regression that silently degrades will flunk
    the ``Equals`` evaluator.
    """
    scenes = [
        _scene(1, voices=[("V1", "First attempt will fail.")], target=15.0),
    ]
    return Case(
        input={
            "scenes": scenes,
            "voice_map": None,
            "language": "en",
            "failure_mode": "tts_transient_failure",
        },
        expected_output={"label": "runtime_error"},
        metadata={"case_name": "tts_transient_failure"},
    )


def _tts_persistent_failure() -> Case:
    scenes = [
        _scene(1, voices=[("V1", "Every attempt will fail.")], target=15.0),
    ]
    return Case(
        input={
            "scenes": scenes,
            "voice_map": None,
            "language": "en",
            "failure_mode": "tts_persistent_failure",
        },
        expected_output={"label": "runtime_error"},
        metadata={"case_name": "tts_persistent_failure"},
    )


def audio_cases() -> list[Case]:
    """Return the canonical five ``render_audio`` cases."""
    return [
        _basic_3_scenes(),
        _long_scene_45s(),
        _multi_voice_blocks(),
        _tts_transient_failure(),
        _tts_persistent_failure(),
    ]


def audio_evaluators() -> list[Evaluator]:
    """Return the audio evaluator stack in spec order."""
    return [
        ContractComplianceEvaluator(AUDIO_CONTRACT),
        AudioInvariantEvaluator(),
        CritiqueStoreEvaluator(store=get_critique_store()),
    ]


def build_audio_experiment() -> Experiment:
    """Build the audio experiment for the strands-evals runner."""
    return Experiment(
        cases=audio_cases(),
        evaluators=audio_evaluators(),
    )


def audio_task(case: Case) -> dict[str, Any]:
    """Replay task adapter for the component-playground surface.

    Returns the case's canonical envelope so the evaluate endpoint can
    score a known-good payload against this component's evaluator
    stack without a live agent run. A live runner can replace this
    once provider plumbing lands in the playground.
    """
    metadata = case.metadata or {}
    expected_output: Any = (
        case.expected_output if case.expected_output is not None else {}
    )
    trajectory = case.expected_trajectory
    if trajectory is None:
        trajectory = metadata.get("canonical_trajectory")
    if trajectory is None:
        trajectory = []
    return {
        "output": expected_output,
        "trajectory": list(trajectory),
        "metadata": {"mode": "replay", "case": case.name},
    }


__all__ = [
    "AUDIO_EVALUATOR_THRESHOLDS",
    "audio_cases",
    "audio_evaluators",
    "audio_task",
    "build_audio_experiment",
]
