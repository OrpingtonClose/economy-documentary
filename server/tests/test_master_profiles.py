"""Tests for master_profiles, title_cards, and preview-for-final guards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tools.master_profiles import (
    DEFAULT_PROFILE,
    PREVIEW_512P,
    PROFILES,
    PreviewProfileForbidden,
    YOUTUBE_1080P,
    YOUTUBE_SHORTS_1080P_9_16,
    get_profile,
    guard_profile_for_filename,
)
from tools.title_cards import (
    SCENE_TYPES,
    CardSpec,
    TitleCardRenderError,
    end_card_for_run,
    render_card,
    title_card_for_topic,
)
from tools.assembly_tools import mux_audio_video, upscale_to_profile


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required for master-profile rendering tests",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _probe(path: str) -> dict:
    """Return a parsed ffprobe dict for the first video+audio streams."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    streams = {s["codec_type"]: s for s in data.get("streams", [])}
    return {
        "video": streams.get("video", {}),
        "audio": streams.get("audio", {}),
        "format": data.get("format", {}),
    }


def _synthesize_video(path: str, w: int, h: int, fps: int, dur: float) -> None:
    """Synthesize a deterministic test video (solid blue) via ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=blue:s={w}x{h}:r={fps}:d={dur}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def _synthesize_voiced_wav(
    path: str, dur: float = 2.0, freq: int = 440, sr: int = 48000,
) -> None:
    """Synthesize a tone WAV to stand in for narration audio."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i",
        f"sine=frequency={freq}:sample_rate={sr}:duration={dur}",
        "-ac", "1",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# MasterProfile dataclass + registry
# ---------------------------------------------------------------------------

class TestMasterProfileRegistry:
    def test_known_profiles_present(self):
        assert set(PROFILES) == {
            "YOUTUBE_1080P",
            "YOUTUBE_SHORTS_1080P_9_16",
            "PREVIEW_512P",
        }

    def test_get_profile_roundtrips(self):
        assert get_profile("YOUTUBE_1080P") is YOUTUBE_1080P
        assert get_profile("PREVIEW_512P") is PREVIEW_512P

    def test_get_profile_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown master profile"):
            get_profile("DOES_NOT_EXIST")

    def test_default_is_youtube_1080p(self):
        assert DEFAULT_PROFILE is YOUTUBE_1080P

    def test_youtube_1080p_specs(self):
        p = YOUTUBE_1080P
        assert (p.width, p.height, p.fps) == (1920, 1080, 24)
        assert p.video_codec == "libx264"
        assert p.preset == "slow"
        assert p.crf == 18
        assert p.pixel_format == "yuv420p"
        assert p.color_space == "bt709"
        assert p.audio_codec == "aac"
        assert p.audio_bitrate == "256k"
        assert p.audio_sample_rate == 48000
        assert p.integrated_lufs == -14.0
        assert p.true_peak_db == -1.0
        assert p.preview_only is False

    def test_shorts_profile_is_vertical(self):
        p = YOUTUBE_SHORTS_1080P_9_16
        assert p.width == 1080 and p.height == 1920
        assert p.fps == 30
        assert p.integrated_lufs == -14.0
        assert p.preview_only is False

    def test_preview_profile_marked(self):
        assert PREVIEW_512P.preview_only is True
        assert (PREVIEW_512P.width, PREVIEW_512P.height) == (512, 320)
        assert PREVIEW_512P.crf == 23
        assert PREVIEW_512P.preset == "veryfast"
        assert PREVIEW_512P.integrated_lufs == -16.0

    def test_profile_is_frozen(self):
        with pytest.raises(Exception):
            YOUTUBE_1080P.width = 1280  # type: ignore[misc]

    def test_variant_returns_new_instance(self):
        v = YOUTUBE_1080P.variant(preset="medium")
        assert v is not YOUTUBE_1080P
        assert v.preset == "medium"
        assert YOUTUBE_1080P.preset == "slow"
        assert v.width == YOUTUBE_1080P.width

    def test_video_and_audio_encode_args_contain_profile_values(self):
        args = YOUTUBE_1080P.video_encode_args()
        assert "libx264" in args
        assert "-crf" in args and "18" in args
        assert "-pix_fmt" in args and "yuv420p" in args
        assert "-color_primaries" in args and "bt709" in args
        a_args = YOUTUBE_1080P.audio_encode_args()
        assert "aac" in a_args
        assert "256k" in a_args
        assert "48000" in a_args

    def test_scale_filter_uses_lanczos(self):
        expr = YOUTUBE_1080P.scale_filter()
        assert "lanczos" in expr
        assert "1920:1080" in expr


