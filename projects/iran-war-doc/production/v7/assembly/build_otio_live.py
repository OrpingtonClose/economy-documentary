#!/usr/bin/env python3
"""
WAR ECONOMY — Live OTIO Builder
=================================
Rebuilds the OTIO timeline from whatever clips currently exist.
Narration is the master clock. Video clips fill in as they arrive.
Remaining gaps are reported so generation knows what's still needed.

Run anytime — it's idempotent. Each run produces:
  1. war_economy_live.otio  — current timeline
  2. otio_status.json       — machine-readable gap report

Usage:
  python3 build_otio_live.py
  python3 build_otio_live.py --clip-dir /path/to/extra/clips
"""

import json
import os
import glob
import sys
from pathlib import Path

try:
    import opentimelineio as otio
except ImportError:
    print("Installing opentimelineio...")
    os.system("pip install opentimelineio -q")
    import opentimelineio as otio

RATE = 24.0
PRODUCTION = "/home/user/workspace/iran-war-doc/production"
NARRATION = os.path.join(PRODUCTION, "narration_audio")
ORIGINAL_PROMPTS = os.path.join(PRODUCTION, "all_video_prompts.json")
FILL_PROMPTS = os.path.join(PRODUCTION, "fill_clips_final.json")
B2_BASE = "https://f004.backblazeb2.com/file/economy-vid-assets/v7_war_economy"
OUTPUT_OTIO = os.path.join(PRODUCTION, "war_economy_live.otio")
OUTPUT_STATUS = os.path.join(PRODUCTION, "otio_status.json")
UPLOAD_TRACKER = os.path.join(PRODUCTION, "upload_tracker.json")

INTER_SCENE_GAP = 1.0  # seconds of black between scenes


def seconds_to_time(sec, rate=RATE):
    return otio.opentime.RationalTime(sec * rate, rate)


def seconds_to_range(start_sec, dur_sec, rate=RATE):
    return otio.opentime.TimeRange(
        start_time=seconds_to_time(start_sec, rate),
        duration=seconds_to_time(dur_sec, rate),
    )


def discover_available_clips(extra_dirs=None):
    """
    Find all clips that exist — either on B2 (via upload tracker)
    or as local files on VMs (passed via extra_dirs).
    Returns dict: clip_id -> {"source": "b2"|"local", "duration": float, "path": str}
    """
    available = {}

    # 1. Clips already on B2 (from upload tracker)
    if os.path.exists(UPLOAD_TRACKER):
        tracker = json.load(open(UPLOAD_TRACKER))
        # Handle nested format: {"uploaded": {clip_id: info}, ...}
        uploaded = tracker.get("uploaded", tracker) if isinstance(tracker, dict) else {}
        if isinstance(uploaded, dict):
            for clip_id, info in uploaded.items():
                if isinstance(info, dict):
                    available[clip_id] = {
                        "source": "b2",
                        "url": f"{B2_BASE}/{clip_id}.mp4",
                        "duration": info.get("duration", 5.0),
                    }

    # 2. Local clip files (from VM downloads or local generation)
    local_dirs = [os.path.join(PRODUCTION, "local_clips")]
    if extra_dirs:
        local_dirs.extend(extra_dirs)

    for d in local_dirs:
        if not os.path.isdir(d):
            continue
        for mp4 in glob.glob(os.path.join(d, "*.mp4")):
            clip_id = Path(mp4).stem
            # Don't count sub-clips
            if "_sub" in clip_id:
                continue
            if clip_id not in available:  # B2 takes precedence
                available[clip_id] = {
                    "source": "local",
                    "path": mp4,
                    "duration": 5.0,  # default; could probe with ffprobe
                }

    return available


def load_all_clip_metadata():
    """
    Load metadata for all clips (original + fill).
    Returns dict: clip_id -> full metadata dict
    """
    metadata = {}

    if os.path.exists(ORIGINAL_PROMPTS):
        for c in json.load(open(ORIGINAL_PROMPTS)):
            metadata[c["clip_id"]] = c

    if os.path.exists(FILL_PROMPTS):
        for c in json.load(open(FILL_PROMPTS)):
            metadata[c["clip_id"]] = c

    return metadata


