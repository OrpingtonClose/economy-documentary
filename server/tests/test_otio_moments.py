"""
Tests for per-moment OTIO validation helpers, the WhisperX duration
oracle, and B2 _meta.json sidecar round-trip.

Covers issues:
    #70 -- Artifact paper trail (sidecar round-trip)
    #82 -- WhisperX silent-degradation removal (fail-loud)
    #84 -- OTIO compliance at ALL moments (per-moment validators)
    #85 -- Extension clip escalation (video < audio -> supervisor_escalate)
    #86 -- WhisperX as duration oracle (projection math vs PAG fixture)

The PAG reference fixture below is the production run that motivated
these changes: the scenario LLM claimed 270 s of narration across 7
scenes, but WhisperX measured 194 s -- a 72 % ratio.  The pipeline
should have caught this around scene 3 (running projection already
below 80 %) and escalated BEFORE burning 2.5 h of GPU on under-length
video.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make server/ imports work when running `pytest` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from tools.otio_moments import (  # noqa: E402
    LTX_CAP_SEC,
    PROJECTION_ALARM_RATIO,
    MeasuredClip,
    WhisperXOracle,
    fire_reflection_event,
    measure_actual_duration_with_whisperx,
    persist_scene_assembly_artifact,
    validate_audio_duration_vs_scene_target,
    validate_scene_assembly,
    validate_video_duration_vs_audio,
)


# ---------------------------------------------------------------------------
# PAG-run reference fixture (#86)
# ---------------------------------------------------------------------------

PAG_RUN = [
    # scene_num, claimed, actual, ratio
    (1, 35.0, 19.8, 0.56),
    (2, 40.0, 29.8, 0.74),
    (3, 38.0, 27.7, 0.73),
    (4, 42.0, 26.6, 0.63),
    (5, 37.0, 31.8, 0.86),
    (6, 45.0, 28.5, 0.63),
    (7, 33.0, 30.0, 0.91),
]
# The PAG-run totals quoted in the issue are rounded to whole seconds;
# the per-scene column sums to 194.2s, which rounds to the stated 194s.
PAG_TOTAL_CLAIMED = 270.0
PAG_TOTAL_ACTUAL = 194.2


@pytest.fixture
def pag_scenes():
    """Return a scenes list mirroring the PAG run's claimed durations."""
    return [
        {
            "scene_num": sn,
            "duration_sec": claimed,
            "voices": [{"voice": "V1", "text": "hello world"}],
        }
        for (sn, claimed, _actual, _ratio) in PAG_RUN
    ]


# ---------------------------------------------------------------------------
# validate_audio_duration_vs_scene_target
# ---------------------------------------------------------------------------

class TestValidateAudioDuration:
    """Per-moment audio clip validator (#84)."""

    def _scene(self, duration_sec: float, num_voices: int = 1) -> dict:
        return {
            "scene_num": 1,
            "duration_sec": duration_sec,
            "voices": [
                {"voice": f"V{i+1}", "text": f"text {i}"} for i in range(num_voices)
            ],
        }

    def test_within_tolerance_passes(self):
        # Scene target 30s with 1 voice -> per-voice budget 30s.
        # Actual 30.3s is within 1.5s tolerance.
        err = validate_audio_duration_vs_scene_target(
            scene_num=1, voice="V1",
            actual_duration_sec=30.3,
            scene=self._scene(30.0, num_voices=1),
        )
        assert err is None

    def test_exceeds_tolerance_fails(self):
        # 30s target, 3 voices -> per-voice budget 10s.
        # Actual 13.84s mirrors the parent-run Scene 5 phrase 2 failure
        # (narration 13.84s vs video capped at 10s).
        err = validate_audio_duration_vs_scene_target(
            scene_num=5, voice="V2",
            actual_duration_sec=13.84,
            scene=self._scene(30.0, num_voices=3),
        )
        assert err is not None
        assert "13.84" in err and "10.00" in err
        assert "scene 5" in err and "V2" in err

    def test_zero_duration_fails(self):
        err = validate_audio_duration_vs_scene_target(
            scene_num=1, voice="V1",
            actual_duration_sec=0.0,
            scene=self._scene(30.0),
        )
        assert err is not None
        assert "must be > 0" in err

    def test_missing_scene_target_fails(self):
        err = validate_audio_duration_vs_scene_target(
            scene_num=1, voice="V1",
            actual_duration_sec=5.0,
            scene={"scene_num": 1, "duration_sec": 0, "voices": [{"voice": "V1", "text": "x"}]},
        )
        assert err is not None
        assert "no duration_sec target" in err

    def test_ignores_empty_voices(self):
        # Voices with empty text don't count towards the denominator.
        # Scene target 30s, 3 voices where only 2 have text -> per-voice = 15s.
        scene = {
            "scene_num": 1,
            "duration_sec": 30.0,
            "voices": [
                {"voice": "V1", "text": "speaks"},
                {"voice": "V2", "text": ""},     # empty -> excluded
                {"voice": "V3", "text": "also"},
            ],
        }
        err = validate_audio_duration_vs_scene_target(
            scene_num=1, voice="V1",
            actual_duration_sec=15.5,  # exactly per-voice budget + slack
            scene=scene,
        )
        assert err is None


