#!/usr/bin/env python3
"""
Upload fill clips from VMs A-D to B2 with embedded metadata.
Collects clips, embeds metadata via ffmpeg, uploads to B2.
"""
import subprocess, json, os, sys, time, tempfile
from pathlib import Path

WORKSPACE = "/home/user/workspace/iran-war-doc/production"
LOCAL_DIR = os.path.join(WORKSPACE, "collected_fill_clips")
B2_BUCKET = "economy-vid-assets"
B2_PATH = "v7_war_economy/"
TRACKER_FILE = os.path.join(WORKSPACE, "fill_upload_tracker.json")

# B2 credentials
B2_KEY_ID = "B2_KEY_ID"
B2_APP_KEY = "B2_APP_KEY"

# VMs with their SSH info
VMS = {
    "A": ("ssh6.vast.ai", "34822"),
    "B": ("ssh6.vast.ai", "34824"),
    "C": ("ssh8.vast.ai", "34824"),
    "D": ("ssh2.vast.ai", "36946"),
}

SSH_OPTS = "-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o UserKnownHostsFile=/dev/null"

def load_tracker():
    if os.path.exists(TRACKER_FILE):
        return json.load(open(TRACKER_FILE))
    return {"uploaded": {}, "errors": []}

def save_tracker(tracker):
    with open(TRACKER_FILE, 'w') as f:
        json.dump(tracker, f, indent=2)

def load_all_manifests():
    """Load all fill clip manifests to get metadata."""
    clips = {}
    manifest = json.load(open(os.path.join(WORKSPACE, "fill_clips_final.json")))
    for c in manifest:
        clips[c['clip_id']] = c
    return clips

def scp_clip(host, port, remote_path, local_path):
    cmd = f"scp {SSH_OPTS} -P {port} root@{host}:{remote_path} {local_path}"
    result = subprocess.run(cmd, shell=True, capture_output=True, timeout=60)
    return result.returncode == 0

def embed_metadata(input_path, output_path, clip_data):
    """Embed production metadata into video file."""
    meta = {
        "clip_id": clip_data.get("clip_id", ""),
        "scene": clip_data.get("scene_id", ""),
        "prompt": clip_data.get("prompt", ""),
        "duration_s": clip_data.get("duration_s", 5),
        "model": "LTX-2.3-22b-dev",
        "resolution": "768x512",
        "frames": 121,
        "fps": 24,
        "steps": 30,
        "cfg_scale": 3.0,
        "stg_scale": 1.0,
        "stg_block": 28,
        "quantization": "none",
        "type": "fill_clip",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    
    # Build ffmpeg metadata args
    meta_args = []
    for k, v in meta.items():
        meta_args.extend(["-metadata", f"{k}={v}"])
    
    cmd = ["ffmpeg", "-y", "-i", input_path, "-c", "copy"] + meta_args + [output_path]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0

def upload_to_b2(local_path, remote_name):
    """Upload file to B2 using b2 CLI."""
    remote_path = f"{B2_PATH}{remote_name}"
    cmd = f"b2 upload-file {B2_BUCKET} {local_path} {remote_path}"
    result = subprocess.run(cmd, shell=True, capture_output=True, timeout=120,
                          env={**os.environ, "B2_APPLICATION_KEY_ID": B2_KEY_ID, "B2_APPLICATION_KEY": B2_APP_KEY})
    return result.returncode == 0, result.stderr.decode()

def main():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    
    tracker = load_tracker()
    manifests = load_all_manifests()
    
    # Build clip->VM map (which VM to download from)
    clip_vm_map = {}
    with open('/tmp/all_fill_clips.txt') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) == 2:
                vm, clip = parts
                if clip not in clip_vm_map:
                    clip_vm_map[clip] = vm
    
    # Filter to only clips on VMs A-D
    clips_to_process = {}
    for clip_id, vm in clip_vm_map.items():
        if vm in VMS and clip_id not in tracker["uploaded"]:
            clips_to_process[clip_id] = vm
    
    total = len(clips_to_process)
    print(f"Clips to upload: {total} (already uploaded: {len(tracker['uploaded'])})")
    
    # Authorize b2
    auth_cmd = f"b2 authorize-account {B2_KEY_ID} {B2_APP_KEY}"
    subprocess.run(auth_cmd, shell=True, capture_output=True, timeout=30)
    
    done = 0
    errors = 0
    for clip_id, vm in sorted(clips_to_process.items()):
        host, port = VMS[vm]
        remote_path = f"/workspace/outputs/{clip_id}.mp4"
        local_raw = os.path.join(LOCAL_DIR, f"{clip_id}_raw.mp4")
        local_meta = os.path.join(LOCAL_DIR, f"{clip_id}.mp4")
        
        # Download
        if not scp_clip(host, port, remote_path, local_raw):
            print(f"  SKIP {clip_id}: SCP failed from VM {vm}")
            tracker["errors"].append({"clip": clip_id, "error": "scp_failed", "vm": vm})
            errors += 1
            continue
        
        # Embed metadata
        clip_data = manifests.get(clip_id, {"clip_id": clip_id})
        if not embed_metadata(local_raw, local_meta, clip_data):
            # If metadata embedding fails, use raw
            os.rename(local_raw, local_meta)
        else:
            os.remove(local_raw)
        
        # Upload to B2
        ok, err = upload_to_b2(local_meta, f"{clip_id}.mp4")
        if ok:
            done += 1
            tracker["uploaded"][clip_id] = {
                "vm": vm,
                "b2_path": f"{B2_PATH}{clip_id}.mp4",
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size_bytes": os.path.getsize(local_meta),
            }
            if done % 10 == 0:
                save_tracker(tracker)
                print(f"  [{done}/{total}] {clip_id} uploaded")
        else:
            print(f"  ERROR {clip_id}: B2 upload failed: {err[:100]}")
            tracker["errors"].append({"clip": clip_id, "error": f"b2: {err[:200]}"})
            errors += 1
        
        # Clean up local file
        if os.path.exists(local_meta):
            os.remove(local_meta)
    
    save_tracker(tracker)
    print(f"\nDone: {done} uploaded, {errors} errors, {len(tracker['uploaded'])} total on B2")

if __name__ == "__main__":
    main()
