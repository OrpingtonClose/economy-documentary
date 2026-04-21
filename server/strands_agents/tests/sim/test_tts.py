"""Direct-proof tests for :class:`FakeTTS`."""

from __future__ import annotations

import os
import wave

import pytest

from strands_agents.sim.recorder import Recorder
from strands_agents.sim.tts import FakeTTS


class TestFakeTTSGenerate:
    def test_writes_valid_wav(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        path = tts.tts_generate(
            scene_num=1,
            voice_role="V1",
            text="hello world foo bar",
            language="en",
        )
        assert os.path.exists(path)
        with wave.open(path, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getnframes() > 0

    def test_duration_follows_word_rate_by_default(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        # 5 words / 2.5 words-per-second → 2.0s nominal
        path = tts.tts_generate(
            scene_num=1, voice_role="V1", text="a b c d e", language="en"
        )
        with wave.open(path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        assert duration == pytest.approx(2.0, abs=0.02)

    def test_duration_floor_keeps_short_blocks_audible(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        path = tts.tts_generate(
            scene_num=1, voice_role="V1", text="hi", language="en"
        )
        with wave.open(path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        # WhisperX breaks on zero-duration clips; fake enforces 0.5s floor
        # so the alignment layer never hits that edge case.
        assert duration >= 0.5

    def test_override_forces_duration(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        tts.set_next_duration(
            scene_num=3, voice_role="V1", language="en", duration=9.9
        )
        path = tts.tts_generate(
            scene_num=3, voice_role="V1", text="three words here", language="en"
        )
        with wave.open(path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        assert duration == pytest.approx(9.9, abs=0.02)

    def test_override_consumed_once(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        tts.set_next_duration(
            scene_num=2, voice_role="V1", language="en", duration=5.0
        )
        first = tts.tts_generate(
            scene_num=2, voice_role="V1", text="a b c d e", language="en"
        )
        second = tts.tts_generate(
            scene_num=2, voice_role="V1", text="a b c d e", language="en"
        )
        with wave.open(first, "rb") as wf:
            d_first = wf.getnframes() / wf.getframerate()
        with wave.open(second, "rb") as wf:
            d_second = wf.getnframes() / wf.getframerate()
        # First call: override fires (5.0). Second call: word-rate (2.0).
        assert d_first == pytest.approx(5.0, abs=0.02)
        assert d_second == pytest.approx(2.0, abs=0.02)

    def test_override_rejects_non_positive(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        with pytest.raises(ValueError, match="positive"):
            tts.set_next_duration(
                scene_num=1, voice_role="V1", language="en", duration=0.0
            )


class TestFakeTTSAlign:
    def test_alignment_shape_matches_whisperx(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        path = tts.tts_generate(
            scene_num=1,
            voice_role="V1",
            text="inflation eats savings slowly",
            language="en",
        )
        seg = tts.whisperx_align(path, "inflation eats savings slowly", "en")
        assert set(seg) >= {"total_duration", "word_count", "words"}
        assert seg["word_count"] == 4
        assert len(seg["words"]) == 4
        for w in seg["words"]:
            assert set(w) == {"word", "start", "end"}
            assert w["end"] >= w["start"]

    def test_timings_span_the_whole_clip(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        path = tts.tts_generate(
            scene_num=1, voice_role="V1", text="one two three", language="en"
        )
        seg = tts.whisperx_align(path, "one two three", "en")
        assert seg["words"][0]["start"] == pytest.approx(0.0, abs=0.001)
        assert seg["words"][-1]["end"] == pytest.approx(
            seg["total_duration"], abs=0.001
        )

    def test_empty_text_gives_empty_words(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))
        path = tts.tts_generate(
            scene_num=1, voice_role="V1", text="", language="en"
        )
        seg = tts.whisperx_align(path, "", "en")
        assert seg["word_count"] == 0
        assert seg["words"] == []

    def test_post_align_hook_can_mutate(self, tmp_path) -> None:
        tts = FakeTTS(tmpdir=str(tmp_path))

        def shorten_first_word(seg: dict) -> None:
            if seg["words"]:
                seg["words"][0]["end"] = seg["words"][0]["start"] + 0.01

        tts.add_post_align_hook(shorten_first_word)
        path = tts.tts_generate(
            scene_num=1, voice_role="V1", text="a b c", language="en"
        )
        seg = tts.whisperx_align(path, "a b c", "en")
        assert seg["words"][0]["end"] == pytest.approx(
            seg["words"][0]["start"] + 0.01, abs=0.001
        )


class TestFakeTTSRecording:
    def test_records_every_op(self, tmp_path) -> None:
        r = Recorder()
        tts = FakeTTS(tmpdir=str(tmp_path), recorder=r)
        path = tts.tts_generate(
            scene_num=1, voice_role="V1", text="hi there", language="en"
        )
        tts.whisperx_align(path, "hi there", "en")
        tts.loudness_normalize(path, target_lufs=-16.0)
        ops = r.ops(channel="tts")
        assert ops == ["tts_generate", "whisperx_align", "loudness_normalize"]