# ---------------------------------------------------------------------------
# validate_video_duration_vs_audio  (#85)
# ---------------------------------------------------------------------------

class TestValidateVideoVsAudio:
    """Per-moment video-vs-narration check (#85)."""

    def test_video_longer_than_audio_passes(self):
        err = validate_video_duration_vs_audio(
            scene_num=1, phrase_idx=0,
            video_duration_sec=10.0,
            audio_duration_sec=7.5,
        )
        assert err is None

    def test_video_equal_audio_passes(self):
        err = validate_video_duration_vs_audio(
            scene_num=1, phrase_idx=0,
            video_duration_sec=9.5,
            audio_duration_sec=9.5,
        )
        assert err is None

    def test_video_short_within_frame_slop_passes(self):
        # 0.2s ffmpeg frame-boundary slop -> within 0.25s tolerance.
        err = validate_video_duration_vs_audio(
            scene_num=1, phrase_idx=0,
            video_duration_sec=9.8,
            audio_duration_sec=10.0,
        )
        assert err is None

    def test_video_shorter_than_audio_fails(self):
        # Parent-run failure: video 10.0s (LTX cap) vs audio 13.84s.
        err = validate_video_duration_vs_audio(
            scene_num=5, phrase_idx=2,
            video_duration_sec=LTX_CAP_SEC,
            audio_duration_sec=13.84,
        )
        assert err is not None
        assert "scene 5" in err and "phrase 2" in err
        assert "10.00" in err and "13.84" in err
        assert "shortfall" in err

    def test_zero_video_fails(self):
        err = validate_video_duration_vs_audio(
            scene_num=1, phrase_idx=0,
            video_duration_sec=0.0,
            audio_duration_sec=5.0,
        )
        assert err is not None
        assert "must be > 0" in err


# ---------------------------------------------------------------------------
# WhisperXOracle projection math  (#86)
# ---------------------------------------------------------------------------