# ---------------------------------------------------------------------------
# Preview-for-final guard
# ---------------------------------------------------------------------------

class TestPreviewGuard:
    def test_non_preview_profile_never_raises(self, tmp_path):
        # Final or not, non-preview profiles are always allowed.
        guard_profile_for_filename(
            YOUTUBE_1080P, str(tmp_path / "final_documentary.mp4"),
        )
        guard_profile_for_filename(
            YOUTUBE_1080P, str(tmp_path / "preview_clip.mp4"),
        )

    def test_preview_profile_allowed_for_non_final_filename(self, tmp_path):
        guard_profile_for_filename(
            PREVIEW_512P, str(tmp_path / "preview_scene.mp4"),
        )
        guard_profile_for_filename(
            PREVIEW_512P, str(tmp_path / "dashboard_proxy.mp4"),
        )

    def test_preview_profile_rejected_for_final_filename(self, tmp_path):
        with pytest.raises(PreviewProfileForbidden):
            guard_profile_for_filename(
                PREVIEW_512P, str(tmp_path / "final_documentary.mp4"),
            )
        with pytest.raises(PreviewProfileForbidden):
            guard_profile_for_filename(
                PREVIEW_512P, str(tmp_path / "FINAL_ru.mp4"),
            )
        with pytest.raises(PreviewProfileForbidden):
            guard_profile_for_filename(
                PREVIEW_512P, str(tmp_path / "some_final_output.mp4"),
            )

    def test_preview_profile_allowed_with_override(self, tmp_path):
        guard_profile_for_filename(
            PREVIEW_512P,
            str(tmp_path / "final_documentary.mp4"),
            preview_final_ok=True,
        )

    def test_mux_rejects_preview_for_final(self, tmp_path):
        video = str(tmp_path / "v.mp4")
        audio = str(tmp_path / "a.wav")
        _synthesize_video(video, 320, 180, 24, 1.0)
        _synthesize_voiced_wav(audio, dur=1.0)
        out = str(tmp_path / "final_documentary.mp4")
        result_raw = mux_audio_video(
            audio_path=audio, video_path=video, output_path=out,
            master_profile=PREVIEW_512P,
        )
        result = json.loads(result_raw)
        assert "error" in result
        assert "preview profile" in result["error"].lower()
        assert not os.path.exists(out)

    def test_mux_accepts_preview_for_final_with_override(self, tmp_path):
        video = str(tmp_path / "v.mp4")
        audio = str(tmp_path / "a.wav")
        _synthesize_video(video, 320, 180, 24, 1.0)
        _synthesize_voiced_wav(audio, dur=1.0)
        out = str(tmp_path / "final_documentary.mp4")
        result_raw = mux_audio_video(
            audio_path=audio, video_path=video, output_path=out,
            master_profile=PREVIEW_512P, preview_final_ok=True,
        )
        result = json.loads(result_raw)
        assert result.get("status") == "muxed", result
        assert os.path.exists(out)


# ---------------------------------------------------------------------------
# Profile application via mux / upscale
# ---------------------------------------------------------------------------

