#!/usr/bin/env python3
"""
Mix TTS narration with video clips and stitch into final scenes, then into full documentary.
Run on each VM after video generation completes to produce per-scene final videos.

Usage: python3 mix_and_stitch.py --video-dir /workspace/video_output --tts-dir /workspace/tts_output \
       --output-dir /workspace/final_output --start-scene 1 --end-scene 7
"""

import argparse
import json
import os
import subprocess
import sys

def get_duration(path):
    """Get media duration in seconds."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    if r.stdout.strip():
        return float(r.stdout.strip())
    return 0

def mix_scene(scene_num, video_dir, tts_dir, output_dir):
    """Mix video and narration for one scene."""
    scene_video = os.path.join(video_dir, f"scene_{scene_num:02d}", f"scene_{scene_num:02d}_video.mp4")
    scene_narr = os.path.join(tts_dir, f"scene_{scene_num:02d}", f"scene_{scene_num:02d}_narration.wav")
    output_file = os.path.join(output_dir, f"scene_{scene_num:02d}_final.mp4")
    
    if os.path.exists(output_file):
        print(f"  Scene {scene_num}: already mixed, skipping")
        return output_file
    
    if not os.path.exists(scene_video):
        # Try to stitch clips manually if scene video doesn't exist
        scene_dir = os.path.join(video_dir, f"scene_{scene_num:02d}")
        clips = sorted([
            os.path.join(scene_dir, f) for f in os.listdir(scene_dir)
            if f.startswith("clip_") and f.endswith(".mp4")
        ]) if os.path.isdir(scene_dir) else []
        
        if not clips:
            print(f"  Scene {scene_num}: NO VIDEO CLIPS FOUND")
            return None
        
        # Stitch clips
        list_file = os.path.join(scene_dir, "stitch_list.txt")
        with open(list_file, "w") as f:
            for c in clips:
                f.write(f"file '{c}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", scene_video],
            check=True, capture_output=True
        )
        os.remove(list_file)
    
    if not os.path.exists(scene_video):
        print(f"  Scene {scene_num}: video file not found")
        return None
    
    vid_dur = get_duration(scene_video)
    
    if not os.path.exists(scene_narr):
        # No narration — just copy video with silent audio
        subprocess.run([
            "ffmpeg", "-y", "-i", scene_video,
            "-af", "apad", "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_file
        ], check=True, capture_output=True)
        print(f"  Scene {scene_num}: video only ({vid_dur:.1f}s)")
        return output_file
    
    narr_dur = get_duration(scene_narr)
    
    # Strategy: 
    # - If video >= narration: trim/speed-adjust video to narration length
    # - If video < narration: slow down video or pad with last frame
    # In both cases, we want the final duration to match narration length
    
    if vid_dur >= narr_dur:
        # Trim video to narration duration, mix audio
        subprocess.run([
            "ffmpeg", "-y",
            "-i", scene_video,
            "-i", scene_narr,
            "-t", str(narr_dur),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_file
        ], check=True, capture_output=True)
    else:
        # Video shorter than narration — loop the video to cover narration
        # Use stream_loop to loop the video, then trim to narration duration
        loops_needed = int(narr_dur / vid_dur) + 2  # a few extra to be safe
        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", str(loops_needed),
            "-i", scene_video,
            "-i", scene_narr,
            "-t", str(narr_dur),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            output_file
        ], check=True, capture_output=True)
    
    final_dur = get_duration(output_file)
    print(f"  Scene {scene_num}: mixed ({final_dur:.1f}s, vid={vid_dur:.1f}s, narr={narr_dur:.1f}s)")
    return output_file

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True)
    parser.add_argument("--tts-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-scene", type=int, default=1)
    parser.add_argument("--end-scene", type=int, default=42)
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    results = []
    for scene_num in range(args.start_scene, args.end_scene + 1):
        result = mix_scene(scene_num, args.video_dir, args.tts_dir, args.output_dir)
        if result:
            results.append({"scene_num": scene_num, "path": result, "duration": get_duration(result)})
    
    # Write manifest
    manifest_path = os.path.join(args.output_dir, f"manifest_{args.start_scene:02d}_{args.end_scene:02d}.json")
    with open(manifest_path, "w") as f:
        json.dump(results, f, indent=2)
    
    total_dur = sum(r["duration"] for r in results)
    print(f"\nDone: {len(results)} scenes, {total_dur:.1f}s total ({total_dur/60:.1f}m)")

if __name__ == "__main__":
    main()