class TestWhisperXOracle:
    """Projection math vs the PAG reference run."""

    def test_empty_oracle_projects_all_targets(self, pag_scenes):
        oracle = WhisperXOracle(target_total_sec=PAG_TOTAL_CLAIMED)
        oracle.register_scenes(pag_scenes)
        assert oracle.measured_total() == pytest.approx(0.0)
        # With nothing measured, projected = sum of targets = 270s.
        assert oracle.project_total() == pytest.approx(PAG_TOTAL_CLAIMED)
        assert oracle.check_projection() is None  # 100% of target

    def test_pag_run_ratio_is_72_percent(self):
        """Sanity: PAG_TOTAL_ACTUAL/PAG_TOTAL_CLAIMED is 72% as stated."""
        assert sum(row[1] for row in PAG_RUN) == pytest.approx(PAG_TOTAL_CLAIMED)
        assert sum(row[2] for row in PAG_RUN) == pytest.approx(PAG_TOTAL_ACTUAL)
        ratio = PAG_TOTAL_ACTUAL / PAG_TOTAL_CLAIMED
        assert 0.71 < ratio < 0.73

    def test_projection_falls_as_measurements_arrive(self, pag_scenes):
        """After scene 3 the projection is already below 80%; oracle must alarm."""
        oracle = WhisperXOracle(target_total_sec=PAG_TOTAL_CLAIMED)
        oracle.register_scenes(pag_scenes)

        # Record scenes 1..3 with the PAG-measured durations.
        for sn, _claimed, actual, _ratio in PAG_RUN[:3]:
            oracle.record(
                scene_num=sn, voice="V1",
                claimed_sec=next(r[1] for r in PAG_RUN if r[0] == sn),
                measured_sec=actual,
            )

        # measured so far = 19.8 + 29.8 + 27.7 = 77.3
        # remaining targets = 42 + 37 + 45 + 33 = 157
        # projected = 77.3 + 157 = 234.3  /  target 270  = 86.8%
        # -- above 80%, so NO alarm yet at scene 3.
        projected_at_3 = 19.8 + 29.8 + 27.7 + 42 + 37 + 45 + 33
        assert oracle.project_total() == pytest.approx(projected_at_3)
        assert oracle.check_projection() is None

        # Now add scenes 4 and 6 (the two worst ratios) to push below 80%.
        for sn, _claimed, actual, _ratio in PAG_RUN:
            if sn in (4, 6) and sn not in oracle.completed_scene_nums():
                oracle.record(
                    scene_num=sn, voice="V1",
                    claimed_sec=next(r[1] for r in PAG_RUN if r[0] == sn),
                    measured_sec=actual,
                )

        # measured: 1,2,3,4,6 = 19.8+29.8+27.7+26.6+28.5 = 132.4
        # remaining: 5,7 = 37+33 = 70
        # projected = 202.4 / 270 = 74.96% -- BELOW 80%, alarm fires.
        assert oracle.project_total() == pytest.approx(132.4 + 70.0)
        alarm = oracle.check_projection()
        assert alarm is not None
        assert "projected total" in alarm
        assert "target" in alarm

    def test_projection_with_all_pag_data_is_final_total(self, pag_scenes):
        oracle = WhisperXOracle(target_total_sec=PAG_TOTAL_CLAIMED)
        oracle.register_scenes(pag_scenes)
        for sn, claimed, actual, _ratio in PAG_RUN:
            oracle.record(scene_num=sn, voice="V1",
                          claimed_sec=claimed, measured_sec=actual)
        # Nothing remaining -- projected equals measured total.
        assert oracle.project_total() == pytest.approx(PAG_TOTAL_ACTUAL)
        alarm = oracle.check_projection()
        assert alarm is not None
        assert "194" in alarm or "193" in alarm  # formatted int

    def test_projection_alarm_ratio_boundary(self, pag_scenes):
        oracle = WhisperXOracle(target_total_sec=100.0)
        # Only one scene, measured = 80, ratio = 80% -> NO alarm (>= threshold).
        oracle.register_scenes([{"scene_num": 1, "duration_sec": 100.0,
                                 "voices": [{"voice": "V1", "text": "x"}]}])
        oracle.record(scene_num=1, voice="V1", claimed_sec=100.0, measured_sec=80.0)
        assert oracle.project_total() == pytest.approx(80.0)
        assert oracle.check_projection() is None

        # Same scene, measured = 79 -> ratio 79% -> alarm.
        oracle2 = WhisperXOracle(target_total_sec=100.0)
        oracle2.register_scenes([{"scene_num": 1, "duration_sec": 100.0,
                                  "voices": [{"voice": "V1", "text": "x"}]}])
        oracle2.record(scene_num=1, voice="V1", claimed_sec=100.0, measured_sec=79.0)
        assert oracle2.check_projection() is not None

    def test_measured_clip_ratio(self):
        clip = MeasuredClip(
            scene_num=1, voice="V1",
            claimed_sec=35.0, measured_sec=19.8,
        )
        assert clip.ratio == pytest.approx(19.8 / 35.0)

    def test_projection_ratio_is_exposed_as_constant(self):
        assert PROJECTION_ALARM_RATIO == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Fire reflection event -- escalation helpers (#85, #86)
# ---------------------------------------------------------------------------

class TestFireReflectionEvent:
    def test_records_event_in_state(self):
        state: dict = {}
        # supervisor_escalate import path is expected to fail (W3 pending).
        fire_reflection_event(state=state,
                              context="projected total 194s vs target 420s = 46%")
        events = state.get("_reflection_events")
        assert isinstance(events, list) and len(events) == 1
        assert events[0]["context"] == "projected total 194s vs target 420s = 46%"
        assert events[0]["kind"] == "projection"

    def test_calls_supervisor_when_available(self):
        """When agents.production_supervisor.supervisor_escalate exists, it's invoked."""
        state: dict = {}
        mock_escalate = MagicMock()
        # Inject a fake module.
        fake_module = MagicMock()
        fake_module.supervisor_escalate = mock_escalate
        with patch.dict(sys.modules, {"agents.production_supervisor": fake_module}):
            fire_reflection_event(state=state, context="projection too low")
        assert mock_escalate.called
        call = mock_escalate.call_args
        assert call.kwargs.get("kind") == "projection_shortfall"
        assert "projection too low" in call.kwargs.get("context", "")
        assert "add_scenes" in call.kwargs.get("actions", [])

    def test_supervisor_unavailable_does_not_raise(self):
        # Simulates W3 not merged yet.
        state: dict = {}
        fire_reflection_event(state=state, context="any context")
        # Event still recorded in state even though supervisor missing.
        assert len(state["_reflection_events"]) == 1


# ---------------------------------------------------------------------------
# WhisperX fail-loud (#82) via measure_actual_duration_with_whisperx
# ---------------------------------------------------------------------------

class TestWhisperXFailLoud:
    def test_returns_duration_from_valid_alignment(self, tmp_path):
        wav = tmp_path / "clip.wav"
        wav.write_bytes(b"\x00")
        fake_result = json.dumps({
            "status": "aligned",
            "words": [{"word": "hi", "start": 0.0, "end": 0.3}],
            "total_duration": 0.3,
        })
        with patch("tools.whisperx_tools.align_narration",
                   return_value=fake_result):
            dur = measure_actual_duration_with_whisperx(
                wav_path=str(wav), text="hi")
        assert dur == pytest.approx(0.3)

    def test_raises_when_status_is_not_aligned(self, tmp_path):
        wav = tmp_path / "clip.wav"
        wav.write_bytes(b"\x00")
        fake_result = json.dumps({"status": "failed", "error": "cuda oom"})
        with patch("tools.whisperx_tools.align_narration",
                   return_value=fake_result), pytest.raises(RuntimeError, match="cuda oom"):
            measure_actual_duration_with_whisperx(wav_path=str(wav), text="hi")

    def test_raises_when_zero_words_aligned(self, tmp_path):
        wav = tmp_path / "clip.wav"
        wav.write_bytes(b"\x00")
        fake_result = json.dumps({
            "status": "aligned",
            "words": [],
            "total_duration": 0.0,
        })
        with patch("tools.whisperx_tools.align_narration",
                   return_value=fake_result), pytest.raises(RuntimeError, match="0 words"):
            measure_actual_duration_with_whisperx(wav_path=str(wav), text="hi")


# ---------------------------------------------------------------------------
# Scene-level assembly validator (#84)
# ---------------------------------------------------------------------------

def _make_clip(scene_num: int, voice: str, duration: float, phrase_idx: int = 0):
    import opentimelineio as otio
    media = otio.schema.ExternalReference(
        target_url="file:///tmp/stub.wav" if voice else "file:///tmp/stub.mp4",
        available_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 30),
            duration=otio.opentime.RationalTime(duration * 30, 30),
        ),
    )
    clip = otio.schema.Clip(
        name=f"scene_{scene_num:03d}_{voice}",
        media_reference=media,
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 30),
            duration=otio.opentime.RationalTime(duration * 30, 30),
        ),
    )
    clip.metadata["documentary"] = {
        "scene_num": scene_num,
        "voice": voice,
        "phrase_idx": phrase_idx,
    }
    return clip


