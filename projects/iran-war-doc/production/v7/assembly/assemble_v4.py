#!/usr/bin/env python3
"""
Assembly v4 — Sequential normalization to avoid resource exhaustion.

- Downloads clips in parallel (lightweight curl)
- Normalizes clips SEQUENTIALLY (ffmpeg is CPU/memory heavy on 2 vCPU)
- Concats with -c copy after normalization
- Muxes with narration audio
- Cleans up aggressively between scenes
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORK_DIR = "/home/user/workspace/iran-war-doc/assembly"
TMP_DIR = os.path.join(WORK_DIR, "tmp")
SCENES_DIR = os.path.join(WORK_DIR, "scenes")
NARR_BASE = "/home/user/workspace/iran-war-doc/production/narration_audio"
MANIFEST = "/home/user/workspace/iran-war-doc/production/assembly_manifest_v2.json"
FINAL_OUTPUT = os.path.join(WORK_DIR, "FINAL_war_economy_v2.mp4")
B2_BASE = "https://f004.backblazeb2.com/file/economy-vid-assets/v7_war_economy"

FPS = 24
WIDTH = 768
HEIGHT = 512

VF = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:-1:-1:color=black,fps={FPS},setsar=1"


def probe_dur(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10
        )
        return float(r.stdout.strip())
    except:
        return 0.0


def download_clip(clip_id):
    path = os.path.join(TMP_DIR, f"{clip_id}.mp4")
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return path
    url = f"{B2_BASE}/{clip_id}.mp4"
    try:
        r = subprocess.run(
            ["curl", "-sL", "--connect-timeout", "15", "--max-time", "120", "-o", path, url],
            timeout=130
        )
        if os.path.exists(path) and os.path.getsize(path) > 500:
            return path
    except:
        pass
    try:
        os.remove(path)
    except:
        pass
    return None


def normalize_clip(clip_id, src_path, out_path):
    """Normalize one clip. Returns True on success."""
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-vf", VF,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an", out_path
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            return True
        return False
    except:
        return False


def clean_tmp():
    for f in os.listdir(TMP_DIR):
        try:
            os.remove(os.path.join(TMP_DIR, f))
        except:
            pass


def assemble_scene(scene_data):
    scene_num = scene_data["scene_number"]
    sid = f"scene_{scene_num:02d}"
    narr_dur = scene_data["narration_duration"]
    clips_meta = scene_data["clips"]
    narr_segs = scene_data.get("narration_segments", [])
    output = os.path.join(SCENES_DIR, f"{sid}.mp4")

    # Check if already done correctly
    if os.path.exists(output) and os.path.getsize(output) > 10000:
        d = probe_dur(output)
        if abs(d - narr_dur) < 3.0:
            print(f"  {sid}: already done ({d:.1f}s)")
            return output

    print(f"\n{'='*60}")
    print(f"Scene {scene_num}: {narr_dur:.1f}s narration, {len(clips_meta)} clips")
    t0 = time.time()

    clean_tmp()

    # Step 1: Download all clips (parallel, lightweight)
    print(f"  Downloading...", end=" ", flush=True)
    downloaded = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(download_clip, c["clip_id"]): c["clip_id"] for c in clips_meta}
        for fut in as_completed(futs):
            cid = futs[fut]
            path = fut.result()
            if path:
                downloaded[cid] = path
    print(f"{len(downloaded)}/{len(clips_meta)}")

    if not downloaded:
        # Black video fallback
        bv = os.path.join(TMP_DIR, "black.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"color=c=black:s={WIDTH}x{HEIGHT}:d={narr_dur}:r={FPS}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", bv
        ], capture_output=True, timeout=120)
        video_path = bv
    else:
        # Step 2: Normalize clips SEQUENTIALLY (key fix for 2 vCPU machine)
        print(f"  Normalizing...", end=" ", flush=True)
        norm_paths = []  # (clip_id, normalized_path) in order
        ordered_ids = [c["clip_id"] for c in clips_meta if c["clip_id"] in downloaded]

        for cid in ordered_ids:
            src = downloaded[cid]
            dst = os.path.join(TMP_DIR, f"{cid}_n.mp4")
            if normalize_clip(cid, src, dst):
                norm_paths.append((cid, dst))
            # Delete raw immediately to save disk
            try:
                os.remove(src)
            except:
                pass

        print(f"{len(norm_paths)}/{len(downloaded)}")

        if not norm_paths:
            bv = os.path.join(TMP_DIR, "black.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=c=black:s={WIDTH}x{HEIGHT}:d={narr_dur}:r={FPS}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p", bv
            ], capture_output=True, timeout=120)
            video_path = bv
        else:
            # Step 3: Concat normalized clips
            concat_txt = os.path.join(TMP_DIR, "concat.txt")
            with open(concat_txt, "w") as f:
                for cid, npath in norm_paths:
                    f.write(f"file '{npath}'\n")

            raw_video = os.path.join(TMP_DIR, "raw.mp4")
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", raw_video],
                capture_output=True, text=True, timeout=300
            )

            if r.returncode != 0 or not os.path.exists(raw_video):
                # Fallback: re-encode concat
                r = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
                     "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                     "-pix_fmt", "yuv420p", raw_video],
                    capture_output=True, text=True, timeout=600
                )

            # Delete normalized clips
            for _, npath in norm_paths:
                try:
                    os.remove(npath)
                except:
                    pass

            if os.path.exists(raw_video):
                raw_dur = probe_dur(raw_video)
                print(f"  Video: {raw_dur:.1f}s (need {narr_dur:.1f}s)")

                # Trim if longer than narration
                video_path = os.path.join(TMP_DIR, "video.mp4")
                if raw_dur > narr_dur + 0.5:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", raw_video, "-t", str(narr_dur), "-c", "copy", video_path],
                        capture_output=True, timeout=60
                    )
                    if os.path.exists(video_path):
                        os.remove(raw_video)
                    else:
                        video_path = raw_video
                else:
                    video_path = raw_video
            else:
                print(f"  Concat failed!")
                bv = os.path.join(TMP_DIR, "black.mp4")
                subprocess.run([
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:s={WIDTH}x{HEIGHT}:d={narr_dur}:r={FPS}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-pix_fmt", "yuv420p", bv
                ], capture_output=True, timeout=120)
                video_path = bv

    # Step 4: Build narration audio
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
            narr_txt = os.path.join(TMP_DIR, "narr.txt")
            with open(narr_txt, "w") as f:
                for nf in narr_files:
                    f.write(f"file '{nf}'\n")
            narr_audio = os.path.join(TMP_DIR, "narration.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", narr_txt,
                 "-c:a", "pcm_s16le", narr_audio],
                capture_output=True, timeout=120
            )
            if not os.path.exists(narr_audio):
                narr_audio = narr_files[0]

    # Step 5: Mux video + audio
    if video_path and os.path.exists(video_path) and narr_audio and os.path.exists(narr_audio):
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-i", narr_audio,
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
             "-shortest", output],
            capture_output=True, timeout=180
        )
    elif video_path and os.path.exists(video_path):
        import shutil
        shutil.copy2(video_path, output)

    # Cleanup
    clean_tmp()

    elapsed = time.time() - t0
    if os.path.exists(output):
        d = probe_dur(output)
        sz = os.path.getsize(output) / 1e6
        ok = abs(d - narr_dur) < 3.0
        print(f"  -> {sid}: {d:.1f}s, {sz:.1f}MB {'OK' if ok else 'MISMATCH'} ({elapsed:.0f}s)")
        return output
    else:
        print(f"  -> {sid}: FAILED ({elapsed:.0f}s)")
        return None


def main():
    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(SCENES_DIR, exist_ok=True)

    with open(MANIFEST) as f:
        assembly = json.load(f)

    start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 26
    do_final = "--no-final" not in sys.argv
    do_upload = "--no-upload" not in sys.argv

    print(f"WAR ECONOMY — Assembly v4")
    print(f"Scenes {start}-{end}, final={'yes' if do_final else 'no'}")
    print(f"{'='*60}")

    for scene_data in assembly["scenes"]:
        sn = scene_data["scene_number"]
        if sn < start or sn > end:
            continue
        assemble_scene(scene_data)

    # Verification
    print(f"\n{'='*60}")
    print("Verification:")
    total_dur = 0
    all_ok = True
    scene_paths = []
    for s in assembly["scenes"]:
        sn = s["scene_number"]
        p = os.path.join(SCENES_DIR, f"scene_{sn:02d}.mp4")
        scene_paths.append(p)
        if os.path.exists(p):
            d = probe_dur(p)
            expected = s["narration_duration"]
            ok = abs(d - expected) < 3.0
            total_dur += d
            if not ok:
                all_ok = False
            print(f"  Scene {sn:2d}: {d:6.1f}s / {expected:6.1f}s  {'OK' if ok else 'BAD'}")
        else:
            print(f"  Scene {sn:2d}: MISSING")
            all_ok = False

    print(f"\n  Total: {total_dur:.0f}s = {total_dur/60:.1f} min (expected {assembly['total_narration_sec']/60:.1f} min)")

    if not do_final:
        return

    # Final concat
    print(f"\n{'='*60}")
    print("Final concatenation...")
    valid = [p for p in scene_paths if os.path.exists(p) and os.path.getsize(p) > 1000]

    # Re-encode each for uniform format
    uniform = []
    for sp in valid:
        upath = sp.replace(".mp4", "_u.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", sp,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", upath],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0 and os.path.exists(upath):
            uniform.append(upath)
        else:
            uniform.append(sp)

    concat_txt = os.path.join(SCENES_DIR, "final.txt")
    with open(concat_txt, "w") as f:
        for u in uniform:
            f.write(f"file '{u}'\n")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", FINAL_OUTPUT],
        capture_output=True, timeout=900
    )

    # Cleanup uniform files
    for u in uniform:
        if u.endswith("_u.mp4"):
            try:
                os.remove(u)
            except:
                pass

    if os.path.exists(FINAL_OUTPUT):
        d = probe_dur(FINAL_OUTPUT)
        sz = os.path.getsize(FINAL_OUTPUT) / 1e6
        print(f"FINAL: {d:.0f}s = {d/60:.1f} min, {sz:.0f} MB")

        if do_upload:
            print("\nUploading to B2...")
            subprocess.run(
                ["b2", "authorize-account", "B2_KEY_ID", "B2_APP_KEY"],
                capture_output=True, timeout=30
            )

            meta = FINAL_OUTPUT.replace(".mp4", "_meta.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", FINAL_OUTPUT,
                 "-metadata", f"title=War Economy — The Real Cost (Full Documentary)",
                 "-metadata", f"comment=LTX-2.3 768x512 24fps | 26 scenes | 912 clips | Qwen3-TTS | {d:.0f}s",
                 "-metadata", "artist=War Economy Documentary Pipeline v7",
                 "-c", "copy", meta],
                capture_output=True, timeout=60
            )

            src = meta if os.path.exists(meta) else FINAL_OUTPUT
            r = subprocess.run(
                ["b2", "file", "upload", "economy-vid-assets", src, "v7_war_economy/FINAL_war_economy_v2.mp4"],
                capture_output=True, text=True, timeout=900
            )
            print("Upload OK" if r.returncode == 0 else f"Upload failed: {r.stderr[:200]}")

            if os.path.exists(meta):
                os.remove(meta)
    else:
        print("FINAL ASSEMBLY FAILED")


if __name__ == "__main__":
    main()
