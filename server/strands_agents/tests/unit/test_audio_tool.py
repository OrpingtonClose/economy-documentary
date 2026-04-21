"""Unit tests for the audio Strands ``@tool`` (Component 04).

Covers:

* Deterministic helper protocol wiring and registry isolation.
* ``render_audio`` happy paths: single scene, multi-scene, multi-voice.
* Voice-map override (role → concrete ``voice_id``).
* Long-scene handling (duration ceiling).
* Input validation (empty ``scenes``, missing voices, empty text, bad id).
* Helper contract enforcement (bad return types, zero duration, empty URL).
* Fail-loud on helper exceptions (TTS, WhisperX, loudness, B2).
* Persistent TTS failure does not write partial state.
* State persistence through ``tool_context.agent.state``.
* Experiment factory shape and threshold table.

Every test is deterministic and offline — no LLM, no GPU, no network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from strands_agents.audio_tool import (
    AudioHelpersNotConfigured,
    TARGET_LUFS,
    _voice_role_of,
    clear_audio_helpers,
    render_audio,
    set_audio_helpers,
)
from strands_agents.evals.experiments.audio import (
    AUDIO_EVALUATOR_THRESHOLDS,
    audio_cases,
    audio_evaluators,
    build_audio_experiment,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scene(
    num: int,
    *,
    voices: list[tuple[str, str]] | None = None,
    target: float = 20.0,
) -> dict[str, Any]:
    return {
        "id": num,
        "target_duration_sec": target,
        "voices": [
            {"voice_id": role, "text": text}
            for role, text in (voices or [("V1", f"Narration for scene {num}.")])
        ],
    }


class _FakeTts:
    """TTS helper that returns a deterministic WAV path per call."""

    def __init__(
        self,
        *,
        fail_first_n: int = 0,
        fail_all: bool = False,
        base_dir: str = "/tmp/documentary-pipeline/audio",
    ) -> None:
        self.calls: list[tuple[int, str, str, str]] = []
        self._fail_first_n = fail_first_n
        self._fail_all = fail_all
        self._base_dir = base_dir

    def __call__(
        self, scene_num: int, voice_role: str, text: str, language: str
    ) -> str:
        self.calls.append((scene_num, voice_role, text, language))
        if self._fail_all:
            raise RuntimeError("tts: persistent failure")
        if len(self.calls) <= self._fail_first_n:
            raise RuntimeError("tts: transient failure")
        return f"{self._base_dir}/scene_{scene_num:03d}_{voice_role}.wav"


class _FakeWhisperx:
    """WhisperX helper that returns a fixed duration per call."""

    def __init__(
        self, *, duration: float = 5.0, word_count: int = 7, fail: bool = False
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._duration = duration
        self._word_count = word_count
        self._fail = fail

    def __call__(
        self, wav_path: str, text: str, language: str
    ) -> dict[str, Any]:
        self.calls.append((wav_path, text, language))
        if self._fail:
            raise RuntimeError("whisperx: align failed")
        return {
            "total_duration": self._duration,
            "word_count": self._word_count,
            "words": [],
        }


class _FakeLoudness:
    """Loudness helper that records invocations."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, float]] = []
        self._fail = fail

    def __call__(self, wav_path: str, target_lufs: float) -> None:
        self.calls.append((wav_path, target_lufs))
        if self._fail:
            raise RuntimeError("loudnorm: failure")


