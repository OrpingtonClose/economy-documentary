#!/usr/bin/env python3
"""
OTIO Timeline Manager v9 — Absolute Single Source of Truth
=============================================================
Creates, reads, updates, and exports the master .otio timeline.
Every pipeline stage reads from and writes back to this file.

The timeline has these tracks:
  - V1_Video: generated LTX-2.3 video clips (or placeholder gaps with prompts)
  - VO_Narration: Qwen3-TTS narration audio segments
  - MX_Music: music/ambient bed (placeholder for future)

Key design:
  - Audio goes on first (audio-first workflow)
  - Prompts are stored as OTIO metadata on video track gaps
  - Video clips are generated to match audio timing
  - Video clips are generated slightly longer, then trimmed via source_range
  - Quality scores and generation params stored in clip metadata
  - No looping, no stretching — ever

v9 additions:
  - Prompt storage on video track gaps (store_prompt_on_gap, get_prompt_for_clip, etc.)
  - Quality tracking (quality_score, needs_regeneration, regeneration_reason)
  - Pipeline state validation (validate_audio_complete, validate_prompts_complete, etc.)
  - Enhanced narration metadata (full_text, word_count, wpm)
  - Prompt export from OTIO metadata to JSON (derivative, not source)
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

        # Stage 2: Generate prompts and store on OTIO
        tl.store_prompt_on_gap(scene_num=1, clip_index=0, prompt_data={...})

        # Stage 3: Read prompts from OTIO for video generation
        prompts = tl.get_all_prompts()

        # Stage 4: Add video clips + quality metadata
        tl.add_video_clip(scene_num=1, clip_id="scene_01_clip00",
                          video_path="clip.mp4", available_duration=10.5,
                          trimmed_duration=8.0)

        # Quality tracking
        tl.mark_clip_for_regeneration("scene_01_clip00", "low quality score")

        # Pipeline state
        state = tl.get_pipeline_state()

        # Export
        tl.save()
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
        """
        Add a narration audio segment to the VO_Narration track.
        This is the FIRST thing added — audio drives all timing.

        Args:
            scene_num: scene number
            seg_index: segment index within scene
            audio_path: path to audio file
            duration_sec: audio duration
            voice: V1/V2/V3
            text_preview: short preview (max 200 chars)
            full_text: full narration text for this segment
            word_count: number of words in the narration
            wpm: estimated words per minute
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
        clip.metadata["word_count"] = word_count or len(full_text.split())
        clip.metadata["wpm"] = wpm or (int(len(full_text.split()) / max(0.01, duration_sec) * 60) if full_text else 0)

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
    # Stage 2: Prompt storage on video track (v9)
    # ------------------------------------------------------------------

    def store_prompt_on_gap(self, scene_num, clip_index, prompt_data):
        """
        Store a generated prompt as metadata on the video track placeholder gap.

        The gap for a scene is split into per-clip gaps if this is the first
        prompt being stored. Each per-clip gap gets prompt metadata.

        Args:
            scene_num: scene number
            clip_index: clip index within the scene
            prompt_data: dict with prompt, shot_type, environment, camera_movement,
                        word_count, generation_params
        """
        track = self.video_track

        # Find the scene gap(s)
        # We need to find either:
        # a) The single pending_video gap for this scene (first prompt storage)
        # b) An existing per-clip gap with matching scene+clip_index
        for i, item in enumerate(track):
            if not isinstance(item, otio.schema.Gap):
                continue

            # Check for already-split per-clip gap
            if (item.metadata.get("scene") == scene_num and
                    item.metadata.get("prompt_clip_index") == clip_index):
                # Update existing per-clip gap
                self._write_prompt_metadata(item, prompt_data, clip_index)
                return

        # If no per-clip gap found, write on the scene-level gap
        # (The scene gap stores all prompts as a JSON dict keyed by clip_index)
        for item in track:
            if (isinstance(item, otio.schema.Gap) and
                    item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") in ("pending_video", "prompts_stored")):
                # Store prompts as a dict keyed by clip index
                prompts_key = "prompt_data"
                existing = item.metadata.get(prompts_key, {})
                if not isinstance(existing, dict):
                    existing = {}
                existing[str(clip_index)] = prompt_data
                item.metadata[prompts_key] = existing
                item.metadata["status"] = "prompts_stored"

                # Also store flat metadata for the current clip
                self._write_prompt_metadata_indexed(item, prompt_data, clip_index)
                return

        log.warning(f"No video gap found for scene {scene_num}, clip {clip_index}")

    def _write_prompt_metadata(self, gap, prompt_data, clip_index):
        """Write prompt metadata fields onto a gap item."""
        gap.metadata["prompt_text"] = prompt_data.get("prompt", "")
        gap.metadata["prompt_shot_type"] = prompt_data.get("shot_type", "")
        gap.metadata["prompt_environment"] = prompt_data.get("environment", "")
        gap.metadata["prompt_camera_movement"] = prompt_data.get("camera_movement", "")
        gap.metadata["prompt_word_count"] = prompt_data.get("word_count", 0)
        gap.metadata["prompt_generation_params"] = prompt_data.get("generation_params", {})
        gap.metadata["prompt_clip_index"] = clip_index

    def _write_prompt_metadata_indexed(self, gap, prompt_data, clip_index):
        """Write prompt metadata with clip-index prefix for multi-clip gaps."""
        prefix = f"prompt_{clip_index}_"
        gap.metadata[f"{prefix}text"] = prompt_data.get("prompt", "")
        gap.metadata[f"{prefix}shot_type"] = prompt_data.get("shot_type", "")
        gap.metadata[f"{prefix}environment"] = prompt_data.get("environment", "")
        gap.metadata[f"{prefix}camera_movement"] = prompt_data.get("camera_movement", "")
        gap.metadata[f"{prefix}word_count"] = prompt_data.get("word_count", 0)
        gap.metadata[f"{prefix}generation_params"] = prompt_data.get("generation_params", {})

    def get_prompt_for_clip(self, scene_num, clip_index):
        """
        Retrieve the prompt stored in OTIO metadata for a specific clip.

        Args:
            scene_num: scene number
            clip_index: clip index within the scene

        Returns:
            dict with prompt data, or None if not found
        """
        for item in self.video_track:
            if not isinstance(item, otio.schema.Gap):
                continue

            # Check per-clip gap
            if (item.metadata.get("scene") == scene_num and
                    item.metadata.get("prompt_clip_index") == clip_index):
                return {
                    "prompt": item.metadata.get("prompt_text", ""),
                    "shot_type": item.metadata.get("prompt_shot_type", ""),
                    "environment": item.metadata.get("prompt_environment", ""),
                    "camera_movement": item.metadata.get("prompt_camera_movement", ""),
                    "word_count": item.metadata.get("prompt_word_count", 0),
                    "generation_params": item.metadata.get("prompt_generation_params", {}),
                }

            # Check scene-level gap with prompt_data dict
            if (item.metadata.get("scene") == scene_num and
                    item.metadata.get("status") == "prompts_stored"):
                prompt_dict = item.metadata.get("prompt_data", {})
                clip_data = prompt_dict.get(str(clip_index))
                if clip_data:
                    return clip_data

        return None

    def get_all_prompts(self):
        """
        Export all prompts stored in OTIO metadata.

        Returns:
            list of dicts, each with scene_number, clip_index, and prompt data
        """
        prompts = []

        for item in self.video_track:
            if not isinstance(item, otio.schema.Gap):
                continue

            scene_num = item.metadata.get("scene")
            if scene_num is None:
                continue

            # Scene-level gap with prompt_data dict
            if item.metadata.get("status") == "prompts_stored":
                prompt_dict = item.metadata.get("prompt_data", {})
                for clip_idx_str, pdata in sorted(prompt_dict.items()):
                    prompts.append({
                        "scene_number": scene_num,
                        "clip_index": int(clip_idx_str),
                        **pdata,
                    })

        return prompts

    def export_prompts_json(self, output_path):
        """
        Export prompts from OTIO metadata to JSON for VM deployment.
        This is the ONLY way prompts leave the OTIO — the JSON is derivative.

        Args:
            output_path: path to write the JSON file
        """
        all_prompts = self.get_all_prompts()

        # Enrich with audio timing
        scenes_audio = self.get_all_scenes_audio_timing()

        export_data = []
        for p in all_prompts:
            scene_num = p["scene_number"]
            clip_idx = p["clip_index"]

            audio_segs = scenes_audio.get(scene_num, [])
            if clip_idx < len(audio_segs):
                seg = audio_segs[clip_idx]
                dur = seg["duration_sec"]
                generation_duration = dur + 0.5
                ltx_clips_needed = max(1, int(generation_duration / 5.04) + (1 if generation_duration % 5.04 > 0.5 else 0))
            else:
                dur = 5.0
                generation_duration = 5.5
                ltx_clips_needed = 1

            export_data.append({
                "clip_id": f"scene_{scene_num:02d}_clip{clip_idx:02d}",
                "scene_number": scene_num,
                "clip_index": clip_idx,
                "target_duration_sec": round(dur, 3),
                "generation_duration_sec": round(generation_duration, 3),
                "ltx_clips_needed": ltx_clips_needed,
                "prompt": p.get("prompt", ""),
                "shot_type": p.get("shot_type", ""),
                "environment": p.get("environment", ""),
                "camera_movement": p.get("camera_movement", ""),
                "word_count": p.get("word_count", 0),
                "audio_start_sec": audio_segs[clip_idx]["start_sec"] if clip_idx < len(audio_segs) else 0,
                "audio_end_sec": audio_segs[clip_idx]["end_sec"] if clip_idx < len(audio_segs) else 0,
            })

        with open(str(output_path), "w") as f:
            json.dump(export_data, f, indent=2)

        return export_data

    # ------------------------------------------------------------------
    # Stage 2b: Read audio timing for prompt generation
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
                    "full_text": item.metadata.get("full_text", ""),
                    "word_count": item.metadata.get("word_count", 0),
                    "wpm": item.metadata.get("wpm", 0),
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
                "full_text": item.metadata.get("full_text", ""),
                "word_count": item.metadata.get("word_count", 0),
                "wpm": item.metadata.get("wpm", 0),
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
                    item.metadata.get("status") in ("pending_video", "prompts_stored")):
                gap_idx = i
                break

        if gap_idx is None:
            # No placeholder — append at end (may happen on incremental builds)
            insert_idx = len(track)
        else:
            insert_idx = gap_idx
            del track[gap_idx]

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

            # Quality metadata defaults
            clip.metadata["quality_score"] = vc.get("quality_score", 0.0)
            clip.metadata["quality_needs_regeneration"] = False
            clip.metadata["quality_regeneration_reason"] = ""

            # Generation params
            clip.metadata["gen_seed"] = vc.get("gen_seed", 0)
            clip.metadata["gen_inference_steps"] = vc.get("gen_inference_steps", 0)
            clip.metadata["gen_cfg_scale"] = vc.get("gen_cfg_scale", 0.0)

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
    # Quality tracking (v9)
    # ------------------------------------------------------------------

    def mark_clip_for_regeneration(self, clip_id, reason):
        """
        Mark a video clip as needing regeneration.

        Args:
            clip_id: the clip name/ID
            reason: string describing why it needs regeneration
        """
        for item in self.video_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_id:
                item.metadata["quality_needs_regeneration"] = True
                item.metadata["quality_regeneration_reason"] = reason
                self.save()
                return True
        return False

    def set_clip_quality_score(self, clip_id, score):
        """
        Set the quality score for a video clip.

        Args:
            clip_id: the clip name/ID
            score: float quality score (0.0 - 1.0)
        """
        for item in self.video_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_id:
                item.metadata["quality_score"] = score
                return True
        return False

    def set_clip_generation_params(self, clip_id, params):
        """
        Store generation parameters on a video clip.

        Args:
            clip_id: the clip name/ID
            params: dict with gen_seed, gen_inference_steps, gen_cfg_scale, etc.
        """
        for item in self.video_track:
            if isinstance(item, otio.schema.Clip) and item.name == clip_id:
                for key, val in params.items():
                    item.metadata[f"gen_{key}"] = val
                return True
        return False

    def get_clips_needing_regeneration(self):
        """
        Get all video clips marked for regeneration.

        Returns:
            list of dicts with clip_id, scene, reason
        """
        clips = []
        for item in self.video_track:
            if (isinstance(item, otio.schema.Clip) and
                    item.metadata.get("quality_needs_regeneration")):
                clips.append({
                    "clip_id": item.name,
                    "scene": item.metadata.get("scene", 0),
                    "reason": item.metadata.get("quality_regeneration_reason", ""),
                    "quality_score": item.metadata.get("quality_score", 0.0),
                })
        return clips

    # ------------------------------------------------------------------
    # Pipeline state validation (v9)
    # ------------------------------------------------------------------

    def validate_audio_complete(self, scene_num=None):
        """
        Check if the audio track is populated.

        Args:
            scene_num: if provided, check only this scene. Otherwise check all scenes.

        Returns:
            True if audio track has narration clips (for specified scene or overall)
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
        """
        Check if all video gaps have prompts stored.

        Args:
            scene_num: if provided, check only this scene.

        Returns:
            True if all relevant video gaps have prompts
        """
        found_any = False
        for item in self.video_track:
            if not isinstance(item, otio.schema.Gap):
                continue

            item_scene = item.metadata.get("scene")
            if item_scene is None:
                continue
            if scene_num is not None and item_scene != scene_num:
                continue

            status = item.metadata.get("status", "")
            if status == "pending_video":
                return False  # Gap without prompts
            if status == "prompts_stored":
                found_any = True

        return found_any

    def validate_video_complete(self, scene_num=None):
        """
        Check if all video placeholders have been replaced with clips.

        Args:
            scene_num: if provided, check only this scene.

        Returns:
            True if no pending video gaps remain
        """
        has_clips = False
        for item in self.video_track:
            item_scene = item.metadata.get("scene") if hasattr(item, "metadata") else None

            if scene_num is not None and item_scene != scene_num:
                continue

            if isinstance(item, otio.schema.Gap):
                status = item.metadata.get("status", "")
                if status in ("pending_video", "prompts_stored"):
                    return False

            if isinstance(item, otio.schema.Clip):
                if item.metadata.get("status") == "complete":
                    has_clips = True

        return has_clips

    def get_pipeline_state(self):
        """
        Get the pipeline completion state for each scene.

        Returns:
            dict mapping scene_num -> {audio: bool, prompts: bool, video: bool}
        """
        # Discover all scenes from narration track
        scene_nums = set()
        for item in self.narration_track:
            if isinstance(item, otio.schema.Clip):
                sn = item.metadata.get("scene")
                if sn is not None:
                    scene_nums.add(sn)

        # Also check video track for scenes
        for item in self.video_track:
            sn = item.metadata.get("scene") if hasattr(item, "metadata") else None
            if sn is not None:
                scene_nums.add(sn)

        state = {}
        for sn in sorted(scene_nums):
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
            "prompts_stored_scenes": [],
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
                item_status = item.metadata.get("status", "")
                if item_status == "pending_video":
                    status["pending_scenes"].append(scene)
                elif item_status == "prompts_stored":
                    status["prompts_stored_scenes"].append(scene)
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
        if s["prompts_stored_scenes"]:
            print(f"Prompts stored (awaiting video): scenes {s['prompts_stored_scenes']}")

        # Quality info
        regen_clips = self.get_clips_needing_regeneration()
        if regen_clips:
            print(f"Clips needing regeneration: {len(regen_clips)}")
            for rc in regen_clips[:5]:
                print(f"  - {rc['clip_id']}: {rc['reason']}")

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


# Import logger at module level for use in methods
import logging
log = logging.getLogger(__name__)