def build_timeline(extra_dirs=None):
    available = discover_available_clips(extra_dirs)
    all_meta = load_all_clip_metadata()

    timeline = otio.schema.Timeline(name="War Economy — The Real Cost")
    timeline.global_start_time = seconds_to_time(0)

    video_track = otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video)
    narr_track = otio.schema.Track(name="VO_Narration", kind=otio.schema.TrackKind.Audio)
    music_track = otio.schema.Track(name="MX_Music", kind=otio.schema.TrackKind.Audio)

    timeline_cursor = 0.0
    status_report = {
        "scenes": [],
        "total_narration_sec": 0,
        "total_video_available_sec": 0,
        "total_video_gap_sec": 0,
        "clips_available": 0,
        "clips_missing": 0,
        "clips_total": 0,
    }

    for scene_num in range(1, 27):
        # --- Narration ---
        narr_path = os.path.join(NARRATION, f"scene_{scene_num:02d}", "manifest.json")
        if not os.path.exists(narr_path):
            print(f"  SKIP scene {scene_num}: no narration manifest")
            continue

        narr = json.load(open(narr_path))
        narr_duration = narr["total_duration"]
        status_report["total_narration_sec"] += narr_duration

        # --- Collect all clips for this scene (original + fill), sorted by clip_index ---
        scene_clips_meta = sorted(
            [m for m in all_meta.values() if m["scene_number"] == scene_num],
            key=lambda c: c["clip_index"]
        )

        # Partition into available vs missing
        scene_available = []
        scene_missing = []
        for cm in scene_clips_meta:
            cid = cm["clip_id"]
            if cid in available:
                scene_available.append((cm, available[cid]))
            else:
                scene_missing.append(cm)

        video_dur = sum(
            avail.get("duration", cm.get("target_duration_sec", 5.0))
            for cm, avail in scene_available
        )
        gap = narr_duration - video_dur
        missing_dur = sum(m.get("target_duration_sec", 5.0) for m in scene_missing)

        scene_status = {
            "scene": scene_num,
            "title": narr.get("scene_title", scene_clips_meta[0]["scene_title"] if scene_clips_meta else ""),
            "narration_sec": round(narr_duration, 1),
            "video_available_sec": round(video_dur, 1),
            "gap_sec": round(max(0, gap), 1),
            "clips_available": len(scene_available),
            "clips_missing": len(scene_missing),
            "clips_total": len(scene_clips_meta),
            "missing_clip_ids": [m["clip_id"] for m in scene_missing],
        }
        status_report["scenes"].append(scene_status)
        status_report["total_video_available_sec"] += video_dur
        status_report["total_video_gap_sec"] += max(0, gap)
        status_report["clips_available"] += len(scene_available)
        status_report["clips_missing"] += len(scene_missing)
        status_report["clips_total"] += len(scene_clips_meta)

        tag = "OK" if gap <= 0.5 else f"GAP {gap:.1f}s"
        print(f"  Scene {scene_num:2d} [{narr_duration:6.1f}s narr] "
              f"[{video_dur:6.1f}s video, {len(scene_available):2d}/{len(scene_clips_meta):2d} clips] "
              f"— {tag}")

        # === VIDEO TRACK ===
        if scene_available:
            if video_dur >= narr_duration:
                # Enough video — play clips, trim to narration length
                cursor = 0.0
                for cm, avail in scene_available:
                    clip_dur = avail.get("duration", cm.get("target_duration_sec", 5.0))
                    remaining = narr_duration - cursor
                    if remaining <= 0:
                        break
                    actual_dur = min(clip_dur, remaining)

                    url = avail.get("url", avail.get("path", f"{B2_BASE}/{cm['clip_id']}.mp4"))
                    media_ref = otio.schema.ExternalReference(
                        target_url=url,
                        available_range=seconds_to_range(0, clip_dur),
                    )
                    clip = otio.schema.Clip(
                        name=cm["clip_id"],
                        media_reference=media_ref,
                        source_range=seconds_to_range(0, actual_dur),
                    )
                    clip.metadata["scene"] = scene_num
                    clip.metadata["prompt"] = cm.get("prompt", "")[:200]
                    clip.metadata["source"] = avail.get("source", "unknown")
                    video_track.append(clip)
                    cursor += actual_dur
            else:
                # Video shorter than narration — distribute clips evenly, gaps between
                gap_total = narr_duration - video_dur
                num_slots = len(scene_available) + 1  # gaps before first, between, after last
                gap_each = gap_total / num_slots

                for i, (cm, avail) in enumerate(scene_available):
                    # Gap before clip
                    if gap_each > 0.04:
                        g = otio.schema.Gap(
                            source_range=seconds_to_range(0, gap_each),
                            name=f"scene_{scene_num:02d}_gap_{i:02d}",
                        )
                        video_track.append(g)

                    clip_dur = avail.get("duration", cm.get("target_duration_sec", 5.0))
                    url = avail.get("url", avail.get("path", f"{B2_BASE}/{cm['clip_id']}.mp4"))
                    media_ref = otio.schema.ExternalReference(
                        target_url=url,
                        available_range=seconds_to_range(0, clip_dur),
                    )
                    clip = otio.schema.Clip(
                        name=cm["clip_id"],
                        media_reference=media_ref,
                        source_range=seconds_to_range(0, clip_dur),
                    )
                    clip.metadata["scene"] = scene_num
                    clip.metadata["prompt"] = cm.get("prompt", "")[:200]
                    clip.metadata["source"] = avail.get("source", "unknown")
                    video_track.append(clip)

                # Trailing gap
                if gap_each > 0.04:
                    g = otio.schema.Gap(
                        source_range=seconds_to_range(0, gap_each),
                        name=f"scene_{scene_num:02d}_gap_trail",
                    )
                    video_track.append(g)
        else:
            # No video at all — full gap
            video_track.append(otio.schema.Gap(
                source_range=seconds_to_range(0, narr_duration),
                name=f"scene_{scene_num:02d}_no_video",
            ))

        # === NARRATION TRACK ===
        for seg in narr["segments"]:
            if seg.get("status") != "complete":
                continue
            seg_dur = seg["duration_sec"]
            audio_ref = otio.schema.ExternalReference(
                target_url=os.path.join(NARRATION, f"scene_{scene_num:02d}", seg["file"]),
                available_range=seconds_to_range(0, seg_dur, 24000),
            )
            narr_clip = otio.schema.Clip(
                name=f"scene_{scene_num:02d}_{seg['file']}",
                media_reference=audio_ref,
                source_range=seconds_to_range(0, seg_dur, 24000),
            )
            narr_clip.metadata["voice"] = seg.get("voice", "")
            narr_clip.metadata["scene"] = scene_num
            narr_track.append(narr_clip)

        # === MUSIC TRACK (placeholder) ===
        music_track.append(otio.schema.Gap(
            source_range=seconds_to_range(0, narr_duration),
            name=f"scene_{scene_num:02d}_music",
        ))

        timeline_cursor += narr_duration

        # Inter-scene gap
        if scene_num < 26:
            for track in [video_track, narr_track, music_track]:
                track.append(otio.schema.Gap(
                    source_range=seconds_to_range(0, INTER_SCENE_GAP),
                    name=f"gap_after_scene_{scene_num:02d}",
                ))
            timeline_cursor += INTER_SCENE_GAP

    # Assemble
    timeline.tracks.append(video_track)
    timeline.tracks.append(narr_track)
    timeline.tracks.append(music_track)

    timeline.metadata["total_duration_sec"] = round(timeline_cursor, 1)
    timeline.metadata["total_duration_min"] = round(timeline_cursor / 60, 1)
    timeline.metadata["clips_available"] = status_report["clips_available"]
    timeline.metadata["clips_missing"] = status_report["clips_missing"]
    timeline.metadata["clips_total"] = status_report["clips_total"]
    timeline.metadata["model"] = "LTX-2.3-22B bf16"
    timeline.metadata["tts"] = "Qwen3-TTS VoiceDesign"
    timeline.metadata["resolution"] = "768x512"
    timeline.metadata["fps"] = 24

    # Write OTIO
    otio.adapters.write_to_file(timeline, OUTPUT_OTIO)

    # Write status report
    status_report["total_narration_sec"] = round(status_report["total_narration_sec"], 1)
    status_report["total_video_available_sec"] = round(status_report["total_video_available_sec"], 1)
    status_report["total_video_gap_sec"] = round(status_report["total_video_gap_sec"], 1)
    status_report["timeline_duration_sec"] = round(timeline_cursor, 1)
    status_report["timeline_duration_min"] = round(timeline_cursor / 60, 1)
    status_report["completion_pct"] = round(
        status_report["clips_available"] / max(1, status_report["clips_total"]) * 100, 1
    )

    with open(OUTPUT_STATUS, "w") as f:
        json.dump(status_report, f, indent=2)

    # Summary
    print(f"\n{'='*60}")
    print(f"OTIO: {OUTPUT_OTIO}")
    print(f"Status: {OUTPUT_STATUS}")
    print(f"Timeline: {timeline_cursor:.1f}s = {timeline_cursor/60:.1f} min")
    print(f"Clips: {status_report['clips_available']}/{status_report['clips_total']} "
          f"({status_report['completion_pct']:.0f}%)")
    print(f"Video: {status_report['total_video_available_sec']:.1f}s available, "
          f"{status_report['total_video_gap_sec']:.1f}s gap remaining")
    print(f"Narration: {status_report['total_narration_sec']:.1f}s")
    print(f"{'='*60}")

    return timeline, status_report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--clip-dir", nargs="*", help="Extra directories with local .mp4 clips")
    args = parser.parse_args()
    build_timeline(extra_dirs=args.clip_dir)
