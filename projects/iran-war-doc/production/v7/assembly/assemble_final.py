#!/usr/bin/env python3
"""
WAR ECONOMY — Final Video Assembly
====================================
Downloads all clips from B2, assembles per-scene with narration,
then concatenates all scenes into the final ~76 minute documentary.

Usage:
  python3 assemble_final.py
  python3 assemble_final.py --scene 1       # assemble only scene 1
  python3 assemble_final.py --skip-download  # skip B2 download (clips already local)
"""

import json
import os
import subprocess
import sys
import time
import glob
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIG
# ============================================================
WORK_DIR = "/workspace/assembly"
CLIPS_DIR = os.path.join(WORK_DIR, "clips")
NARRATION_DIR = os.path.join(WORK_DIR, "narration")
SCENES_DIR = os.path.join(WORK_DIR, "scenes")
MANIFEST = os.path.join(WORK_DIR, "assembly_manifest.json")
FINAL_OUTPUT = os.path.join(WORK_DIR, "war_economy_final.mp4")

B2_BUCKET = "economy-vid-assets"
B2_PREFIX = "v7_war_economy"
B2_KEY_ID = "B2_KEY_ID"
B2_APP_KEY = "B2_APP_KEY"
B2_BASE_URL = "https://f004.backblazeb2.com/file/economy-vid-assets/v7_war_economy"

INTER_SCENE_GAP = 1.0  # seconds of black between scenes
CROSSFADE_DUR = 0.3     # crossfade between clips within a scene
FPS = 24
WIDTH = 768
HEIGHT = 512