class _FakeB2:
    """B2 upload helper that returns deterministic URLs."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    def __call__(self, wav_path: str) -> str:
        self.calls.append(wav_path)
        if self._fail:
            raise RuntimeError("b2: upload failed")
        return f"https://b2.example.com/{wav_path.rsplit('/', 1)[-1]}"


@pytest.fixture(autouse=True)
def _isolate_helpers() -> None:
    """Ensure every test starts with an empty helper registry."""
    clear_audio_helpers()
    yield
    clear_audio_helpers()


def _install_helpers(
    *,
    tts: _FakeTts | None = None,
    whisperx: _FakeWhisperx | None = None,
    loudness: _FakeLoudness | None = None,
    b2: _FakeB2 | None = None,
) -> tuple[_FakeTts, _FakeWhisperx, _FakeLoudness, _FakeB2]:
    tts = tts or _FakeTts()
    whisperx = whisperx or _FakeWhisperx()
    loudness = loudness or _FakeLoudness()
    b2 = b2 or _FakeB2()
    set_audio_helpers(
        tts_generate=tts,
        whisperx_align=whisperx,
        loudness_normalize=loudness,
        b2_upload=b2,
    )
    return tts, whisperx, loudness, b2


# ---------------------------------------------------------------------------
# Helper registry
# ---------------------------------------------------------------------------


def test_render_audio_requires_helpers() -> None:
    with pytest.raises(AudioHelpersNotConfigured):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_clear_audio_helpers_restores_unconfigured_state() -> None:
    _install_helpers()
    clear_audio_helpers()
    with pytest.raises(AudioHelpersNotConfigured):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_set_audio_helpers_replaces_previous_registration() -> None:
    first_tts, *_ = _install_helpers()
    second_tts = _FakeTts(base_dir="/tmp/second")
    set_audio_helpers(
        tts_generate=second_tts,
        whisperx_align=_FakeWhisperx(),
        loudness_normalize=_FakeLoudness(),
        b2_upload=_FakeB2(),
    )
    render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)
    assert first_tts.calls == []
    assert second_tts.calls, "second TTS helper should have been invoked"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_basic_3_scenes_produces_one_block_per_voice() -> None:
    tts, whisperx, loudness, b2 = _install_helpers(
        whisperx=_FakeWhisperx(duration=6.5, word_count=10),
    )
    result = render_audio.__wrapped__(
        scenes=[_scene(1), _scene(2), _scene(3)],
        tool_context=None,
    )
    assert result["scene_count"] == 3
    assert result["block_count"] == 3
    assert len(result["narration_blocks"]) == 3
    assert len(tts.calls) == 3
    assert len(whisperx.calls) == 3
    assert len(loudness.calls) == 3
    assert len(b2.calls) == 3
    assert all(call[1] == TARGET_LUFS for call in loudness.calls)
    assert result["whisperx_alignment"]["total_duration_sec"] == pytest.approx(
        3 * 6.5, rel=1e-6
    )
    assert result["whisperx_alignment"]["language"] == "en"


def test_long_scene_preserves_duration() -> None:
    _install_helpers(whisperx=_FakeWhisperx(duration=44.7, word_count=23))
    scenes = [
        _scene(
            1,
            voices=[("V1", "A forty-five second monologue on the velocity of money.")],
            target=45.0,
        )
    ]
    result = render_audio.__wrapped__(scenes=scenes, tool_context=None)
    assert result["narration_blocks"][0]["duration_sec"] == pytest.approx(44.7)
    assert result["whisperx_alignment"]["per_clip"]["scene_001_V1"][
        "total_duration"
    ] == pytest.approx(44.7)


def test_multi_voice_scene_emits_one_block_per_voice_in_order() -> None:
    _install_helpers(whisperx=_FakeWhisperx(duration=7.0))
    scenes = [
        _scene(
            1,
            voices=[
                ("V1", "Narrator opens."),
                ("V2", "Expert weighs in."),
                ("V3", "Skeptic pushes back."),
            ],
        )
    ]
    result = render_audio.__wrapped__(scenes=scenes, tool_context=None)
    assert result["block_count"] == 3
    roles = [b["voice_role"] for b in result["narration_blocks"]]
    assert roles == ["V1", "V2", "V3"]
    block_ids = [b["block_id"] for b in result["narration_blocks"]]
    assert block_ids == ["scene_001_V1", "scene_001_V2", "scene_001_V3"]


def test_voice_map_overrides_concrete_voice_id() -> None:
    _install_helpers()
    voice_map = {"V1": "qwen3-tts:male_01", "V2": "qwen3-tts:female_01"}
    scenes = [
        _scene(
            1,
            voices=[("V1", "Narrator opens."), ("V2", "Expert weighs in.")],
        )
    ]
    result = render_audio.__wrapped__(
        scenes=scenes, voice_map=voice_map, tool_context=None
    )
    voice_ids = {b["voice_role"]: b["voice_id"] for b in result["narration_blocks"]}
    assert voice_ids == {"V1": "qwen3-tts:male_01", "V2": "qwen3-tts:female_01"}


def test_voice_map_falls_back_to_role_when_unmapped() -> None:
    _install_helpers()
    scenes = [
        _scene(1, voices=[("V1", "A"), ("V2", "B")]),
    ]
    result = render_audio.__wrapped__(
        scenes=scenes, voice_map={"V1": "mapped_01"}, tool_context=None
    )
    voice_ids = {b["voice_role"]: b["voice_id"] for b in result["narration_blocks"]}
    assert voice_ids == {"V1": "mapped_01", "V2": "V2"}


def test_language_is_forwarded_to_tts_and_whisperx() -> None:
    tts, whisperx, _, _ = _install_helpers()
    render_audio.__wrapped__(
        scenes=[_scene(1)], language="ru", tool_context=None
    )
    assert tts.calls[0][3] == "ru"
    assert whisperx.calls[0][2] == "ru"


def test_loudness_normalization_invoked_with_target_lufs() -> None:
    _, _, loudness, _ = _install_helpers()
    render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)
    assert loudness.calls[0][1] == TARGET_LUFS
    assert loudness.calls[0][1] == -23.0


def test_b2_upload_receives_each_wav_path() -> None:
    _, _, _, b2 = _install_helpers()
    scenes = [_scene(1), _scene(2)]
    result = render_audio.__wrapped__(scenes=scenes, tool_context=None)
    wav_paths = [b["wav_path"] for b in result["narration_blocks"]]
    assert b2.calls == wav_paths


def test_whisperx_alignment_carries_per_clip_word_counts() -> None:
    _install_helpers(whisperx=_FakeWhisperx(duration=3.0, word_count=5))
    result = render_audio.__wrapped__(
        scenes=[_scene(1), _scene(2)], tool_context=None
    )
    per_clip = result["whisperx_alignment"]["per_clip"]
    assert set(per_clip) == {"scene_001_V1", "scene_002_V1"}
    assert all(c["word_count"] == 5 for c in per_clip.values())


def test_narration_blocks_include_b2_url() -> None:
    _install_helpers()
    result = render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)
    block = result["narration_blocks"][0]
    assert block["b2_url"].startswith("https://b2.example.com/")
    assert block["b2_url"].endswith("scene_001_V1.wav")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_scenes_raises_value_error() -> None:
    _install_helpers()
    with pytest.raises(ValueError, match="non-empty scenes list"):
        render_audio.__wrapped__(scenes=[], tool_context=None)


def test_scene_without_voices_raises_value_error() -> None:
    _install_helpers()
    with pytest.raises(ValueError, match="no voices"):
        render_audio.__wrapped__(
            scenes=[{"id": 1, "voices": []}], tool_context=None
        )


def test_voice_with_empty_text_raises_value_error() -> None:
    _install_helpers()
    scenes = [{"id": 1, "voices": [{"voice_id": "V1", "text": "   "}]}]
    with pytest.raises(ValueError, match="empty text"):
        render_audio.__wrapped__(scenes=scenes, tool_context=None)


def test_voice_missing_id_raises_value_error() -> None:
    _install_helpers()
    scenes = [{"id": 1, "voices": [{"text": "hello"}]}]
    with pytest.raises(ValueError, match="voice_role / role / voice_id"):
        render_audio.__wrapped__(scenes=scenes, tool_context=None)


def test_scene_missing_id_raises_value_error() -> None:
    _install_helpers()
    scenes = [{"voices": [{"voice_id": "V1", "text": "hello"}]}]
    with pytest.raises(ValueError, match="missing id"):
        render_audio.__wrapped__(scenes=scenes, tool_context=None)


def test_scene_id_zero_is_accepted() -> None:
    _install_helpers()
    scenes = [{"id": 0, "voices": [{"voice_id": "V1", "text": "hello"}]}]
    result = render_audio.__wrapped__(scenes=scenes, tool_context=None)
    assert result["narration_blocks"][0]["scene_num"] == 0
    assert result["narration_blocks"][0]["block_id"] == "scene_000_V1"


def test_scene_id_non_convertible_raises_value_error() -> None:
    _install_helpers()
    scenes = [
        {"id": "not-a-number", "voices": [{"voice_id": "V1", "text": "hello"}]}
    ]
    with pytest.raises(ValueError, match="int-convertible"):
        render_audio.__wrapped__(scenes=scenes, tool_context=None)


# ---------------------------------------------------------------------------
# Helper return-value contract
# ---------------------------------------------------------------------------


def test_tts_empty_wav_path_raises_runtime_error() -> None:
    class _BadTts:
        def __call__(self, *args: Any, **kwargs: Any) -> str:
            return ""

    set_audio_helpers(
        tts_generate=_BadTts(),
        whisperx_align=_FakeWhisperx(),
        loudness_normalize=_FakeLoudness(),
        b2_upload=_FakeB2(),
    )
    with pytest.raises(RuntimeError, match="invalid wav_path"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_whisperx_non_dict_return_raises_runtime_error() -> None:
    class _BadAlign:
        def __call__(self, *args: Any, **kwargs: Any) -> list:
            return []

    set_audio_helpers(
        tts_generate=_FakeTts(),
        whisperx_align=_BadAlign(),
        loudness_normalize=_FakeLoudness(),
        b2_upload=_FakeB2(),
    )
    with pytest.raises(RuntimeError, match="non-dict"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_whisperx_zero_duration_raises_runtime_error() -> None:
    _install_helpers(whisperx=_FakeWhisperx(duration=0.0))
    with pytest.raises(RuntimeError, match="non-positive duration"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_b2_empty_url_raises_runtime_error() -> None:
    class _BadB2:
        def __call__(self, wav_path: str) -> str:
            return ""

    set_audio_helpers(
        tts_generate=_FakeTts(),
        whisperx_align=_FakeWhisperx(),
        loudness_normalize=_FakeLoudness(),
        b2_upload=_BadB2(),
    )
    with pytest.raises(RuntimeError, match="invalid url"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


# ---------------------------------------------------------------------------
# Fail-loud on helper exceptions
# ---------------------------------------------------------------------------


def test_tts_transient_failure_reraises() -> None:
    _install_helpers(tts=_FakeTts(fail_first_n=1))
    with pytest.raises(RuntimeError, match="tts: transient failure"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_tts_persistent_failure_writes_no_partial_state() -> None:
    tts = _FakeTts(fail_all=True)
    _install_helpers(tts=tts)
    agent = MagicMock()
    state = MagicMock()
    agent.state = state
    ctx = MagicMock()
    ctx.agent = agent
    with pytest.raises(RuntimeError, match="tts: persistent failure"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=ctx)
    state.set.assert_not_called()


def test_whisperx_failure_reraises_and_skips_b2() -> None:
    _, _, _, b2 = _install_helpers(whisperx=_FakeWhisperx(fail=True))
    with pytest.raises(RuntimeError, match="align failed"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)
    assert b2.calls == []


def test_loudness_failure_reraises_and_skips_whisperx() -> None:
    _, whisperx, _, _ = _install_helpers(loudness=_FakeLoudness(fail=True))
    with pytest.raises(RuntimeError, match="loudnorm: failure"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)
    assert whisperx.calls == []


def test_b2_failure_reraises() -> None:
    _install_helpers(b2=_FakeB2(fail=True))
    with pytest.raises(RuntimeError, match="b2: upload failed"):
        render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_is_persisted_on_success() -> None:
    _install_helpers()
    agent = MagicMock()
    state = MagicMock()
    agent.state = state
    ctx = MagicMock()
    ctx.agent = agent
    result = render_audio.__wrapped__(
        scenes=[_scene(1), _scene(2)], tool_context=ctx
    )
    set_keys = [call.args[0] for call in state.set.call_args_list]
    assert "whisperx_alignment" in set_keys
    assert "narration_blocks" in set_keys
    assert "_audio_stage_complete" in set_keys
    assert "_audio_needs_regeneration" in set_keys
    assert result["whisperx_alignment"]["language"] == "en"


def test_state_write_tolerates_missing_tool_context() -> None:
    _install_helpers()
    # Should not raise — tool_context=None disables state plumbing.
    render_audio.__wrapped__(scenes=[_scene(1)], tool_context=None)


def test_state_write_tolerates_agent_without_state() -> None:
    _install_helpers()
    ctx = MagicMock()
    ctx.agent = None
    # Should not raise even when no agent is attached.
    render_audio.__wrapped__(scenes=[_scene(1)], tool_context=ctx)


# ---------------------------------------------------------------------------
# Experiment factory
# ---------------------------------------------------------------------------


def test_audio_experiment_has_five_cases() -> None:
    cases = audio_cases()
    assert len(cases) == 5
    names = [c.metadata["case_name"] for c in cases]
    assert names == [
        "basic_3_scenes",
        "long_scene_45s",
        "multi_voice_blocks",
        "tts_transient_failure",
        "tts_persistent_failure",
    ]


def test_audio_experiment_has_three_evaluators() -> None:
    evaluators = audio_evaluators()
    assert len(evaluators) == 3
    evaluator_names = [type(e).__name__ for e in evaluators]
    assert evaluator_names == [
        "ContractComplianceEvaluator",
        "AudioInvariantEvaluator",
        "CritiqueStoreEvaluator",
    ]


def test_threshold_table_matches_evaluator_stack() -> None:
    evaluator_names = {type(e).__name__ for e in audio_evaluators()}
    assert set(AUDIO_EVALUATOR_THRESHOLDS) == evaluator_names


def test_hard_gates_require_perfect_score() -> None:
    assert AUDIO_EVALUATOR_THRESHOLDS["ContractComplianceEvaluator"] == (1.0, True)
    assert AUDIO_EVALUATOR_THRESHOLDS["AudioInvariantEvaluator"] == (1.0, True)


def test_critique_store_is_soft_gate_above_threshold() -> None:
    min_score, hard_gate = AUDIO_EVALUATOR_THRESHOLDS["CritiqueStoreEvaluator"]
    assert hard_gate is False
    assert min_score == pytest.approx(0.75)


def test_build_audio_experiment_wires_cases_and_evaluators() -> None:
    exp = build_audio_experiment()
    assert len(exp.cases) == 5
    assert len(exp.evaluators) == 3


def test_failure_cases_declare_runtime_error_label() -> None:
    failure_names = {"tts_transient_failure", "tts_persistent_failure"}
    for case in audio_cases():
        if case.metadata["case_name"] in failure_names:
            assert case.expected_output == {"label": "runtime_error"}


def test_success_cases_carry_expected_whisperx_alignment() -> None:
    success_names = {"basic_3_scenes", "long_scene_45s", "multi_voice_blocks"}
    for case in audio_cases():
        if case.metadata["case_name"] in success_names:
            assert "whisperx_alignment" in case.expected_output
            assert "narration_blocks" in case.expected_output
            assert case.expected_output["label"] == "success"


# ---------------------------------------------------------------------------
# _voice_role_of key priority
# ---------------------------------------------------------------------------


def test_voice_role_of_prefers_voice_role_over_voice_id() -> None:
    voice = {"voice_role": "V1", "voice_id": "qwen3-tts:male_01", "text": "hi"}
    assert _voice_role_of(voice) == "V1"


def test_voice_role_of_falls_back_to_role_before_voice_id() -> None:
    voice = {"role": "V2", "voice_id": "qwen3-tts:female_03"}
    assert _voice_role_of(voice) == "V2"


def test_voice_role_of_returns_voice_id_only_when_no_abstract_role() -> None:
    voice = {"voice_id": "qwen3-tts:male_01"}
    assert _voice_role_of(voice) == "qwen3-tts:male_01"


def test_voice_role_of_skips_blank_role_keys() -> None:
    voice = {"voice_role": "", "role": None, "voice_id": "qwen3-tts:male_01"}
    assert _voice_role_of(voice) == "qwen3-tts:male_01"


def test_voice_role_of_raises_when_no_keys_present() -> None:
    with pytest.raises(ValueError, match="voice_role / role / voice_id"):
        _voice_role_of({"text": "hi"})