def _make_gap(scene_num: int, duration: float, status: str = "", gap_type: str = ""):
    import opentimelineio as otio
    gap = otio.schema.Gap(
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 30),
            duration=otio.opentime.RationalTime(duration * 30, 30),
        ),
    )
    gap.metadata["documentary"] = {
        "scene_num": scene_num,
        "status": status,
        "gap_type": gap_type,
    }
    return gap


def _build_scene_timeline(scene_num: int, narr_dur: float, video_dur: float):
    import opentimelineio as otio
    tl = otio.schema.Timeline(name="t")
    v = otio.schema.Track(name="V1_Video", kind="Video")
    v.append(_make_clip(scene_num, "", video_dur))
    tl.tracks.append(v)
    a = otio.schema.Track(name="A1_Narration", kind="Audio")
    a.append(_make_clip(scene_num, "V1", narr_dur))
    tl.tracks.append(a)
    return tl


class TestSceneAssembly:
    def test_pass_when_video_covers_audio(self):
        tl = _build_scene_timeline(scene_num=1, narr_dur=9.0, video_dur=10.0)
        assert validate_scene_assembly(tl, scene_num=1) is None

    def test_fail_when_video_shorter_than_audio(self):
        tl = _build_scene_timeline(scene_num=1, narr_dur=13.84, video_dur=10.0)
        err = validate_scene_assembly(tl, scene_num=1)
        assert err is not None
        assert "shortfall" in err

    def test_fail_when_empty_placeholder_gap_remains(self):
        import opentimelineio as otio
        tl = otio.schema.Timeline(name="t")
        v = otio.schema.Track(name="V1_Video", kind="Video")
        v.append(_make_gap(scene_num=1, duration=5.0, status="empty"))
        tl.tracks.append(v)
        a = otio.schema.Track(name="A1_Narration", kind="Audio")
        a.append(_make_clip(1, "V1", 5.0))
        tl.tracks.append(a)
        err = validate_scene_assembly(tl, scene_num=1)
        assert err is not None
        assert "placeholder gap" in err

    def test_fail_when_no_video_clips(self):
        import opentimelineio as otio
        tl = otio.schema.Timeline(name="t")
        tl.tracks.append(otio.schema.Track(name="V1_Video", kind="Video"))
        a = otio.schema.Track(name="A1_Narration", kind="Audio")
        a.append(_make_clip(1, "V1", 5.0))
        tl.tracks.append(a)
        err = validate_scene_assembly(tl, scene_num=1)
        assert err is not None
        assert "no video clips" in err

    def test_ignores_structural_inter_voice_gaps(self):
        import opentimelineio as otio
        tl = otio.schema.Timeline(name="t")
        v = otio.schema.Track(name="V1_Video", kind="Video")
        v.append(_make_clip(1, "", 10.0))
        v.append(_make_gap(1, 0.3, status="", gap_type="inter_voice"))
        tl.tracks.append(v)
        a = otio.schema.Track(name="A1_Narration", kind="Audio")
        a.append(_make_clip(1, "V1", 9.0))
        tl.tracks.append(a)
        assert validate_scene_assembly(tl, scene_num=1) is None

    def test_persist_scene_assembly_artifact_writes_otio(self, tmp_path):
        tl = _build_scene_timeline(scene_num=3, narr_dur=5.0, video_dur=5.0)
        out = persist_scene_assembly_artifact(tl, scene_num=3, out_dir=str(tmp_path))
        assert os.path.exists(out)
        assert out.endswith("scene_003_assembly.otio")


