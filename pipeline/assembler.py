#!/usr/bin/env python3
"""
OTIO Assembler — Final Video Assembly from Timeline
=====================================================
Reads the completed OTIO timeline and renders the final documentary.
The OTIO timeline is the SINGLE SOURCE OF TRUTH — the assembler just executes it.

Process:
  1. Read OTIO timeline (all tracks, all clips with timing)
  2. For each scene: download/locate video clips and narration audio
  3. Assemble per-scene: concatenate video clips trimmed to OTIO source_range,
     overlay narration audio at precise timing
  4. Concatenate all scenes with inter-scene gaps
  5. Export final MP4 + production metadata
  6. Optionally export OTIO to FCPXML/EDL for NLE import

Key constraints:
  - WAV narration is sacred — never re-encode audio unnecessarily
  - Use MPEG-TS intermediate for final concat (avoids timestamp corruption)
  - No looping, no stretching — only trim (which OTIO already handles via source_range)
  - All timing comes from OTIO — the assembler doesn't make timing decisions

Usage:
  python3 assembler.py --otio war_economy_v8.otio \\
                       --output-dir ./final \\
                       --clips-dir ./clips \\
                       --audio-dir ./audio
"""

import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import opentimelineio as otio

from pipeline.otio_timeline import OTIOTimeline, range_duration_sec, rt_to_seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# Assembly constants
FPS = 24
WIDTH = 768
HEIGHT = 512
INTER_SCENE_GAP = 1.0
B2_BASE_URL = "https://f004.backblazeb2.com/file/economy-vid-assets"


def run_cmd(cmd, timeout=300, desc=""):
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0 and desc:
            log.warning(f"  [{desc}]: {result.stderr[:200]}")
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        log.warning(f"  TIMEOUT [{desc}]")
        return False, "", "timeout"
    except Exception as e:
        log.error(f"  ERROR [{desc}]: {e}")
        return False, "", str(e)


def probe_duration(path):
    """Get media duration via ffprobe."""
    cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{path}"'
    ok, out, _ = run_cmd(cmd, timeout=10, desc=f"probe {os.path.basename(path)}")
    if ok and out.strip():
        try:
            return float(out.strip())
        except ValueError:
            pass
    return 0.0


