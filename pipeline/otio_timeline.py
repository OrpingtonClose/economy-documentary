#!/usr/bin/env python3
"""
OTIO Timeline Manager — Single Source of Truth
================================================
Creates, reads, updates, and exports the master .otio timeline.
Every pipeline stage reads from and writes back to this file.

The timeline has these tracks:
  - V1_Video: generated LTX-2.3 video clips
  - VO_Narration: Qwen3-TTS narration audio segments
  - MX_Music: music/ambient bed (placeholder for future)

Key design:
  - Audio goes on first (audio-first workflow)
  - Video clips are generated to match audio timing
  - Video clips are generated slightly longer, then trimmed via source_range
  - No looping, no stretching — ever
"""

import json
import os
from pathlib import Path

import opentimelineio as otio

# Constants
VIDEO_RATE = 24.0
AUDIO_RATE = 24000.0
INTER_SCENE_GAP_SEC = 1.0


def seconds_to_rt(sec, rate=VIDEO_RATE):
    """Convert seconds to RationalTime."""
    return otio.opentime.RationalTime(sec * rate, rate)


def seconds_to_range(start_sec, dur_sec, rate=VIDEO_RATE):
    """Convert start + duration (seconds) to TimeRange."""
    return otio.opentime.TimeRange(
        start_time=seconds_to_rt(start_sec, rate),
        duration=seconds_to_rt(dur_sec, rate),
    )


def rt_to_seconds(rt):
    """Convert RationalTime to seconds."""
    return rt.value / rt.rate


def range_duration_sec(tr):
    """Get duration of a TimeRange in seconds."""
    return rt_to_seconds(tr.duration)