# ---------------------------------------------------------------------------
# Multi-voice scene completeness (regression for PR #115 review finding)
# ---------------------------------------------------------------------------

def _multi_voice_scene_timeline(scene_num: int, num_voices: int,
                                video_phrases: int, narr_dur: float = 10.0,
                                video_dur: float = 10.0):
    """Build a scene with ``num_voices`` narration clips and ``video_phrases``
    video clips.  The initial placeholder gap is dropped once ``video_phrases
    >= 1`` (mirroring the real add_video_clip behaviour on phrase_idx=0)."""
    import opentimelineio as otio
    tl = otio.schema.Timeline(name="t")
    v = otio.schema.Track(name="V1_Video", kind="Video")
    if video_phrases == 0:
        v.append(_make_gap(scene_num, 0.0, status="empty"))
    else:
        for p in range(video_phrases):
            clip = _make_clip(scene_num, "", video_dur, phrase_idx=p)
            v.append(clip)
    tl.tracks.append(v)
    a = otio.schema.Track(name="A1_Narration", kind="Audio")
    for i in range(num_voices):
        a.append(_make_clip(scene_num, f"V{i+1}", narr_dur, phrase_idx=i))
    tl.tracks.append(a)
    return tl


class TestSceneCompleteness:
    """Regression for PR #115 review: multi-voice scenes must not fire
    the assembly check after only phrase_idx=0 is persisted."""

    def test_multi_voice_incomplete_after_phrase_0(self):
        """3-voice scene with only phrase 0 video -> NOT complete."""
        from tools.otio_tools import _scene_is_video_complete
        tl = _multi_voice_scene_timeline(scene_num=5, num_voices=3, video_phrases=1)
        assert _scene_is_video_complete(tl, scene_num=5) is False

    def test_multi_voice_incomplete_after_phrase_1(self):
        """3-voice scene with phrases 0,1 but not 2 -> NOT complete."""
        from tools.otio_tools import _scene_is_video_complete
        tl = _multi_voice_scene_timeline(scene_num=5, num_voices=3, video_phrases=2)
        assert _scene_is_video_complete(tl, scene_num=5) is False

    def test_multi_voice_complete_after_all_phrases(self):
        """3-voice scene with all 3 phrases -> complete."""
        from tools.otio_tools import _scene_is_video_complete
        tl = _multi_voice_scene_timeline(scene_num=5, num_voices=3, video_phrases=3)
        assert _scene_is_video_complete(tl, scene_num=5) is True

    def test_single_voice_complete_after_phrase_0(self):
        from tools.otio_tools import _scene_is_video_complete
        tl = _multi_voice_scene_timeline(scene_num=1, num_voices=1, video_phrases=1)
        assert _scene_is_video_complete(tl, scene_num=1) is True

    def test_placeholder_gap_blocks_completeness(self):
        """If the placeholder gap is still present, scene is not complete
        regardless of clip counts."""
        import opentimelineio as otio
        from tools.otio_tools import _scene_is_video_complete
        tl = otio.schema.Timeline(name="t")
        v = otio.schema.Track(name="V1_Video", kind="Video")
        v.append(_make_gap(1, 0.0, status="empty"))
        tl.tracks.append(v)
        a = otio.schema.Track(name="A1_Narration", kind="Audio")
        a.append(_make_clip(1, "V1", 5.0))
        tl.tracks.append(a)
        assert _scene_is_video_complete(tl, scene_num=1) is False

    def test_en_alternate_narration_ignored(self):
        """V1_EN alternate-language clips don't count against the required
        video-clip count (they share the primary's video phrase)."""
        import opentimelineio as otio
        from tools.otio_tools import _scene_is_video_complete
        tl = otio.schema.Timeline(name="t")
        v = otio.schema.Track(name="V1_Video", kind="Video")
        v.append(_make_clip(1, "", 10.0, phrase_idx=0))
        tl.tracks.append(v)
        a = otio.schema.Track(name="A1_Narration", kind="Audio")
        a.append(_make_clip(1, "V1", 10.0, phrase_idx=0))
        a.append(_make_clip(1, "V1_EN", 10.0, phrase_idx=0))  # alternate lang
        tl.tracks.append(a)
        # Only one primary-language clip, one video clip -> complete.
        assert _scene_is_video_complete(tl, scene_num=1) is True

    def test_find_scene_from_state_accepts_list(self):
        """Regression for PR #115 review: _find_scene_from_state must accept
        a native list on state['scenes'], not only a JSON string."""
        from tools.otio_tools import _find_scene_from_state
        scenes_list = [
            {"scene_num": 1, "duration_sec": 30.0},
            {"scene_num": 5, "duration_sec": 45.0},
        ]
        # Native list -- must NOT fall through str(raw) which would produce
        # invalid JSON (single-quoted repr).
        found = _find_scene_from_state({"scenes": scenes_list}, scene_num=5)
        assert found is not None
        assert found["duration_sec"] == 45.0

    def test_find_scene_from_state_accepts_json_string(self):
        from tools.otio_tools import _find_scene_from_state
        state = {"scenes": '[{"scene_num": 2, "duration_sec": 22.0}]'}
        found = _find_scene_from_state(state, scene_num=2)
        assert found is not None
        assert found["duration_sec"] == 22.0

    def test_find_scene_from_state_returns_none_for_garbage(self):
        from tools.otio_tools import _find_scene_from_state
        assert _find_scene_from_state({"scenes": "not-json"}, scene_num=1) is None
        assert _find_scene_from_state({}, scene_num=1) is None

    def test_chained_callback_runs_guardian_even_if_oracle_raises(self):
        """Regression for PR #115 review: if whisperx_oracle_callback raises,
        the chained wrapper must still invoke timeline_guardian_callback so
        OTIO violations are caught."""
        from unittest.mock import patch

        # We exercise _chained_after_agent_callback directly; the real
        # CallbackContext isn't needed because we patch both inner calls.
        from agents import audio_agent as audio_agent_mod

        fake_ctx = object()
        guardian_calls = []

        def boom(_ctx):
            raise RuntimeError("scenes malformed")

        def guardian(_ctx):
            guardian_calls.append(1)
            return None

        with patch.object(audio_agent_mod, "whisperx_oracle_callback", boom), \
             patch.object(audio_agent_mod, "timeline_guardian_callback", guardian):
            # Must NOT raise and MUST call the guardian.
            result = audio_agent_mod._chained_after_agent_callback(fake_ctx)
        assert result is None
        assert guardian_calls == [1]

    def test_extension_sub_clips_do_not_inflate_count(self):
        """Extension clips (sub_idx set) decorate phrases, not new phrases."""
        import opentimelineio as otio
        from tools.otio_tools import _scene_is_video_complete
        tl = otio.schema.Timeline(name="t")
        v = otio.schema.Track(name="V1_Video", kind="Video")
        primary = _make_clip(1, "", 10.0, phrase_idx=0)
        v.append(primary)
        ext = _make_clip(1, "", 3.0, phrase_idx=0)
        ext.metadata["documentary"]["sub_idx"] = 0
        v.append(ext)
        tl.tracks.append(v)
        a = otio.schema.Track(name="A1_Narration", kind="Audio")
        a.append(_make_clip(1, "V1", 10.0, phrase_idx=0))
        a.append(_make_clip(1, "V2", 10.0, phrase_idx=1))
        tl.tracks.append(a)
        # 2 narration, 1 real video (ext ignored) -> NOT complete.
        assert _scene_is_video_complete(tl, scene_num=1) is False