class TestProfileApplication:
    def test_mux_upscales_to_profile_resolution(self, tmp_path):
        # Synthesize a 512x320 video (matching the PAG-run baseline) and
        # confirm the mux re-encodes to 1920x1080 @ 24fps when driven by
        # YOUTUBE_1080P.
        video = str(tmp_path / "ltx_proxy.mp4")
        audio = str(tmp_path / "narration.wav")
        _synthesize_video(video, 512, 320, 24, 1.0)
        _synthesize_voiced_wav(audio, dur=1.0)
        out = str(tmp_path / "scene_master.mp4")

        result = json.loads(mux_audio_video(
            audio_path=audio, video_path=video, output_path=out,
            master_profile=YOUTUBE_1080P,
        ))
        assert result.get("status") == "muxed", result

        probe = _probe(out)
        assert probe["video"]["width"] == 1920
        assert probe["video"]["height"] == 1080
        assert probe["video"]["codec_name"] == "h264"
        assert probe["video"]["pix_fmt"] == "yuv420p"
        assert probe["audio"]["codec_name"] == "aac"
        assert int(probe["audio"].get("sample_rate", 0)) == 48000

    def test_mux_legacy_path_unaffected(self, tmp_path):
        # Without a master_profile, mux_audio_video must keep the legacy
        # behaviour (no upscale, libx264 fast).
        video = str(tmp_path / "clip.mp4")
        audio = str(tmp_path / "narration.wav")
        _synthesize_video(video, 640, 360, 24, 1.0)
        _synthesize_voiced_wav(audio, dur=1.0)
        out = str(tmp_path / "scene.mp4")

        result = json.loads(mux_audio_video(
            audio_path=audio, video_path=video, output_path=out,
        ))
        assert result.get("status") == "muxed", result

        probe = _probe(out)
        assert probe["video"]["width"] == 640
        assert probe["video"]["height"] == 360

    def test_mux_loops_video_when_shorter_than_audio(self, tmp_path):
        # The slice 9j frozen-frame regression: a 4.7s LTX clip paired with
        # 13s of TTS narration. Without -stream_loop the muxer holds the
        # last video frame for 8s. With the fix the video loops and the
        # output duration matches the audio (within one frame).
        video = str(tmp_path / "short_clip.mp4")
        audio = str(tmp_path / "long_narration.wav")
        _synthesize_video(video, 320, 180, 24, 1.0)
        _synthesize_voiced_wav(audio, dur=3.0)
        out = str(tmp_path / "scene_muxed.mp4")

        result = json.loads(mux_audio_video(
            audio_path=audio, video_path=video, output_path=out,
        ))
        assert result.get("status") == "muxed", result

        probe = _probe(out)
        v_dur = float(probe["video"].get("duration", 0))
        a_dur = float(probe["audio"].get("duration", 0))
        assert abs(v_dur - a_dur) < 0.1, (
            f"video {v_dur}s vs audio {a_dur}s — loop did not fill audio"
        )
        assert 2.9 <= a_dur <= 3.1, a_dur

    def test_mux_does_not_truncate_when_video_longer_than_audio(self, tmp_path):
        # Symmetric pin: when video is longer, no -shortest is added so the
        # video keeps its full duration (audio plays then ends, video tail
        # plays out — original contract).
        video = str(tmp_path / "long_clip.mp4")
        audio = str(tmp_path / "short_narration.wav")
        _synthesize_video(video, 320, 180, 24, 3.0)
        _synthesize_voiced_wav(audio, dur=1.0)
        out = str(tmp_path / "scene_long_video.mp4")

        result = json.loads(mux_audio_video(
            audio_path=audio, video_path=video, output_path=out,
        ))
        assert result.get("status") == "muxed", result

        probe = _probe(out)
        v_dur = float(probe["video"].get("duration", 0))
        assert 2.9 <= v_dur <= 3.1, (
            f"video duration {v_dur}s — must not be truncated to audio length"
        )

    def test_upscale_to_profile_writes_expected_resolution(self, tmp_path):
        src = str(tmp_path / "src.mp4")
        _synthesize_video(src, 512, 320, 24, 1.0)
        out = str(tmp_path / "upscaled.mp4")
        result = json.loads(upscale_to_profile(
            src, out, YOUTUBE_1080P,
        ))
        assert result.get("status") == "upscaled", result
        probe = _probe(out)
        assert probe["video"]["width"] == 1920
        assert probe["video"]["height"] == 1080

    def test_upscale_rejects_preview_for_final(self, tmp_path):
        src = str(tmp_path / "src.mp4")
        _synthesize_video(src, 512, 320, 24, 1.0)
        out = str(tmp_path / "final_cut.mp4")
        result = json.loads(upscale_to_profile(src, out, PREVIEW_512P))
        assert "error" in result
        assert not os.path.exists(out)