class OTIOTimeline:
    """
    Manages the master .otio timeline file.

    Usage:
        tl = OTIOTimeline("war_economy_v8.otio")
        tl.create_empty("War Economy V8")

        # Stage 1: Add narration
        tl.add_narration_clip(scene_num=1, seg_index=0, audio_path="seg_00.wav",
                              duration_sec=12.3, voice="V1")

        # Stage 2: Read audio timing for prompt generation
        segments = tl.get_scene_audio_segments(scene_num=1)

        # Stage 3: Add video clips
        tl.add_video_clip(scene_num=1, clip_id="scene_01_clip00",
                          video_path="clip.mp4", available_duration=10.5,
                          trimmed_duration=8.0)

        # Export
        tl.save()
        tl.export_fcpxml("timeline.fcpxml")
    """

    def __init__(self, otio_path):
        self.path = str(otio_path)
        self.timeline = None

    # ------------------------------------------------------------------
    # Creation & I/O
    # ------------------------------------------------------------------

    def create_empty(self, name="War Economy V8"):
        """Create a fresh timeline with standard tracks."""
        self.timeline = otio.schema.Timeline(name=name)
        self.timeline.global_start_time = seconds_to_rt(0)

        video_track = otio.schema.Track(
            name="V1_Video", kind=otio.schema.TrackKind.Video
        )
        narr_track = otio.schema.Track(
            name="VO_Narration", kind=otio.schema.TrackKind.Audio
        )
        music_track = otio.schema.Track(
            name="MX_Music", kind=otio.schema.TrackKind.Audio
        )

        self.timeline.tracks.append(video_track)
        self.timeline.tracks.append(narr_track)
        self.timeline.tracks.append(music_track)

        self.timeline.metadata["pipeline_version"] = "v8_otio_centric"
        self.timeline.metadata["video_model"] = "LTX-2.3-22B bf16"
        self.timeline.metadata["tts_model"] = "Qwen3-TTS VoiceDesign"
        self.timeline.metadata["resolution"] = "768x512"
        self.timeline.metadata["fps"] = int(VIDEO_RATE)
        self.timeline.metadata["no_looping"] = True
        self.timeline.metadata["no_stretching"] = True
        self.timeline.metadata["no_text_on_screen"] = True

        self.save()
        return self

    def load(self):
        """Load timeline from .otio file."""
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Timeline not found: {self.path}")
        self.timeline = otio.adapters.read_from_file(self.path)
        return self

    def save(self):
        """Save timeline to .otio file."""
        otio.adapters.write_to_file(self.timeline, self.path)
        return self

    def export_fcpxml(self, output_path):
        """Export timeline to Final Cut Pro XML."""
        otio.adapters.write_to_file(self.timeline, str(output_path))

    # ------------------------------------------------------------------
    # Track accessors
    # ------------------------------------------------------------------

    def _get_track(self, name):
        for t in self.timeline.tracks:
            if t.name == name:
                return t
        raise KeyError(f"Track '{name}' not found in timeline")

    @property
    def video_track(self):
        return self._get_track("V1_Video")

    @property
    def narration_track(self):
        return self._get_track("VO_Narration")

    @property
    def music_track(self):
        return self._get_track("MX_Music")

    # ------------------------------------------------------------------
    # Stage 1: Narration (audio-first)
    # ------------------------------------------------------------------

    def add_narration_clip(self, scene_num, seg_index, audio_path,
                           duration_sec, voice="V1", text_preview=""):
        """
        Add a narration audio segment to the VO_Narration track.
        This is the FIRST thing added — audio drives all timing.
        """
        media_ref = otio.schema.ExternalReference(
            target_url=str(audio_path),
            available_range=seconds_to_range(0, duration_sec, AUDIO_RATE),
        )

        clip_name = f"scene_{scene_num:02d}_seg{seg_index:02d}_{voice}"
        clip = otio.schema.Clip(
            name=clip_name,
            media_reference=media_ref,
            source_range=seconds_to_range(0, duration_sec, AUDIO_RATE),
        )
        clip.metadata["scene"] = scene_num
        clip.metadata["voice"] = voice
        clip.metadata["seg_index"] = seg_index
        clip.metadata["text_preview"] = text_preview[:200]

        self.narration_track.append(clip)
        return clip

    def add_scene_gap(self, scene_num):
        """Add inter-scene gap on all tracks."""
        gap_dur = INTER_SCENE_GAP_SEC
        for track in [self.video_track, self.narration_track, self.music_track]:
            gap = otio.schema.Gap(
                source_range=seconds_to_range(0, gap_dur,
                                              AUDIO_RATE if track.kind == otio.schema.TrackKind.Audio else VIDEO_RATE),
                name=f"gap_after_scene_{scene_num:02d}",
            )
            track.append(gap)

    def add_narration_scene_placeholder_video(self, scene_num, narr_duration_sec):
        """
        After adding all narration for a scene, add a placeholder gap on the
        video track so it stays in sync. This gap will be replaced with actual
        video clips in stage 3.
        """
        gap = otio.schema.Gap(
            source_range=seconds_to_range(0, narr_duration_sec),
            name=f"scene_{scene_num:02d}_video_pending",
        )
        gap.metadata["scene"] = scene_num
        gap.metadata["status"] = "pending_video"
        self.video_track.append(gap)

    def add_music_placeholder(self, scene_num, duration_sec):
        """Add a gap on the music track for this scene."""
        gap = otio.schema.Gap(
            source_range=seconds_to_range(0, duration_sec, AUDIO_RATE),
            name=f"scene_{scene_num:02d}_music_placeholder",
        )
        gap.metadata["scene"] = scene_num
        self.music_track.append(gap)

    # ------------------------------------------------------------------
    # Stage 2: Read audio timing for prompt generation
    # ------------------------------------------------------------------

    def get_scene_audio_segments(self, scene_num):
        """
        Read narration segments for a scene from the OTIO timeline.
        Returns list of dicts with timing info for prompt generation.
        """
        segments = []
        cursor_sec = 0.0

        for item in self.narration_track:
            if isinstance(item, otio.schema.Gap):
                cursor_sec += range_duration_sec(item.source_range)
                continue

            if not isinstance(item, otio.schema.Clip):
                continue

            item_scene = item.metadata.get("scene", 0)
            dur = range_duration_sec(item.source_range)

            if item_scene == scene_num:
                segments.append({
                    "clip_name": item.name,
                    "scene_num": scene_num,
                    "seg_index": item.metadata.get("seg_index", 0),
                    "voice": item.metadata.get("voice", "V1"),
                    "start_sec": cursor_sec,
                    "duration_sec": dur,
                    "end_sec": cursor_sec + dur,
                    "text_preview": item.metadata.get("text_preview", ""),
                    "audio_path": item.media_reference.target_url
                                  if hasattr(item, "media_reference") and item.media_reference else "",
                })

            cursor_sec += dur

        return segments

    def get_all_scenes_audio_timing(self):
        """
        Get audio timing for all scenes. Returns dict: scene_num -> list of segments.
        """
        scenes = {}
        cursor_sec = 0.0

        for item in self.narration_track:
            if isinstance(item, otio.schema.Gap):
                cursor_sec += range_duration_sec(item.source_range)
                continue
            if not isinstance(item, otio.schema.Clip):
                continue

            scene_num = item.metadata.get("scene", 0)
            dur = range_duration_sec(item.source_range)

            if scene_num not in scenes:
                scenes[scene_num] = []

            scenes[scene_num].append({
                "clip_name": item.name,
                "scene_num": scene_num,
                "seg_index": item.metadata.get("seg_index", 0),
                "voice": item.metadata.get("voice", "V1"),
                "start_sec": cursor_sec,
                "duration_sec": dur,
                "end_sec": cursor_sec + dur,
                "text_preview": item.metadata.get("text_preview", ""),
                "audio_path": item.media_reference.target_url
                              if hasattr(item, "media_reference") and item.media_reference else "",
            })
            cursor_sec += dur

        return scenes

    def get_scene_total_duration(self, scene_num):
        """Get total narration duration for a scene (in seconds)."""
        segs = self.get_scene_audio_segments(scene_num)
        if not segs:
            return 0.0
        return sum(s["duration_sec"] for s in segs)

    # ------------------------------------------------------------------
    # Stage 3: Video clip placement
    # ------------------------------------------------------------------

    def replace_video_gap_with_clips(self, scene_num, video_clips):
        """
        Replace the placeholder video gap for a scene with actual video clips.

        video_clips: list of dicts, each with:
            - clip_id: str
            - video_path: str (path to mp4)
            - available_duration_sec: float (actual generated length)
            - trimmed_duration_sec: float (how long it should appear in timeline)
            - prompt: str (for metadata)
        """
        track = self.video_track
        narr_dur = self.get_scene_total_duration(scene_num)

        # Find and remove the placeholder gap for this scene
        gap_idx = None
        for i, item in enumerate(track):
            if (isinstance(item, otio.schema.Gap) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") == "pending_video"):
                gap_idx = i
                break

        if gap_idx is None:
            # No placeholder — append at end (may happen on incremental builds)
            insert_idx = len(track)
        else:
            insert_idx = gap_idx
            del track[gap_idx]

        # Calculate total video duration we have
        total_video_dur = sum(vc["trimmed_duration_sec"] for vc in video_clips)

        # Insert clips
        inserted = 0
        cursor = 0.0
        for vc in video_clips:
            remaining = narr_dur - cursor
            if remaining <= 0.04:
                break

            trim_dur = min(vc["trimmed_duration_sec"], remaining)

            media_ref = otio.schema.ExternalReference(
                target_url=str(vc["video_path"]),
                available_range=seconds_to_range(0, vc["available_duration_sec"]),
            )
            clip = otio.schema.Clip(
                name=vc["clip_id"],
                media_reference=media_ref,
                source_range=seconds_to_range(0, trim_dur),
            )
            clip.metadata["scene"] = scene_num
            clip.metadata["prompt"] = vc.get("prompt", "")[:300]
            clip.metadata["available_duration"] = vc["available_duration_sec"]
            clip.metadata["trimmed_duration"] = trim_dur
            clip.metadata["status"] = "complete"

            track.insert(insert_idx + inserted, clip)
            inserted += 1
            cursor += trim_dur

        # If video is shorter than narration, add a trailing gap (black)
        if cursor < narr_dur - 0.04:
            gap_dur = narr_dur - cursor
            gap = otio.schema.Gap(
                source_range=seconds_to_range(0, gap_dur),
                name=f"scene_{scene_num:02d}_video_shortfall",
            )
            gap.metadata["scene"] = scene_num
            track.insert(insert_idx + inserted, gap)

        self.save()

    # ------------------------------------------------------------------
    # Query / Status
    # ------------------------------------------------------------------

    def get_timeline_status(self):
        """Get a summary of the timeline state."""
        status = {
            "name": self.timeline.name,
            "scenes": {},
            "total_narration_sec": 0,
            "total_video_sec": 0,
            "total_video_gaps_sec": 0,
            "video_clips_count": 0,
            "narration_clips_count": 0,
            "pending_scenes": [],
        }

        # Narration timing
        for item in self.narration_track:
            if isinstance(item, otio.schema.Clip):
                scene = item.metadata.get("scene", 0)
                dur = range_duration_sec(item.source_range)
                status["total_narration_sec"] += dur
                status["narration_clips_count"] += 1
                if scene not in status["scenes"]:
                    status["scenes"][scene] = {"narration_sec": 0, "video_sec": 0, "video_gaps_sec": 0, "clips": 0}
                status["scenes"][scene]["narration_sec"] += dur

        # Video track
        for item in self.video_track:
            scene = item.metadata.get("scene", 0) if hasattr(item, "metadata") else 0
            dur = range_duration_sec(item.source_range)

            if isinstance(item, otio.schema.Clip):
                status["total_video_sec"] += dur
                status["video_clips_count"] += 1
                if scene in status["scenes"]:
                    status["scenes"][scene]["video_sec"] += dur
                    status["scenes"][scene]["clips"] += 1
            elif isinstance(item, otio.schema.Gap):
                if item.metadata.get("status") == "pending_video":
                    status["pending_scenes"].append(scene)
                status["total_video_gaps_sec"] += dur
                if scene in status["scenes"]:
                    status["scenes"][scene]["video_gaps_sec"] += dur

        status["total_duration_sec"] = status["total_narration_sec"]
        status["total_duration_min"] = round(status["total_narration_sec"] / 60, 1)
        status["completion_pct"] = round(
            status["total_video_sec"] / max(0.1, status["total_narration_sec"]) * 100, 1
        )

        return status

    def print_status(self):
        """Print a human-readable timeline status."""
        s = self.get_timeline_status()
        print(f"\n{'='*60}")
        print(f"Timeline: {s['name']}")
        print(f"{'='*60}")
        print(f"Duration: {s['total_duration_sec']:.1f}s ({s['total_duration_min']} min)")
        print(f"Narration: {s['total_narration_sec']:.1f}s ({s['narration_clips_count']} segments)")
        print(f"Video: {s['total_video_sec']:.1f}s ({s['video_clips_count']} clips)")
        print(f"Video gaps: {s['total_video_gaps_sec']:.1f}s")
        print(f"Completion: {s['completion_pct']}%")
        if s["pending_scenes"]:
            print(f"Pending video: scenes {s['pending_scenes']}")
        print(f"{'='*60}")

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def to_assembly_json(self, output_path):
        """Export timeline as a JSON assembly manifest for ffmpeg rendering."""
        scenes_audio = self.get_all_scenes_audio_timing()
        assembly = {
            "timeline": self.timeline.name,
            "total_scenes": len(scenes_audio),
            "scenes": [],
        }

        for scene_num in sorted(scenes_audio.keys()):
            audio_segs = scenes_audio[scene_num]
            narr_dur = sum(s["duration_sec"] for s in audio_segs)

            # Collect video clips for this scene
            video_clips = []
            for item in self.video_track:
                if (isinstance(item, otio.schema.Clip) and
                        item.metadata.get("scene") == scene_num):
                    video_clips.append({
                        "clip_id": item.name,
                        "video_path": item.media_reference.target_url if item.media_reference else "",
                        "trimmed_duration_sec": range_duration_sec(item.source_range),
                        "available_duration_sec": item.metadata.get("available_duration", 0),
                    })

            assembly["scenes"].append({
                "scene_number": scene_num,
                "narration_duration_sec": round(narr_dur, 3),
                "narration_segments": [{
                    "file": s["audio_path"],
                    "voice": s["voice"],
                    "duration_sec": round(s["duration_sec"], 3),
                } for s in audio_segs],
                "video_clips": video_clips,
            })

        with open(str(output_path), "w") as f:
            json.dump(assembly, f, indent=2)

        return assembly
