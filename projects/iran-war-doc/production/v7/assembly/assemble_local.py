#!/usr/bin/env python3
"""
Local assembly — downloads clips scene-by-scene, assembles each scene,
then concatenates all scenes into the final video. Cleans up aggressively.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORK_DIR = "/home/user/workspace/iran-war-doc/assembly"
CLIPS_DIR = os.path.join(WORK_DIR, "clips")
SCENES_DIR = os.path.join(WORK_DIR, "scenes")
NARR_BASE = "/home/user/workspace/iran-war-doc/production/narration_audio"
MANIFEST = "/home/user/workspace/iran-war-doc/production/assembly_manifest_v2.json"
FINAL_OUTPUT = os.path.join(WORK_DIR, "FINAL_war_economy_v2.mp4")
B2_BASE = "https://f004.backblazeb2.com/file/economy-vid-assets/v7_war_economy"

FPS = 24
WIDTH = 768
HEIGHT = 512


def run(cmd, timeout=300, desc=""):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and desc:
            print(f"  WARN [{desc}]: {r.stderr[:150]}")
        return r.returncode == 0
    except:
        return False


def probe_dur(path):
    r = subprocess.run(
        f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{path}"',
        shell=True, capture_output=True, text=True, timeout=10
    )
    try:
        return float(r.stdout.strip())
    except:
        return 5.0


def download_clip(clip_id):
    path = os.path.join(CLIPS_DIR, f"{clip_id}.mp4")
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return path
    url = f"{B2_BASE}/{clip_id}.mp4"
    subprocess.run(f'curl -sL -o "{path}" "{url}"', shell=True, timeout=60)
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return path
    return None


def assemble_scene(scene_data):
    scene_num = scene_data["scene_number"]
    sid = f"scene_{scene_num:02d}"
    narr_dur = scene_data["narration_duration"]
    clips_meta = scene_data["clips"]
    narr_segs = scene_data["narration_segments"]
    output = os.path.join(SCENES_DIR, f"{sid}.mp4")

    if os.path.exists(output) and os.path.getsize(output) > 10000:
        d = probe_dur(output)
        if abs(d - narr_dur) < 2.0:
            print(f"  {sid}: already done ({d:.1f}s)")
            return output

    print(f"\n{'='*50}")
    print(f"Scene {scene_num} ({narr_dur:.1f}s, {len(clips_meta)} clips)")

    # Download scene clips
    os.makedirs(CLIPS_DIR, exist_ok=True)
    clip_paths = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(download_clip, c["clip_id"]): c for c in clips_meta}
        for f in as_completed(futures):
            path = f.result()
            if path:
                clip_paths.append((futures[f]["clip_id"], path))

    # Sort by clip_id to maintain order
    clip_paths.sort(key=lambda x: x[0])
    paths_only = [p for _, p in clip_paths]
    
    print(f"  Downloaded {len(paths_only)}/{len(clips_meta)} clips")

    if not paths_only:
        # Black video fallback
        run(f'ffmpeg -y -f lavfi -i "color=c=black:s={WIDTH}x{HEIGHT}:d={narr_dur}:r={FPS}" '
            f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p "{output}"', desc=f"black {sid}")
    else:
        # Build concat list — use clips sequentially, trimmed to narration duration
        concat_txt = os.path.join(CLIPS_DIR, "concat.txt")
        with open(concat_txt, "w") as f:
            for p in paths_only:
                f.write(f"file '{p}'\n")

        # Concat all clips, then trim to narration duration
        raw = os.path.join(CLIPS_DIR, "raw.mp4")
        run(f'ffmpeg -y -f concat -safe 0 -i "{concat_txt}" '
            f'-vf "scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
            f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS}" '
            f'-c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -an "{raw}"',
            timeout=600, desc=f"concat {sid}")

        video_path = os.path.join(CLIPS_DIR, "video.mp4")
        if os.path.exists(raw):
            run(f'ffmpeg -y -i "{raw}" -t {narr_dur} -c copy "{video_path}"',
                timeout=120, desc=f"trim {sid}")
            if not os.path.exists(video_path):
                video_path = raw
        else:
            video_path = None

    # Build narration
    narr_dir = os.path.join(NARR_BASE, sid)
    narr_files = []
    for seg in narr_segs:
        seg_path = os.path.join(narr_dir, seg["file"])
        if os.path.exists(seg_path):
            narr_files.append(seg_path)

    narr_audio = None
    if narr_files:
        if len(narr_files) == 1:
            narr_audio = narr_files[0]
        else:
            narr_concat_txt = os.path.join(CLIPS_DIR, "narr.txt")
            with open(narr_concat_txt, "w") as f:
                for nf in narr_files:
                    f.write(f"file '{nf}'\n")
            narr_audio = os.path.join(CLIPS_DIR, "narration.wav")
            run(f'ffmpeg -y -f concat -safe 0 -i "{narr_concat_txt}" -c:a pcm_s16le "{narr_audio}"',
                timeout=120, desc=f"narr {sid}")

    # Mux video + narration
    if video_path and os.path.exists(video_path) and narr_audio and os.path.exists(narr_audio):
        run(f'ffmpeg -y -i "{video_path}" -i "{narr_audio}" '
            f'-map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k '
            f'-shortest "{output}"',
            timeout=120, desc=f"mux {sid}")
    elif video_path and os.path.exists(video_path):
        import shutil
        shutil.copy2(video_path, output)

    # Cleanup clips to free disk
    for _, p in clip_paths:
        try: os.remove(p)
        except: pass
    for f in ["raw.mp4", "video.mp4", "concat.txt", "narr.txt", "narration.wav"]:
        try: os.remove(os.path.join(CLIPS_DIR, f))
        except: pass

    if os.path.exists(output):
        d = probe_dur(output)
        sz = os.path.getsize(output) / 1e6
        print(f"  ✓ {sid}: {d:.1f}s, {sz:.1f}MB")
    else:
        print(f"  ✗ {sid}: FAILED")

    return output


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(CLIPS_DIR, exist_ok=True)
    os.makedirs(SCENES_DIR, exist_ok=True)

    with open(MANIFEST) as f:
        assembly = json.load(f)

    print(f"WAR ECONOMY — Local Assembly")
    print(f"{'='*50}")
    print(f"Scenes: {assembly['total_scenes']}, Clips: {assembly['total_clips']}")
    print(f"Narration: {assembly['total_narration_sec']/60:.1f} min")

    # Assemble scene by scene
    scene_paths = []
    for scene_data in assembly["scenes"]:
        path = assemble_scene(scene_data)
        scene_paths.append(path)

    # Final concatenation
    print(f"\n{'='*50}")
    print(f"Concatenating {len(scene_paths)} scenes...")

    concat_txt = os.path.join(SCENES_DIR, "final.txt")
    valid = 0
    with open(concat_txt, "w") as f:
        for sp in scene_paths:
            if os.path.exists(sp) and os.path.getsize(sp) > 1000:
                # Re-encode for uniform format
                u = sp.replace(".mp4", "_u.mp4")
                run(f'ffmpeg -y -i "{sp}" -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p '
                    f'-c:a aac -b:a 192k -ar 44100 -ac 2 "{u}"',
                    timeout=300, desc=f"uniform {os.path.basename(sp)}")
                target = u if os.path.exists(u) else sp
                f.write(f"file '{target}'\n")
                valid += 1

    print(f"  {valid} valid scenes")
    run(f'ffmpeg -y -f concat -safe 0 -i "{concat_txt}" -c copy "{FINAL_OUTPUT}"',
        timeout=600, desc="final concat")

    if os.path.exists(FINAL_OUTPUT):
        d = probe_dur(FINAL_OUTPUT)
        sz = os.path.getsize(FINAL_OUTPUT) / 1e6
        print(f"\n✓ FINAL: {FINAL_OUTPUT}")
        print(f"  Duration: {d:.1f}s = {d/60:.1f} min")
        print(f"  Size: {sz:.1f} MB")

        # Upload to B2
        print("\nUploading to B2...")
        run(f'b2 authorize-account B2_KEY_ID B2_APP_KEY', desc="b2 auth")
        
        meta = FINAL_OUTPUT.replace(".mp4", "_meta.mp4")
        run(f'ffmpeg -y -i "{FINAL_OUTPUT}" '
            f'-metadata title="War Economy — The Real Cost (Full Documentary)" '
            f'-metadata comment="LTX-2.3 | 768x512 | 26 scenes | 912 clips | Qwen3-TTS | Full coverage" '
            f'-metadata artist="War Economy Documentary Pipeline" '
            f'-c copy "{meta}"', desc="embed metadata")

        src = meta if os.path.exists(meta) else FINAL_OUTPUT
        ok = run(f'b2 file upload economy-vid-assets "{src}" v7_war_economy/FINAL_war_economy_v2.mp4',
                 timeout=600, desc="b2 upload")
        if ok:
            print("✓ Uploaded to B2!")
        else:
            print("✗ B2 upload failed")
    else:
        print("\n✗ Final assembly FAILED")


if __name__ == "__main__":
    main()
