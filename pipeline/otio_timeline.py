#!/usr/bin/env python3
"""
OTIO Timeline Manager — Absolute Single Source of Truth
=========================================================
Creates, reads, updates, and exports the master .otio timeline.
Every pipeline stage reads from and writes back to this file.

The timeline has these tracks:
  - V1_Video: generated LTX-2.3 video clips (or placeholder gaps with prompt metadata)
  - VO_Narration: Qwen3-TTS narration audio segments
  - MX_Music: music/ambient bed (placeholder for future)

Key design:
  - Audio goes on first (audio-first workflow)
  - Prompts are stored as OTIO metadata on video track gaps
  - Video clips are generated to match audio timing
  - Video clips are generated slightly longer, then trimmed via source_range
  - Quality scores and generation params stored in clip metadata
  - No looping, no stretching — ever

Metadata prefixes:
  - prompt_*: prompt-related metadata on video track gaps/clips
  - quality_*: quality tracking metadata on video clips
  - gen_*: generation parameters on video clips
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
        tl = OTIOTimeline("war_economy_v9.otio")
        tl.create_empty("War Economy V9")

        # Stage 1: Add narration
        tl.add_narration_clip(scene_num=1, seg_index=0, audio_path="seg_00.wav",
                              duration_sec=12.3, voice="V1")

        # Stage 2: Read audio timing for prompt generation
        segments = tl.get_scene_audio_segments(scene_num=1)

        # Stage 2b: Store prompts on video track
        tl.store_prompt_on_gap(scene_num=1, clip_index=0, prompt_data={...})

        # Stage 3: Add video clips
        tl.add_video_clip(scene_num=1, clip_id="scene_01_clip00",
                          video_path="clip.mp4", available_duration=10.5,
                          trimmed_duration=8.0)

        # Quality tracking
        tl.mark_clip_for_regeneration("scene_01_clip00", "motion artifacts")

        # Pipeline state
        state = tl.get_pipeline_state()

        # Export
        tl.save()
        tl.export_fcpxml("timeline.fcpxml")
        tl.export_prompts_json("prompts.json")
    """

    def __init__(self, otio_path):
        self.path = str(otio_path)
        self.timeline = None

    # ------------------------------------------------------------------
    # Creation & I/O
    # ------------------------------------------------------------------

    def create_empty(self, name="War Economy V9"):
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

        self.timeline.metadata["pipeline_version"] = "v9_otio_centric"
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
                           duration_sec, voice="V1", text_preview="",
                           full_text="", word_count=0, wpm=0):
        """Add a narration audio segment to the VO_Narration track.

        This is the FIRST thing added — audio drives all timing.

        Args:
            scene_num: scene number
            seg_index: segment index within the scene
            audio_path: path to the audio WAV file
            duration_sec: duration of the audio segment in seconds
            voice: narration voice identifier (V1/V2/V3)
            text_preview: short preview of narration text
            full_text: full narration text for this segment
            word_count: word count of the narration text
            wpm: estimated speaking rate in words per minute
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
        clip.metadata["full_text"] = full_text
        clip.metadata["word_count"] = word_count or len(full_text.split()) if full_text else 0
        clip.metadata["wpm"] = wpm

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
        """After adding all narration for a scene, add a placeholder gap on the
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
        """Read narration segments for a scene from the OTIO timeline.

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
                    "full_text": item.metadata.get("full_text", ""),
                    "word_count": item.metadata.get("word_count", 0),
                    "wpm": item.metadata.get("wpm", 0),
                    "audio_path": item.media_reference.target_url
                                  if hasattr(item, "media_reference") and item.media_reference else "",
                })

            cursor_sec += dur

        return segments

    def get_all_scenes_audio_timing(self):
        """Get audio timing for all scenes. Returns dict: scene_num -> list of segments."""
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
                "full_text": item.metadata.get("full_text", ""),
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
    # Stage 2b: Prompt storage on video track
    # ------------------------------------------------------------------

    def store_prompt_on_gap(self, scene_num, clip_index, prompt_data):
        """Store a full prompt and its metadata on a video track gap.

        The video track gap for a scene gets split into per-clip gaps, each
        carrying its own prompt metadata. If per-clip gaps already exist,
        the prompt is stored on the matching gap.

        Args:
            scene_num: scene number
            clip_index: index of the clip within the scene
            prompt_data: dict with prompt, shot_type, environment, camera_movement,
                         word_count, generation_params, etc.
        """
        track = self.video_track

        # Find the gap(s) for this scene
        # Look for an existing per-clip gap first
        target_name = f"scene_{scene_num:02d}_clip{clip_index:02d}_prompt"
        for item in track:
            if isinstance(item, otio.schema.Gap) and item.name == target_name:
                # Update existing per-clip gap with prompt data
                self._write_prompt_metadata(item, prompt_data)
                return

        # If we have a single pending_video gap for the scene, we need to split it
        # into per-clip gaps. Find the pending gap.
        gap_idx = None
        for i, item in enumerate(track):
            if (isinstance(item, otio.schema.Gap) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") == "pending_video"):
                gap_idx = i
                break

        if gap_idx is not None:
            # This is the first prompt for this scene — the gap needs splitting.
            # We'll convert the monolithic gap to individual per-clip gaps as
            # prompts come in. For now, just tag the existing gap with this prompt
            # and mark it for later splitting.
            gap = track[gap_idx]
            if "prompt_clips" not in gap.metadata:
                gap.metadata["prompt_clips"] = {}
            gap.metadata["prompt_clips"][str(clip_index)] = prompt_data
            gap.metadata["status"] = "prompts_pending"
            return

        # Look for a prompts_pending gap (already partially populated)
        for item in track:
            if (isinstance(item, otio.schema.Gap) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") == "prompts_pending"):
                if "prompt_clips" not in item.metadata:
                    item.metadata["prompt_clips"] = {}
                item.metadata["prompt_clips"][str(clip_index)] = prompt_data
                return

    def _write_prompt_metadata(self, gap, prompt_data):
        """Write prompt metadata fields onto a gap/clip with consistent prefixes."""
        gap.metadata["prompt_text"] = prompt_data.get("prompt", "")
        gap.metadata["prompt_shot_type"] = prompt_data.get("shot_type", "")
        gap.metadata["prompt_environment"] = prompt_data.get("environment", "")
        gap.metadata["prompt_camera_movement"] = prompt_data.get("camera_movement", "")
        gap.metadata["prompt_word_count"] = prompt_data.get("word_count", 0)
        gap.metadata["prompt_dramatic_position"] = prompt_data.get("dramatic_position", "")
        gap.metadata["prompt_generation_params"] = prompt_data.get("generation_params", {})

    def store_all_scene_prompts(self, scene_num, prompts_list):
        """Store all prompts for a scene, splitting the video gap into per-clip gaps.

        This is the preferred method — store all prompts at once so the gap can
        be properly split into per-clip segments with correct durations.

        Args:
            scene_num: scene number
            prompts_list: list of prompt dicts (one per clip), each with
                          target_duration_sec and prompt metadata
        """
        track = self.video_track

        # Find the pending video gap for this scene
        gap_idx = None
        for i, item in enumerate(track):
            if (isinstance(item, otio.schema.Gap) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") in ("pending_video", "prompts_pending")):
                gap_idx = i
                break

        if gap_idx is None:
            return

        # Remove the monolithic gap
        del track[gap_idx]

        # Insert per-clip gaps with prompt metadata and correct durations
        for ci, prompt_data in enumerate(prompts_list):
            dur = prompt_data.get("target_duration_sec", 5.0)
            gap = otio.schema.Gap(
                source_range=seconds_to_range(0, dur),
                name=f"scene_{scene_num:02d}_clip{ci:02d}_prompt",
            )
            gap.metadata["scene"] = scene_num
            gap.metadata["clip_index"] = ci
            gap.metadata["status"] = "prompt_ready"
            self._write_prompt_metadata(gap, prompt_data)
            track.insert(gap_idx + ci, gap)

    def get_prompt_for_clip(self, scene_num, clip_index):
        """Retrieve prompt metadata from OTIO for a specific clip.

        Returns dict with prompt data, or None if not found.
        """
        track = self.video_track

        # Check per-clip gaps first
        target_name = f"scene_{scene_num:02d}_clip{clip_index:02d}_prompt"
        for item in track:
            if isinstance(item, otio.schema.Gap) and item.name == target_name:
                return {
                    "prompt": item.metadata.get("prompt_text", ""),
                    "shot_type": item.metadata.get("prompt_shot_type", ""),
                    "environment": item.metadata.get("prompt_environment", ""),
                    "camera_movement": item.metadata.get("prompt_camera_movement", ""),
                    "word_count": item.metadata.get("prompt_word_count", 0),
                    "dramatic_position": item.metadata.get("prompt_dramatic_position", ""),
                    "generation_params": item.metadata.get("prompt_generation_params", {}),
                    "duration_sec": range_duration_sec(item.source_range),
                }

        # Check prompts_pending gap (fallback for partially stored)
        for item in track:
            if (isinstance(item, otio.schema.Gap) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") == "prompts_pending"):
                clips_data = item.metadata.get("prompt_clips", {})
                return clips_data.get(str(clip_index))

        # Check video clips (prompts stored on completed clips)
        for item in track:
            if (isinstance(item, otio.schema.Clip) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("clip_index") == clip_index):
                return {
                    "prompt": item.metadata.get("prompt_text", item.metadata.get("prompt", "")),
                    "shot_type": item.metadata.get("prompt_shot_type", ""),
                    "environment": item.metadata.get("prompt_environment", ""),
                }

        return None

    def get_all_prompts(self):
        """Export all prompts from OTIO metadata.

        Returns list of prompt dicts (replaces the need for a separate JSON file).
        """
        prompts = []
        track = self.video_track

        for item in track:
            if isinstance(item, otio.schema.Gap):
                scene = item.metadata.get("scene")
                if scene is None:
                    continue

                # Per-clip prompt gap
                if item.metadata.get("status") == "prompt_ready":
                    prompts.append({
                        "clip_id": item.name.replace("_prompt", ""),
                        "scene_number": scene,
                        "clip_index": item.metadata.get("clip_index", 0),
                        "target_duration_sec": range_duration_sec(item.source_range),
                        "prompt": item.metadata.get("prompt_text", ""),
                        "shot_type": item.metadata.get("prompt_shot_type", ""),
                        "environment": item.metadata.get("prompt_environment", ""),
                        "camera_movement": item.metadata.get("prompt_camera_movement", ""),
                        "word_count": item.metadata.get("prompt_word_count", 0),
                        "dramatic_position": item.metadata.get("prompt_dramatic_position", ""),
                        "generation_params": item.metadata.get("prompt_generation_params", {}),
                    })

                # Monolithic gap with embedded prompts
                elif item.metadata.get("status") == "prompts_pending":
                    clips_data = item.metadata.get("prompt_clips", {})
                    for ci_str, pdata in sorted(clips_data.items()):
                        prompts.append(pdata)

        return prompts

    # ------------------------------------------------------------------
    # Stage 3: Video clip placement
    # ------------------------------------------------------------------

    def replace_video_gap_with_clips(self, scene_num, video_clips):
        """Replace the placeholder video gap(s) for a scene with actual video clips.

        Handles both monolithic pending_video gaps and per-clip prompt gaps.

        video_clips: list of dicts, each with:
            - clip_id: str
            - video_path: str (path to mp4)
            - available_duration_sec: float (actual generated length)
            - trimmed_duration_sec: float (how long it should appear in timeline)
            - prompt: str (for metadata)
        """
        track = self.video_track
        narr_dur = self.get_scene_total_duration(scene_num)

        # Find and remove all gaps for this scene (both monolithic and per-clip)
        gaps_to_remove = []
        first_gap_idx = None
        for i, item in enumerate(track):
            if isinstance(item, otio.schema.Gap) and item.metadata.get("scene") == scene_num:
                status = item.metadata.get("status", "")
                if status in ("pending_video", "prompts_pending", "prompt_ready"):
                    gaps_to_remove.append(i)
                    if first_gap_idx is None:
                        first_gap_idx = i

        if first_gap_idx is None:
            # No placeholder — append at end (may happen on incremental builds)
            insert_idx = len(track)
        else:
            insert_idx = first_gap_idx
            # Remove in reverse order to preserve indices
            for idx in reversed(gaps_to_remove):
                del track[idx]

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
            clip.metadata["clip_index"] = inserted
            clip.metadata["prompt_text"] = vc.get("prompt", "")[:500]
            clip.metadata["available_duration"] = vc["available_duration_sec"]
            clip.metadata["trimmed_duration"] = trim_dur
            clip.metadata["status"] = "complete"

            # Carry over prompt metadata if available
            prompt_meta = vc.get("prompt_metadata", {})
            if prompt_meta:
                clip.metadata["prompt_shot_type"] = prompt_meta.get("shot_type", "")
                clip.metadata["prompt_environment"] = prompt_meta.get("environment", "")
                clip.metadata["prompt_camera_movement"] = prompt_meta.get("camera_movement", "")

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
    # Quality tracking
    # ------------------------------------------------------------------

    def set_clip_quality(self, clip_id, quality_score, needs_regeneration=False,
                         regeneration_reason=""):
        """Set quality metadata on a video clip.

        Args:
            clip_id: the clip name/id to update
            quality_score: float 0.0-1.0
            needs_regeneration: bool
            regeneration_reason: str explaining why regeneration is needed
        """
        for item in self.video_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_id:
                item.metadata["quality_score"] = quality_score
                item.metadata["quality_needs_regeneration"] = needs_regeneration
                item.metadata["quality_regeneration_reason"] = regeneration_reason
                return True
        return False

    def mark_clip_for_regeneration(self, clip_id, reason):
        """Mark a video clip as needing regeneration.

        Args:
            clip_id: the clip name/id
            reason: why regeneration is needed (e.g., "motion artifacts", "wrong framing")
        """
        return self.set_clip_quality(clip_id, quality_score=0.0,
                                     needs_regeneration=True,
                                     regeneration_reason=reason)

    def get_clips_needing_regeneration(self):
        """Return a list of clips that have been marked for regeneration.

        Returns list of dicts with clip_id, scene, reason.
        """
        clips = []
        for item in self.video_track:
            if (isinstance(item, otio.schema.Clip) and
                    item.metadata.get("quality_needs_regeneration", False)):
                clips.append({
                    "clip_id": item.name,
                    "scene": item.metadata.get("scene", 0),
                    "reason": item.metadata.get("quality_regeneration_reason", ""),
                    "quality_score": item.metadata.get("quality_score", 0.0),
                })
        return clips

    def set_clip_generation_metadata(self, clip_id, seed=None, inference_steps=None,
                                     cfg_scale=None, generation_time=None):
        """Store generation parameters on a video clip for reproducibility.

        Args:
            clip_id: the clip name/id
            seed: random seed used
            inference_steps: number of inference steps
            cfg_scale: classifier-free guidance scale
            generation_time: time taken to generate in seconds
        """
        for item in self.video_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_id:
                if seed is not None:
                    item.metadata["gen_seed"] = seed
                if inference_steps is not None:
                    item.metadata["gen_inference_steps"] = inference_steps
                if cfg_scale is not None:
                    item.metadata["gen_cfg_scale"] = cfg_scale
                if generation_time is not None:
                    item.metadata["gen_time_sec"] = generation_time
                return True
        return False

    # ------------------------------------------------------------------
    # Pipeline state validation
    # ------------------------------------------------------------------

    def validate_audio_complete(self, scene_num=None):
        """Check if the audio track is populated.

        Args:
            scene_num: if provided, check only this scene; otherwise check all

        Returns True if audio track has narration clips (for the specified scene or overall).
        """
        for item in self.narration_track:
            if not isinstance(item, otio.schema.Clip):
                continue
            if scene_num is None:
                return True  # At least one clip exists
            if item.metadata.get("scene") == scene_num:
                return True
        return False

    def validate_prompts_complete(self, scene_num=None):
        """Check if all video gaps have prompts stored.

        Args:
            scene_num: if provided, check only this scene; otherwise check all

        Returns True if all video placeholders have associated prompts.
        """
        for item in self.video_track:
            if not isinstance(item, otio.schema.Gap):
                continue
            item_scene = item.metadata.get("scene")
            if item_scene is None:
                continue
            if scene_num is not None and item_scene != scene_num:
                continue

            status = item.metadata.get("status", "")
            # pending_video means no prompts yet
            if status == "pending_video":
                return False
            # prompt_ready is good; prompts_pending means partially stored
            if status == "prompts_pending":
                return False

        # Also verify that there ARE prompt gaps or completed clips
        has_prompts = False
        for item in self.video_track:
            if isinstance(item, otio.schema.Gap):
                item_scene = item.metadata.get("scene")
                if scene_num is not None and item_scene != scene_num:
                    continue
                if item.metadata.get("status") == "prompt_ready":
                    has_prompts = True
                    break
            elif isinstance(item, otio.schema.Clip):
                item_scene = item.metadata.get("scene")
                if scene_num is not None and item_scene != scene_num:
                    continue
                if item.metadata.get("prompt_text") or item.metadata.get("prompt"):
                    has_prompts = True
                    break

        return has_prompts

    def validate_video_complete(self, scene_num=None):
        """Check if all video placeholders are replaced with clips.

        Args:
            scene_num: if provided, check only this scene; otherwise check all

        Returns True if there are no pending video gaps.
        """
        has_clips = False
        for item in self.video_track:
            item_scene = item.metadata.get("scene") if hasattr(item, "metadata") else None

            if scene_num is not None and item_scene != scene_num:
                continue

            if isinstance(item, otio.schema.Gap):
                status = item.metadata.get("status", "")
                if status in ("pending_video", "prompts_pending", "prompt_ready"):
                    return False
            elif isinstance(item, otio.schema.Clip):
                if item_scene is not None:
                    has_clips = True

        return has_clips

    def get_pipeline_state(self):
        """Get the pipeline state for each scene: which phases are complete.

        Returns dict mapping scene_num to phase completion status.
        """
        # Discover all scenes from narration track
        scenes = set()
        for item in self.narration_track:
            if isinstance(item, otio.schema.Clip):
                scenes.add(item.metadata.get("scene", 0))

        state = {}
        for sn in sorted(scenes):
            state[sn] = {
                "audio": self.validate_audio_complete(sn),
                "prompts": self.validate_prompts_complete(sn),
                "video": self.validate_video_complete(sn),
            }
        return state

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
            "prompts_stored": 0,
        }

        # Narration timing
        for item in self.narration_track:
            if isinstance(item, otio.schema.Clip):
                scene = item.metadata.get("scene", 0)
                dur = range_duration_sec(item.source_range)
                status["total_narration_sec"] += dur
                status["narration_clips_count"] += 1
                if scene not in status["scenes"]:
                    status["scenes"][scene] = {
                        "narration_sec": 0, "video_sec": 0,
                        "video_gaps_sec": 0, "clips": 0,
                        "prompts": 0,
                    }
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
                gap_status = item.metadata.get("status", "")
                if gap_status == "pending_video":
                    status["pending_scenes"].append(scene)
                elif gap_status == "prompt_ready":
                    status["prompts_stored"] += 1
                    if scene in status["scenes"]:
                        status["scenes"][scene]["prompts"] += 1
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
        print(f"Prompts stored: {s['prompts_stored']}")
        print(f"Completion: {s['completion_pct']}%")
        if s["pending_scenes"]:
            print(f"Pending video: scenes {s['pending_scenes']}")
        print(f"{'='*60}")

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_prompts_json(self, output_path):
        """Export prompts from OTIO metadata to JSON for VM deployment.

        This is the ONLY way prompts leave the OTIO — the JSON is a
        derivative, not the source. The OTIO metadata is authoritative.

        Args:
            output_path: path to write the JSON file

        Returns:
            list of prompt dicts that were exported
        """
        prompts = self.get_all_prompts()

        # Enrich with generation parameters for the video generator
        enriched = []
        for p in prompts:
            dur = p.get("target_duration_sec", 5.0)
            generation_duration = dur + 0.5
            ltx_clips_needed = max(1, int(generation_duration / 5.04) + (1 if generation_duration % 5.04 > 0.5 else 0))

            enriched.append({
                **p,
                "generation_duration_sec": round(generation_duration, 3),
                "ltx_clips_needed": ltx_clips_needed,
            })

        with open(str(output_path), "w") as f:
            json.dump(enriched, f, indent=2)

        return enriched

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