def run_cmd(cmd, timeout=300, desc=""):
    """Run a shell command, return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0 and desc:
            print(f"  WARN [{desc}]: {result.stderr[:200]}")
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT [{desc}]")
        return False, "", "timeout"
    except Exception as e:
        print(f"  ERROR [{desc}]: {e}")
        return False, "", str(e)


def probe_duration(path):
    """Get video/audio duration via ffprobe."""
    cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{path}"'
    ok, out, _ = run_cmd(cmd, timeout=10, desc=f"probe {os.path.basename(path)}")
    if ok and out.strip():
        try:
            return float(out.strip())
        except ValueError:
            pass
    return 5.0  # fallback


def authorize_b2():
    """Authorize B2 CLI."""
    ok, _, _ = run_cmd(f"b2 authorize-account {B2_KEY_ID} {B2_APP_KEY}", timeout=30, desc="b2 auth")
    return ok


def download_clip(clip_id):
    """Download a single clip from B2. Returns (clip_id, success, path)."""
    local_path = os.path.join(CLIPS_DIR, f"{clip_id}.mp4")
    if os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
        return clip_id, True, local_path

    url = f"{B2_BASE_URL}/{clip_id}.mp4"
    cmd = f'curl -sL -o "{local_path}" "{url}"'
    ok, _, _ = run_cmd(cmd, timeout=60, desc=f"dl {clip_id}")
    if ok and os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
        return clip_id, True, local_path
    return clip_id, False, local_path


def download_all_clips(assembly):
    """Download all clips from B2 in parallel."""
    os.makedirs(CLIPS_DIR, exist_ok=True)

    all_clip_ids = []
    for scene in assembly["scenes"]:
        for clip in scene["clips"]:
            all_clip_ids.append(clip["clip_id"])

    print(f"\nDownloading {len(all_clip_ids)} clips from B2...")
    done = 0
    failed = []

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(download_clip, cid): cid for cid in all_clip_ids}
        for future in as_completed(futures):
            cid, ok, path = future.result()
            done += 1
            if not ok:
                failed.append(cid)
            if done % 50 == 0 or done == len(all_clip_ids):
                print(f"  [{done}/{len(all_clip_ids)}] downloaded ({len(failed)} failed)")

    if failed:
        print(f"  WARNING: {len(failed)} clips failed to download: {failed[:10]}...")
    else:
        print(f"  All {len(all_clip_ids)} clips downloaded successfully")

    return failed


def assemble_scene(scene_data):
    """
    Assemble a single scene: video clips + narration audio.
    
    Strategy:
    - Clips play in sequence to fill the narration duration
    - If total clip duration < narration duration, distribute black gaps between clips
    - If total clip duration > narration duration, trim last clip
    - Narration segments are concatenated and overlaid as audio
    """
    scene_num = scene_data["scene_number"]
    scene_id = f"scene_{scene_num:02d}"
    scene_dir = os.path.join(SCENES_DIR, scene_id)
    os.makedirs(scene_dir, exist_ok=True)

    narr_dur = scene_data["narration_duration"]
    clips_meta = scene_data["clips"]
    narr_segs = scene_data["narration_segments"]

    print(f"\n{'='*60}")
    print(f"Scene {scene_num}: {scene_data.get('scene_title', '')} ({narr_dur:.1f}s)")
    print(f"  {len(clips_meta)} clips, {len(narr_segs)} narration segments")

    output_path = os.path.join(SCENES_DIR, f"{scene_id}.mp4")
    if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
        actual_dur = probe_duration(output_path)
        if abs(actual_dur - narr_dur) < 2.0:
            print(f"  Already assembled ({actual_dur:.1f}s), skipping")
            return output_path

    # ---- Step 1: Probe actual clip durations ----
    clip_paths = []
    clip_durs = []
    for cm in clips_meta:
        cpath = os.path.join(CLIPS_DIR, f"{cm['clip_id']}.mp4")
        if os.path.exists(cpath):
            dur = probe_duration(cpath)
            clip_paths.append(cpath)
            clip_durs.append(dur)
        else:
            print(f"  MISSING: {cm['clip_id']}")

    total_video = sum(clip_durs)
    print(f"  Video: {total_video:.1f}s from {len(clip_paths)} clips, need {narr_dur:.1f}s")

    # ---- Step 2: Build video track ----
    # Strategy: play all clips in order. If shorter than narration, add black gaps.
    # If longer, trim the end.

    if len(clip_paths) == 0:
        # No clips — generate black video
        black_path = os.path.join(scene_dir, "black.mp4")
        run_cmd(
            f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={narr_dur}:r={FPS}" '
            f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{black_path}"',
            timeout=120, desc=f"black {scene_id}"
        )
        video_path = black_path
    else:
        # Normalize all clips to same resolution/fps and concat
        normalized = []
        for i, (cpath, cdur) in enumerate(zip(clip_paths, clip_durs)):
            norm_path = os.path.join(scene_dir, f"clip_{i:03d}.mp4")
            # Scale to target resolution, set fps, add black padding if needed
            run_cmd(
                f'ffmpeg -y -i "{cpath}" '
                f'-vf "scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
                f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,'
                f'fps={FPS},setsar=1" '
                f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -an '
                f'"{norm_path}"',
                timeout=60, desc=f"norm {scene_id} clip {i}"
            )
            if os.path.exists(norm_path) and os.path.getsize(norm_path) > 1000:
                normalized.append(norm_path)

        if total_video < narr_dur and len(normalized) > 0:
            # Need to add black gaps between clips to fill narration duration
            gap_total = narr_dur - total_video
            num_gaps = len(normalized) + 1  # before first, between, after last
            gap_each = gap_total / num_gaps

            # Create black gap clip
            gap_path = os.path.join(scene_dir, "gap.mp4")
            if gap_each > 0.04:
                run_cmd(
                    f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={gap_each}:r={FPS}" '
                    f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{gap_path}"',
                    timeout=30, desc=f"gap {scene_id}"
                )

            # Build concat list with gaps
            concat_list = os.path.join(scene_dir, "concat.txt")
            with open(concat_list, "w") as f:
                for i, npath in enumerate(normalized):
                    if gap_each > 0.04 and os.path.exists(gap_path):
                        f.write(f"file '{gap_path}'\n")
                    f.write(f"file '{npath}'\n")
                # Trailing gap
                if gap_each > 0.04 and os.path.exists(gap_path):
                    f.write(f"file '{gap_path}'\n")
        else:
            # Enough video — just concat all clips
            concat_list = os.path.join(scene_dir, "concat.txt")
            with open(concat_list, "w") as f:
                for npath in normalized:
                    f.write(f"file '{npath}'\n")

        # Concat all video
        raw_video = os.path.join(scene_dir, "video_raw.mp4")
        run_cmd(
            f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
            f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{raw_video}"',
            timeout=300, desc=f"concat {scene_id}"
        )

        # Trim to exact narration duration
        video_path = os.path.join(scene_dir, "video.mp4")
        run_cmd(
            f'ffmpeg -y -i "{raw_video}" -t {narr_dur} '
            f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{video_path}"',
            timeout=120, desc=f"trim {scene_id}"
        )

        if not os.path.exists(video_path) or os.path.getsize(video_path) < 1000:
            video_path = raw_video

    # ---- Step 3: Build narration audio track ----
    narr_dir = os.path.join(NARRATION_DIR, scene_id)
    narr_files = []
    for seg in narr_segs:
        seg_path = os.path.join(narr_dir, seg["file"])
        if os.path.exists(seg_path):
            narr_files.append(seg_path)

    if narr_files:
        if len(narr_files) == 1:
            narr_audio = narr_files[0]
        else:
            # Concat narration segments
            narr_concat_list = os.path.join(scene_dir, "narr_concat.txt")
            with open(narr_concat_list, "w") as f:
                for nf in narr_files:
                    f.write(f"file '{nf}'\n")
            narr_audio = os.path.join(scene_dir, "narration.wav")
            run_cmd(
                f'ffmpeg -y -f concat -safe 0 -i "{narr_concat_list}" '
                f'-c:a pcm_s16le "{narr_audio}"',
                timeout=120, desc=f"narr concat {scene_id}"
            )
    else:
        narr_audio = None

    # ---- Step 4: Mux video + narration ----
    if narr_audio and os.path.exists(video_path):
        run_cmd(
            f'ffmpeg -y -i "{video_path}" -i "{narr_audio}" '
            f'-map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k '
            f'-shortest "{output_path}"',
            timeout=120, desc=f"mux {scene_id}"
        )
    elif os.path.exists(video_path):
        shutil.copy2(video_path, output_path)

    if os.path.exists(output_path):
        final_dur = probe_duration(output_path)
        size_mb = os.path.getsize(output_path) / 1e6
        print(f"  ✓ {scene_id}: {final_dur:.1f}s, {size_mb:.1f} MB")

        # Cleanup intermediate files to save disk
        for f in glob.glob(os.path.join(scene_dir, "clip_*.mp4")):
            os.remove(f)
        for f in ["video_raw.mp4", "video.mp4", "gap.mp4", "narration.wav"]:
            fp = os.path.join(scene_dir, f)
            if os.path.exists(fp):
                os.remove(fp)
    else:
        print(f"  ✗ {scene_id}: assembly FAILED")

    return output_path


def concat_all_scenes(scene_paths):
    """Concatenate all scene videos into the final documentary."""
    print(f"\n{'='*60}")
    print(f"Concatenating {len(scene_paths)} scenes into final video...")

    # Create inter-scene black gap
    gap_path = os.path.join(SCENES_DIR, "scene_gap.mp4")
    run_cmd(
        f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={INTER_SCENE_GAP}:r={FPS}" '
        f'-f lavfi -i "anullsrc=r=44100:cl=stereo" '
        f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 192k '
        f'-shortest "{gap_path}"',
        timeout=30, desc="scene gap"
    )

    # Build final concat list
    concat_list = os.path.join(SCENES_DIR, "final_concat.txt")
    valid_scenes = []
    with open(concat_list, "w") as f:
        for i, sp in enumerate(scene_paths):
            if os.path.exists(sp) and os.path.getsize(sp) > 1000:
                # Re-encode to ensure uniform format for concat
                uniform_path = sp.replace(".mp4", "_uniform.mp4")
                run_cmd(
                    f'ffmpeg -y -i "{sp}" '
                    f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p '
                    f'-c:a aac -b:a 192k -ar 44100 -ac 2 '
                    f'"{uniform_path}"',
                    timeout=300, desc=f"uniform scene {i+1}"
                )
                target = uniform_path if os.path.exists(uniform_path) else sp
                f.write(f"file '{target}'\n")
                valid_scenes.append(target)
                # Add gap between scenes (not after last)
                if i < len(scene_paths) - 1 and os.path.exists(gap_path):
                    f.write(f"file '{gap_path}'\n")

    print(f"  Concatenating {len(valid_scenes)} valid scenes...")

    # Final concat
    run_cmd(
        f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
        f'-c copy "{FINAL_OUTPUT}"',
        timeout=600, desc="final concat"
    )

    if os.path.exists(FINAL_OUTPUT):
        dur = probe_duration(FINAL_OUTPUT)
        size_mb = os.path.getsize(FINAL_OUTPUT) / 1e6
        print(f"\n✓ FINAL VIDEO: {FINAL_OUTPUT}")
        print(f"  Duration: {dur:.1f}s = {dur/60:.1f} min")
        print(f"  Size: {size_mb:.1f} MB")
        return True
    else:
        print("✗ Final concat FAILED")
        return False


def upload_to_b2():
    """Upload the final video to B2."""
    if not os.path.exists(FINAL_OUTPUT):
        print("No final video to upload")
        return False

    print(f"\nUploading to B2...")
    authorize_b2()

    # Embed metadata
    meta_path = FINAL_OUTPUT.replace(".mp4", "_meta.mp4")
    run_cmd(
        f'ffmpeg -y -i "{FINAL_OUTPUT}" '
        f'-metadata title="War Economy — The Real Cost" '
        f'-metadata comment="Full documentary | LTX-2.3 | 768x512 | 26 scenes | 435 clips | Qwen3-TTS narration" '
        f'-metadata artist="War Economy Documentary Pipeline" '
        f'-metadata date="2026" '
        f'-c copy "{meta_path}"',
        timeout=120, desc="embed metadata"
    )

    upload_src = meta_path if os.path.exists(meta_path) else FINAL_OUTPUT
    remote_path = f"{B2_PREFIX}/FINAL_war_economy.mp4"

    ok, out, err = run_cmd(
        f'b2 upload-file {B2_BUCKET} "{upload_src}" "{remote_path}"',
        timeout=600, desc="b2 upload final"
    )

    if ok:
        print(f"✓ Uploaded to B2: {remote_path}")
    else:
        print(f"✗ Upload failed: {err[:200]}")

    # Cleanup
    if os.path.exists(meta_path):
        os.remove(meta_path)

    return ok


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", type=int, help="Assemble only this scene number")
    parser.add_argument("--skip-download", action="store_true", help="Skip B2 download")
    parser.add_argument("--skip-upload", action="store_true", help="Skip B2 upload at end")
    args = parser.parse_args()

    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(CLIPS_DIR, exist_ok=True)
    os.makedirs(SCENES_DIR, exist_ok=True)

    # Load manifest
    with open(MANIFEST) as f:
        assembly = json.load(f)

    print(f"WAR ECONOMY — Final Assembly")
    print(f"{'='*60}")
    print(f"Scenes: {assembly['total_scenes']}")
    print(f"Clips: {assembly['total_clips']}")
    print(f"Narration: {assembly['total_narration_sec']/60:.1f} min")

    # Download clips
    if not args.skip_download:
        authorize_b2()
        failed = download_all_clips(assembly)
        if len(failed) > 10:
            print(f"Too many download failures ({len(failed)}), aborting")
            sys.exit(1)

    # Assemble scenes
    scene_paths = []
    scenes_to_build = assembly["scenes"]
    if args.scene:
        scenes_to_build = [s for s in scenes_to_build if s["scene_number"] == args.scene]

    for scene_data in scenes_to_build:
        path = assemble_scene(scene_data)
        scene_paths.append(path)

    if args.scene:
        print(f"\nScene {args.scene} assembled: {scene_paths[0]}")
        return

    # Concat all scenes
    success = concat_all_scenes(scene_paths)

    # Upload
    if success and not args.skip_upload:
        upload_to_b2()

    print(f"\n{'='*60}")
    print(f"Assembly complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