# ---------------------------------------------------------------------------
# B2 _meta.json sidecar round-trip (#70)
# ---------------------------------------------------------------------------

class _FakeB2Bucket:
    """Minimal B2 bucket double that captures uploads in-memory."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_local_file(self, local_file: str, file_name: str) -> None:
        with open(local_file, "rb") as fh:
            self.objects[file_name] = fh.read()

    def upload(self, source, key: str) -> None:
        # source is UploadSourceBytes; the real API exposes .read_bytes
        # but for the in-memory double we pull from source's internal attr.
        data = getattr(source, "data_bytes", None) or getattr(source, "_bytes", None)
        if data is None:
            # Fallback: introspect via get_content_bytes() when b2sdk is real.
            data = source.get_content_bytes() if hasattr(source, "get_content_bytes") else b""
        self.objects[key] = data


@pytest.fixture
def fake_b2(monkeypatch):
    """Patch the b2_checkpoint module to use an in-memory bucket."""
    from tools import b2_checkpoint

    bucket = _FakeB2Bucket()
    monkeypatch.setattr(b2_checkpoint, "_get_bucket", lambda: bucket)
    monkeypatch.setattr(b2_checkpoint, "_run_id", "test_run_123")
    monkeypatch.setenv("B2_RUN_ID", "test_run_123")
    return bucket


class TestSidecarRoundTrip:
    def test_upload_with_sidecar_writes_both(self, fake_b2, tmp_path):
        from tools import b2_checkpoint

        wav = tmp_path / "scene_001_V1.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 1024)

        meta = {
            "creator_agent": "audio_agent",
            "prompt_used": "narrator voice: documentary style",
            "qa_results_so_far": [{"check": "file_exists", "status": "pass"}],
            "validation_outcomes": [{"phase": "audio", "pass": True}],
            "parent_artifact_refs": ["state/scenes.json"],
        }

        ok = b2_checkpoint.upload_with_sidecar(
            local_path=str(wav),
            b2_relative_path="audio/scene_001_V1.wav",
            meta=meta,
        )
        assert ok is True

        primary_key = "test_run_123/audio/scene_001_V1.wav"
        sidecar_key = "test_run_123/audio/scene_001_V1.wav._meta.json"
        assert primary_key in fake_b2.objects
        assert sidecar_key in fake_b2.objects

        sidecar = json.loads(fake_b2.objects[sidecar_key].decode("utf-8"))
        # Caller-provided fields preserved.
        assert sidecar["creator_agent"] == "audio_agent"
        assert sidecar["prompt_used"].startswith("narrator voice")
        assert sidecar["parent_artifact_refs"] == ["state/scenes.json"]
        # Provenance fields auto-populated.
        assert sidecar["primary_b2_key"] == primary_key
        assert sidecar["run_id"] == "test_run_123"
        assert sidecar["primary_size_bytes"] > 0
        # All required keys present.
        for k in b2_checkpoint.SIDECAR_REQUIRED_KEYS:
            assert k in sidecar

    def test_upload_file_without_meta_skips_sidecar(self, fake_b2, tmp_path):
        from tools import b2_checkpoint

        mp4 = tmp_path / "clip.mp4"
        mp4.write_bytes(b"ftyp" + b"\x00" * 4)
        ok = b2_checkpoint.upload_file(str(mp4), "video/clip.mp4")
        assert ok is True
        assert "test_run_123/video/clip.mp4" in fake_b2.objects
        assert "test_run_123/video/clip.mp4._meta.json" not in fake_b2.objects

    def test_upload_tts_clip_with_meta_round_trip(self, fake_b2, tmp_path):
        from tools import b2_checkpoint

        wav = tmp_path / "scene_002_V2.wav"
        wav.write_bytes(b"\x00" * 2048)
        b2_checkpoint.upload_tts_clip(
            wav_path=str(wav),
            meta={
                "creator_agent": "audio_agent",
                "prompt_used": "voice 2 narration",
                "parent_artifact_refs": ["state/scenes.json"],
            },
        )
        assert "test_run_123/audio/scene_002_V2.wav" in fake_b2.objects
        sidecar_key = "test_run_123/audio/scene_002_V2.wav._meta.json"
        assert sidecar_key in fake_b2.objects
        sidecar = json.loads(fake_b2.objects[sidecar_key].decode("utf-8"))
        assert sidecar["creator_agent"] == "audio_agent"

    def test_upload_video_clip_with_meta_round_trip(self, fake_b2, tmp_path):
        from tools import b2_checkpoint

        mp4 = tmp_path / "scene_003_phrase_001.mp4"
        mp4.write_bytes(b"ftyp" + b"\x00" * 1024)
        b2_checkpoint.upload_video_clip(
            mp4_path=str(mp4),
            meta={
                "creator_agent": "production_supervisor",
                "prompt_used": "cinematic documentary b-roll",
                "validation_outcomes": [
                    {"phase": "production", "pass": True, "detail": "video >= audio"},
                ],
                "parent_artifact_refs": [
                    "state/visual_concepts.json",
                    "audio/scene_003_V1.wav",
                ],
            },
        )
        sidecar_key = "test_run_123/video/scene_003_phrase_001.mp4._meta.json"
        assert sidecar_key in fake_b2.objects
        sidecar = json.loads(fake_b2.objects[sidecar_key].decode("utf-8"))
        assert sidecar["creator_agent"] == "production_supervisor"
        assert len(sidecar["parent_artifact_refs"]) == 2
        assert sidecar["validation_outcomes"][0]["pass"] is True

    def test_sidecar_fills_defaults_when_meta_is_sparse(self, fake_b2, tmp_path):
        from tools import b2_checkpoint

        wav = tmp_path / "x.wav"
        wav.write_bytes(b"\x00")
        ok = b2_checkpoint.upload_with_sidecar(
            str(wav),
            "audio/x.wav",
            meta={"creator_agent": "audio_agent"},  # bare minimum
        )
        assert ok is True
        sidecar_key = "test_run_123/audio/x.wav._meta.json"
        sidecar = json.loads(fake_b2.objects[sidecar_key].decode("utf-8"))
        # Defaulted fields present and empty-but-typed (not missing).
        assert sidecar["qa_results_so_far"] == []
        assert sidecar["validation_outcomes"] == []
        assert sidecar["parent_artifact_refs"] == []
        assert sidecar["prompt_used"] == ""
        # Caller field preserved.
        assert sidecar["creator_agent"] == "audio_agent"