# ---------------------------------------------------------------------------
# Title / end cards
# ---------------------------------------------------------------------------

class TestTitleCardRendering:
    def test_scene_types_match_contract(self):
        assert SCENE_TYPES == (
            "title_card", "hook", "body", "outro", "end_card",
        )

    def test_cardspec_rejects_bad_kind(self):
        with pytest.raises(ValueError):
            CardSpec(kind="banner", duration_sec=2.0, title="x")

    def test_cardspec_rejects_zero_duration(self):
        with pytest.raises(ValueError):
            CardSpec(kind="title_card", duration_sec=0.0, title="x")

    def test_title_card_renders_with_drawtext(self, tmp_path):
        spec = title_card_for_topic(
            topic="Supply Shocks 2026",
            channel="Economy Documentary",
            duration_sec=2.0,
        )
        out = str(tmp_path / "title.mp4")
        render_card(spec, YOUTUBE_1080P, out)

        probe = _probe(out)
        assert probe["video"]["width"] == 1920
        assert probe["video"]["height"] == 1080
        assert probe["video"]["codec_name"] == "h264"
        # Duration within ±100ms of requested.
        dur = float(probe["format"].get("duration", 0))
        assert 1.9 <= dur <= 2.2, dur

    def test_end_card_renders_with_cta_and_sources(self, tmp_path):
        spec = end_card_for_run(
            channel="Economy Documentary",
            cta="Subscribe for more",
            sources_line="Sources in the description",
            duration_sec=3.0,
        )
        out = str(tmp_path / "end.mp4")
        render_card(spec, YOUTUBE_1080P, out)
        probe = _probe(out)
        assert probe["video"]["width"] == 1920
        dur = float(probe["format"].get("duration", 0))
        assert 2.8 <= dur <= 3.3, dur

    def test_card_renders_at_preview_geometry(self, tmp_path):
        # Even a preview profile should be able to render a card at its
        # native geometry \u2014 the preview-for-final guard only fires at
        # mux time.
        spec = title_card_for_topic(
            topic="Preview", channel="Ch", duration_sec=1.0,
        )
        out = str(tmp_path / "preview_title.mp4")
        render_card(spec, PREVIEW_512P, out)
        probe = _probe(out)
        assert probe["video"]["width"] == 512
        assert probe["video"]["height"] == 320

    def test_card_renders_at_shorts_vertical_geometry(self, tmp_path):
        spec = title_card_for_topic(
            topic="Vertical", channel="Shorts", duration_sec=1.0,
        )
        out = str(tmp_path / "shorts_title.mp4")
        render_card(spec, YOUTUBE_SHORTS_1080P_9_16, out)
        probe = _probe(out)
        assert probe["video"]["width"] == 1080
        assert probe["video"]["height"] == 1920

    def test_card_raises_when_font_missing(self, tmp_path):
        spec = title_card_for_topic(
            topic="x", channel="y", duration_sec=1.0,
        )
        out = str(tmp_path / "broken.mp4")
        with pytest.raises(TitleCardRenderError, match="font not found"):
            render_card(
                spec, YOUTUBE_1080P, out,
                font_path="/does/not/exist.ttf",
            )

    def test_card_title_with_special_characters_renders(self, tmp_path):
        # Colons and quotes are meta-chars inside ``drawtext`` \u2014 the
        # escaper must keep them rendering cleanly.
        spec = CardSpec(
            kind="title_card",
            duration_sec=1.0,
            title="It's 2026: the money is on fire",
            subtitle="Economy Documentary \u2014 Ep. 42",
        )
        out = str(tmp_path / "special.mp4")
        render_card(spec, YOUTUBE_1080P, out)
        assert os.path.exists(out)