class Assembler:
    """
    Reads the OTIO timeline and renders the final documentary video.

    The assembler is a pure executor — it reads timing from OTIO and renders.
    All creative/timing decisions are already baked into the OTIO timeline.
    """

    def __init__(self, otio_path, output_dir, clips_dir=None, audio_dir=None):
        """
        Args:
            otio_path: path to .otio timeline
            output_dir: directory for final output
            clips_dir: directory where video clips are stored (local)
            audio_dir: directory where narration audio is stored
        """
        self.otio_path = str(otio_path)
        self.output_dir = str(output_dir)
        self.clips_dir = str(clips_dir) if clips_dir else os.path.join(output_dir, "clips")
        self.audio_dir = str(audio_dir) if audio_dir else os.path.join(output_dir, "audio")

        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.clips_dir, exist_ok=True)

        self.otio_tl = OTIOTimeline(self.otio_path)
        self.otio_tl.load()

    def _resolve_media_path(self, target_url, media_type="video"):
        """
        Resolve an OTIO media reference URL to a local file path.
        Handles local paths, B2 URLs, and relative paths.
        """
        if os.path.exists(target_url):
            return target_url

        # Check in clips/audio dir
        basename = os.path.basename(target_url)
        if media_type == "video":
            local = os.path.join(self.clips_dir, basename)
        else:
            local = os.path.join(self.audio_dir, basename)

        if os.path.exists(local):
            return local

        # For audio, check in scene subdirectories
        if media_type == "audio":
            for dirpath, _, filenames in os.walk(self.audio_dir):
                if basename in filenames:
                    return os.path.join(dirpath, basename)

        return target_url  # Return as-is, caller will handle missing

    def _download_clip(self, url, local_path):
        """Download a clip from URL (B2) if not already local."""
        if os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
            return True

        cmd = f'curl -sL -o "{local_path}" "{url}"'
        ok, _, _ = run_cmd(cmd, timeout=60, desc=f"download {os.path.basename(local_path)}")
        return ok and os.path.exists(local_path) and os.path.getsize(local_path) > 1000

    def assemble_scene(self, scene_num):
        """
        Assemble a single scene from the OTIO timeline.

        Reads video clips and narration from OTIO, renders a per-scene MP4.
        """
        scenes_dir = os.path.join(self.output_dir, "scenes")
        os.makedirs(scenes_dir, exist_ok=True)
        scene_work = os.path.join(scenes_dir, f"scene_{scene_num:02d}")
        os.makedirs(scene_work, exist_ok=True)

        output_path = os.path.join(scenes_dir, f"scene_{scene_num:02d}.mp4")

        # Check if already assembled
        if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
            log.info(f"  Scene {scene_num} — already assembled, skipping")
            return output_path

        # Get narration duration from OTIO
        narr_dur = self.otio_tl.get_scene_total_duration(scene_num)
        if narr_dur <= 0:
            log.warning(f"  Scene {scene_num} — no narration duration, skipping")
            return None

        log.info(f"\n  Scene {scene_num} — narration: {narr_dur:.1f}s")

        # ---- Collect video clips from OTIO video track ----
        video_items = []
        for item in self.otio_tl.video_track:
            if hasattr(item, "metadata") and item.metadata.get("scene") == scene_num:
                if isinstance(item, otio.schema.Clip):
                    dur = range_duration_sec(item.source_range)
                    url = item.media_reference.target_url if item.media_reference else ""
                    video_items.append({
                        "name": item.name,
                        "url": url,
                        "duration": dur,
                        "available_duration": range_duration_sec(item.media_reference.available_range)
                                              if item.media_reference and hasattr(item.media_reference, "available_range")
                                                 and item.media_reference.available_range else dur,
                    })
                elif isinstance(item, otio.schema.Gap):
                    dur = range_duration_sec(item.source_range)
                    video_items.append({"name": item.name, "type": "gap", "duration": dur})

        # ---- Build video track ----
        normalized = []
        for i, vi in enumerate(video_items):
            if vi.get("type") == "gap":
                # Create black gap
                gap_path = os.path.join(scene_work, f"gap_{i:03d}.mp4")
                run_cmd(
                    f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={vi["duration"]}:r={FPS}" '
                    f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{gap_path}"',
                    timeout=30, desc=f"gap scene {scene_num}",
                )
                if os.path.exists(gap_path):
                    normalized.append(gap_path)
                continue

            # Resolve and normalize video clip
            local_path = self._resolve_media_path(vi["url"], "video")
            if not os.path.exists(local_path):
                # Try downloading from B2
                dl_path = os.path.join(self.clips_dir, f"{vi['name']}.mp4")
                if vi["url"].startswith("http"):
                    self._download_clip(vi["url"], dl_path)
                    local_path = dl_path

            if not os.path.exists(local_path):
                log.warning(f"    MISSING: {vi['name']}")
                # Insert black gap instead
                gap_path = os.path.join(scene_work, f"missing_{i:03d}.mp4")
                run_cmd(
                    f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={vi["duration"]}:r={FPS}" '
                    f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{gap_path}"',
                    timeout=30, desc=f"missing {vi['name']}",
                )
                if os.path.exists(gap_path):
                    normalized.append(gap_path)
                continue

            # Normalize clip to standard format and trim to OTIO source_range duration
            norm_path = os.path.join(scene_work, f"clip_{i:03d}.mp4")
            run_cmd(
                f'ffmpeg -y -i "{local_path}" -t {vi["duration"]} '
                f'-vf "scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
                f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,'
                f'fps={FPS},setsar=1" '
                f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -an '
                f'"{norm_path}"',
                timeout=60, desc=f"norm {vi['name']}",
            )
            if os.path.exists(norm_path) and os.path.getsize(norm_path) > 1000:
                normalized.append(norm_path)

        if not normalized:
            # No video at all — black
            black_path = os.path.join(scene_work, "black.mp4")
            run_cmd(
                f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={narr_dur}:r={FPS}" '
                f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{black_path}"',
                timeout=120, desc=f"black scene {scene_num}",
            )
            video_path = black_path
        else:
            # Concat all video segments
            concat_list = os.path.join(scene_work, "video_concat.txt")
            with open(concat_list, "w") as f:
                for np in normalized:
                    f.write(f"file '{np}'\n")

            raw_video = os.path.join(scene_work, "video_raw.mp4")
            run_cmd(
                f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
                f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{raw_video}"',
                timeout=300, desc=f"concat scene {scene_num}",
            )

            # Trim to exact narration duration
            video_path = os.path.join(scene_work, "video.mp4")
            run_cmd(
                f'ffmpeg -y -i "{raw_video}" -t {narr_dur} '
                f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{video_path}"',
                timeout=120, desc=f"trim scene {scene_num}",
            )
            if not os.path.exists(video_path):
                video_path = raw_video

        # ---- Build narration audio track ----
        audio_segments = self.otio_tl.get_scene_audio_segments(scene_num)
        narr_files = []
        for seg in audio_segments:
            audio_path = self._resolve_media_path(seg["audio_path"], "audio")
            if os.path.exists(audio_path):
                narr_files.append(audio_path)

        narr_audio = None
        if narr_files:
            if len(narr_files) == 1:
                narr_audio = narr_files[0]
            else:
                narr_concat_list = os.path.join(scene_work, "narr_concat.txt")
                with open(narr_concat_list, "w") as f:
                    for nf in narr_files:
                        f.write(f"file '{nf}'\n")
                narr_audio = os.path.join(scene_work, "narration.wav")
                run_cmd(
                    f'ffmpeg -y -f concat -safe 0 -i "{narr_concat_list}" '
                    f'-c:a pcm_s16le "{narr_audio}"',
                    timeout=120, desc=f"narr concat scene {scene_num}",
                )

        # ---- Mux video + narration ----
        if narr_audio and os.path.exists(narr_audio) and os.path.exists(video_path):
            run_cmd(
                f'ffmpeg -y -i "{video_path}" -i "{narr_audio}" '
                f'-map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k '
                f'-shortest "{output_path}"',
                timeout=120, desc=f"mux scene {scene_num}",
            )
        elif os.path.exists(video_path):
            shutil.copy2(video_path, output_path)

        if os.path.exists(output_path):
            final_dur = probe_duration(output_path)
            size_mb = os.path.getsize(output_path) / 1e6
            log.info(f"  Scene {scene_num}: {final_dur:.1f}s, {size_mb:.1f} MB")

            # Cleanup intermediate files
            for f in os.listdir(scene_work):
                fp = os.path.join(scene_work, f)
                if os.path.isfile(fp):
                    os.remove(fp)
        else:
            log.error(f"  Scene {scene_num}: assembly FAILED")

        return output_path

    def concat_all_scenes(self, scene_paths):
        """
        Concatenate all scene videos into the final documentary.
        Uses MPEG-TS intermediate to avoid timestamp corruption.
        """
        log.info(f"\n{'='*60}")
        log.info(f"Concatenating {len(scene_paths)} scenes into final video...")

        # Create inter-scene black gap with silent audio
        gap_path = os.path.join(self.output_dir, "scene_gap.mp4")
        run_cmd(
            f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={INTER_SCENE_GAP}:r={FPS}" '
            f'-f lavfi -i "anullsrc=r=44100:cl=stereo" '
            f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 192k '
            f'-shortest "{gap_path}"',
            timeout=30, desc="scene gap",
        )

        # Uniform encoding for reliable concat
        valid_scenes = []
        for i, sp in enumerate(scene_paths):
            if not sp or not os.path.exists(sp) or os.path.getsize(sp) < 1000:
                continue
            uniform_path = sp.replace(".mp4", "_uniform.mp4")
            run_cmd(
                f'ffmpeg -y -i "{sp}" '
                f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p '
                f'-c:a aac -b:a 192k -ar 44100 -ac 2 '
                f'"{uniform_path}"',
                timeout=300, desc=f"uniform scene {i + 1}",
            )
            target = uniform_path if os.path.exists(uniform_path) else sp
            valid_scenes.append(target)

        if not valid_scenes:
            log.error("No valid scenes to concatenate!")
            return None

        # Build concat list with gaps
        concat_list = os.path.join(self.output_dir, "final_concat.txt")
        with open(concat_list, "w") as f:
            for i, vs in enumerate(valid_scenes):
                f.write(f"file '{vs}'\n")
                if i < len(valid_scenes) - 1 and os.path.exists(gap_path):
                    f.write(f"file '{gap_path}'\n")

        # Final concat
        final_path = os.path.join(self.output_dir, "THE_WAR_ECONOMY_v8_final.mp4")
        run_cmd(
            f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
            f'-c copy "{final_path}"',
            timeout=600, desc="final concat",
        )

        if os.path.exists(final_path):
            dur = probe_duration(final_path)
            size_mb = os.path.getsize(final_path) / 1e6
            log.info(f"\nFINAL VIDEO: {final_path}")
            log.info(f"  Duration: {dur:.1f}s = {dur / 60:.1f} min")
            log.info(f"  Size: {size_mb:.1f} MB")

            # Embed production metadata
            meta_path = final_path.replace(".mp4", "_meta.mp4")
            status = self.otio_tl.get_timeline_status()
            run_cmd(
                f'ffmpeg -y -i "{final_path}" '
                f'-metadata title="War Economy — The Real Cost (V8 OTIO)" '
                f'-metadata comment="LTX-2.3 bf16 | {WIDTH}x{HEIGHT} | '
                f'{status["video_clips_count"]} clips | Qwen3-TTS narration | OTIO-centric pipeline" '
                f'-metadata artist="War Economy Documentary Pipeline V8" '
                f'-c copy "{meta_path}"',
                timeout=120, desc="embed metadata",
            )
            if os.path.exists(meta_path):
                os.replace(meta_path, final_path)

            return final_path
        else:
            log.error("Final concatenation FAILED")
            return None

    def assemble_all(self, download_clips=False, upload_final=False,
                     b2_key_id=None, b2_app_key=None):
        """
        Full assembly pipeline: scenes → concat → final.

        Reads everything from the OTIO timeline.
        """
        log.info(f"\n{'='*60}")
        log.info(f"OTIO ASSEMBLER — Reading timeline and rendering")
        log.info(f"{'='*60}")

        status = self.otio_tl.get_timeline_status()
        log.info(f"Timeline: {status['name']}")
        log.info(f"Duration: {status['total_duration_min']} min")
        log.info(f"Video clips: {status['video_clips_count']}")
        log.info(f"Narration segments: {status['narration_clips_count']}")
        log.info(f"Completion: {status['completion_pct']}%")

        # Determine which scenes exist
        scene_nums = sorted(status["scenes"].keys())
        if not scene_nums:
            log.error("No scenes found in OTIO timeline!")
            return None

        # Assemble each scene
        scene_paths = []
        for scene_num in scene_nums:
            path = self.assemble_scene(scene_num)
            scene_paths.append(path)

        # Concatenate all scenes
        final_path = self.concat_all_scenes(scene_paths)

        # Export OTIO to additional formats
        if final_path:
            # Export FCPXML for NLE import
            fcpxml_path = os.path.join(self.output_dir, "war_economy_v8.fcpxml")
            try:
                self.otio_tl.export_fcpxml(fcpxml_path)
                log.info(f"Exported FCPXML: {fcpxml_path}")
            except Exception as e:
                log.warning(f"FCPXML export failed: {e}")

            # Export assembly JSON
            assembly_json = os.path.join(self.output_dir, "assembly_manifest.json")
            self.otio_tl.to_assembly_json(assembly_json)
            log.info(f"Exported assembly JSON: {assembly_json}")

            # Upload to B2
            if upload_final and b2_key_id and b2_app_key:
                self._upload_to_b2(final_path, b2_key_id, b2_app_key)

        log.info(f"\n{'='*60}")
        log.info(f"ASSEMBLY COMPLETE")
        log.info(f"{'='*60}")

        return final_path

    def _upload_to_b2(self, final_path, b2_key_id, b2_app_key):
        """Upload the final video to B2."""
        log.info("Uploading to B2...")
        run_cmd(f"b2 authorize-account {b2_key_id} {b2_app_key}", timeout=30, desc="b2 auth")

        remote_path = "v8_war_economy/THE_WAR_ECONOMY_v8_final.mp4"
        ok, _, _ = run_cmd(
            f'b2 upload-file economy-vid-assets "{final_path}" "{remote_path}"',
            timeout=600, desc="b2 upload final",
        )
        if ok:
            log.info(f"Uploaded to B2: {remote_path}")
        else:
            log.warning("B2 upload failed")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="OTIO-driven video assembler")
    parser.add_argument("--otio", required=True, help="Path to .otio timeline")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--clips-dir", default=None, help="Video clips directory")
    parser.add_argument("--audio-dir", default=None, help="Narration audio directory")
    parser.add_argument("--scene", type=int, default=None, help="Assemble single scene")
    parser.add_argument("--upload", action="store_true", help="Upload to B2")
    parser.add_argument("--b2-key-id", default=None)
    parser.add_argument("--b2-app-key", default=None)
    args = parser.parse_args()

    asm = Assembler(
        otio_path=args.otio,
        output_dir=args.output_dir,
        clips_dir=args.clips_dir,
        audio_dir=args.audio_dir,
    )

    if args.scene:
        asm.assemble_scene(args.scene)
    else:
        asm.assemble_all(
            upload_final=args.upload,
            b2_key_id=args.b2_key_id,
            b2_app_key=args.b2_app_key,
        )


if __name__ == "__main__":
    main()
