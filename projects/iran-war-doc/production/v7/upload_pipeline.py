#!/usr/bin/env python3
"""
Pull completed clips from Vast.ai VMs, embed production metadata into video files,
upload to B2 (economy-vid-assets bucket), and track what's been uploaded.
"""
import subprocess, json, os, sys, time, tempfile, shutil
from pathlib import Path
from datetime import datetime

WORKSPACE = "/home/user/workspace/iran-war-doc/production"
LOCAL_CLIPS = os.path.join(WORKSPACE, "collected_clips")
UPLOAD_TRACKER = os.path.join(WORKSPACE, "upload_tracker.json")
B2_BUCKET = "b2://economy-vid-assets/v7_war_economy/"

# VM definitions
VMS = [
    {"name": "H100_US", "id": "33100690", "host": "ssh9.vast.ai", "port": "20690",
     "output_dirs": ["/workspace/outputs"], "manifest": "clips_h100_us_round2.json",
     "extra_outputs": ["/workspace/outputs_round1"]},  # round 1 completed clips
    {"name": "H100_US2", "id": "33101455", "host": "ssh9.vast.ai", "port": "21454",
     "output_dirs": ["/workspace/outputs"], "manifest": "clips_h100_us2.json"},
    {"name": "H100_DC_GPU0", "id": "33103194_g0", "host": "ssh8.vast.ai", "port": "23194",
     "output_dirs": ["/workspace/outputs_gpu0"], "manifest": "clips_h100_dc_gpu0.json"},
    {"name": "H100_DC_GPU1", "id": "33103194_g1", "host": "ssh8.vast.ai", "port": "23194",
     "output_dirs": ["/workspace/outputs_gpu1"], "manifest": "clips_h100_dc_gpu1.json"},
    {"name": "H100_US3", "id": "33103197", "host": "ssh2.vast.ai", "port": "23196",
     "output_dirs": ["/workspace/outputs"], "manifest": "clips_h100_us3.json"},
    {"name": "H100_JP", "id": "33133362", "host": "ssh9.vast.ai", "port": "13362",
     "output_dirs": ["/workspace/outputs"], "manifest": "clips_final_batch.json"},
]

SSH_OPTS = "-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5"

def load_tracker():
    if os.path.exists(UPLOAD_TRACKER):
        return json.load(open(UPLOAD_TRACKER))
    return {"uploaded": {}, "collected": {}, "errors": []}

def save_tracker(tracker):
    with open(UPLOAD_TRACKER, 'w') as f:
        json.dump(tracker, f, indent=2)

def load_manifest(manifest_file):
    """Load manifest to get clip metadata."""
    path = os.path.join(WORKSPACE, manifest_file)
    if os.path.exists(path):
        return {c['clip_id']: c for c in json.load(open(path))}
    return {}

def ssh_cmd(host, port, cmd):
    full_cmd = f"ssh {SSH_OPTS} -p {port} root@{host} \"{cmd}\""
    try:
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", -1

def list_completed_clips(vm):
    """List clips that have all sub-clips generated (final mp4 assembled or all subs present)."""
    completed = []
    for output_dir in vm["output_dirs"] + vm.get("extra_outputs", []):
        # List all files
        out, rc = ssh_cmd(vm["host"], vm["port"], 
            f"ls {output_dir}/*.mp4 2>/dev/null")
        if rc != 0 or not out:
            continue
        
        files = [os.path.basename(f) for f in out.split('\n') if f.strip()]
        
        # Group by clip_id (remove _subNN suffix)
        clip_subs = {}
        for f in files:
            # Pattern: scene_XX_clipYY_subNN.mp4 or scene_XX_clipYY.mp4 (final)
            name = f.replace('.mp4', '')
            if '_sub' in name:
                clip_id = name.rsplit('_sub', 1)[0]
                sub_num = int(name.rsplit('_sub', 1)[1].replace('_sub', ''))
                if clip_id not in clip_subs:
                    clip_subs[clip_id] = {"subs": [], "has_final": False, "dir": output_dir}
                clip_subs[clip_id]["subs"].append(sub_num)
            else:
                clip_id = name
                if clip_id not in clip_subs:
                    clip_subs[clip_id] = {"subs": [], "has_final": False, "dir": output_dir}
                clip_subs[clip_id]["has_final"] = True
        
        # A clip is "complete" if it has a final .mp4 (the script creates one)
        # OR if we can determine all subs are present from the manifest
        for clip_id, info in clip_subs.items():
            if info["has_final"]:
                completed.append({"clip_id": clip_id, "dir": info["dir"], 
                                  "file": f"{clip_id}.mp4", "subs": sorted(info["subs"])})
            elif info["subs"]:
                # Check if all subs are present by looking at max sub number
                # The script numbers subs 00, 01, 02... so if we have 0..N contiguous, it might be done
                # But we can't be sure without the manifest. Include sub-clips for now.
                max_sub = max(info["subs"])
                if list(range(max_sub + 1)) == sorted(info["subs"]):
                    completed.append({"clip_id": clip_id, "dir": info["dir"],
                                      "file": None, "subs": sorted(info["subs"])})
    
    return completed

def scp_file(host, port, remote_path, local_path):
    """Download a file from VM."""
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    cmd = f"scp {SSH_OPTS} -P {port} root@{host}:{remote_path} {local_path}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return result.returncode == 0

def concat_subs(clip_dir, clip_id, subs):
    """Concatenate sub-clips into a single clip using ffmpeg."""
    sub_files = [os.path.join(clip_dir, f"{clip_id}_sub{s:02d}.mp4") for s in subs]
    # Check all exist
    for f in sub_files:
        if not os.path.exists(f):
            return None
    
    # Create concat list
    list_file = os.path.join(clip_dir, "concat_list.txt")
    with open(list_file, 'w') as f:
        for sf in sub_files:
            f.write(f"file '{sf}'\n")
    
    output = os.path.join(clip_dir, f"{clip_id}.mp4")
    cmd = f"ffmpeg -y -f concat -safe 0 -i {list_file} -c copy {output}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    os.remove(list_file)
    
    if result.returncode == 0 and os.path.exists(output):
        return output
    return None

def embed_metadata(input_path, clip_id, metadata):
    """Embed production metadata into video file using ffmpeg."""
    output_path = input_path.replace('.mp4', '_meta.mp4')
    
    meta_str = json.dumps(metadata, ensure_ascii=False)
    
    # Build ffmpeg metadata args
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy",
        "-metadata", f"title={clip_id}",
        "-metadata", f"comment={meta_str}",
        "-metadata", f"description=War Economy Documentary - {clip_id}",
        "-metadata", f"artist=LTX-2.3 22B bf16",
        "-metadata", f"album=War Economy Documentary v7",
        "-metadata", f"date={datetime.utcnow().strftime('%Y-%m-%d')}",
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0 and os.path.exists(output_path):
        # Replace original with metadata version
        shutil.move(output_path, input_path)
        return True
    return False

def upload_to_b2(local_path, clip_id):
    """Upload to B2 bucket."""
    b2_filename = f"v7_war_economy/{clip_id}.mp4"
    cmd = f"b2 file upload economy-vid-assets {local_path} {b2_filename} --no-progress"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    return result.returncode == 0, result.stdout + result.stderr

def main():
    os.makedirs(LOCAL_CLIPS, exist_ok=True)
    tracker = load_tracker()
    
    # Load all manifests for metadata
    all_metadata = {}
    for vm in VMS:
        manifest = load_manifest(vm["manifest"])
        all_metadata.update(manifest)
    # Also load the original H100 US manifest
    orig = load_manifest("clips_h100_us.json")
    all_metadata.update(orig)
    
    total_collected = 0
    total_uploaded = 0
    
    for vm in VMS:
        print(f"\n{'='*60}")
        print(f"Processing {vm['name']} ({vm['host']}:{vm['port']})")
        print(f"{'='*60}")
        
        completed = list_completed_clips(vm)
        print(f"  Found {len(completed)} completed clips")
        
        for clip_info in completed:
            clip_id = clip_info["clip_id"]
            
            # Skip if already uploaded
            if clip_id in tracker["uploaded"]:
                print(f"  [{clip_id}] Already uploaded, skipping")
                continue
            
            clip_dir = os.path.join(LOCAL_CLIPS, clip_id)
            os.makedirs(clip_dir, exist_ok=True)
            
            # Download sub-clips
            print(f"  [{clip_id}] Downloading from {vm['name']}...")
            downloaded_subs = []
            for sub in clip_info["subs"]:
                sub_file = f"{clip_id}_sub{sub:02d}.mp4"
                remote = f"{clip_info['dir']}/{sub_file}"
                local = os.path.join(clip_dir, sub_file)
                
                if os.path.exists(local) and os.path.getsize(local) > 0:
                    downloaded_subs.append(sub)
                    continue
                
                if scp_file(vm["host"], vm["port"], remote, local):
                    downloaded_subs.append(sub)
                else:
                    print(f"    FAILED to download {sub_file}")
            
            # Also download final clip if it exists
            if clip_info["file"]:
                remote = f"{clip_info['dir']}/{clip_info['file']}"
                local = os.path.join(clip_dir, clip_info["file"])
                if not (os.path.exists(local) and os.path.getsize(local) > 0):
                    scp_file(vm["host"], vm["port"], remote, local)
            
            # Concatenate if needed
            final_path = os.path.join(clip_dir, f"{clip_id}.mp4")
            if not os.path.exists(final_path) and downloaded_subs:
                print(f"    Concatenating {len(downloaded_subs)} sub-clips...")
                result = concat_subs(clip_dir, clip_id, downloaded_subs)
                if not result:
                    print(f"    FAILED to concatenate")
                    continue
            
            if not os.path.exists(final_path):
                print(f"    No final clip available, skipping")
                continue
            
            total_collected += 1
            
            # Embed metadata
            meta = all_metadata.get(clip_id, {})
            production_meta = {
                "clip_id": clip_id,
                "model": "LTX-2.3-22B-bf16",
                "resolution": "768x512",
                "frames": 121,
                "fps": 24,
                "steps": 30,
                "cfg": 3.0,
                "stg": 1.0,
                "vm": vm["name"],
                "scene": meta.get("scene_number", ""),
                "scene_title": meta.get("scene_title", ""),
                "target_duration": meta.get("target_duration_sec", ""),
                "prompt": meta.get("prompt", "")[:500],
                "narration_trigger": meta.get("narration_trigger", ""),
                "color_palette": meta.get("color_palette", ""),
                "generated": datetime.utcnow().isoformat()
            }
            
            print(f"    Embedding metadata...")
            embed_metadata(final_path, clip_id, production_meta)
            
            # Upload to B2
            print(f"    Uploading to B2...")
            success, output = upload_to_b2(final_path, clip_id)
            if success:
                total_uploaded += 1
                tracker["uploaded"][clip_id] = {
                    "b2_path": f"v7_war_economy/{clip_id}.mp4",
                    "size_bytes": os.path.getsize(final_path),
                    "uploaded_at": datetime.utcnow().isoformat(),
                    "vm_source": vm["name"]
                }
                print(f"    ✓ Uploaded to B2")
            else:
                tracker["errors"].append({"clip_id": clip_id, "error": output[:200], 
                                          "time": datetime.utcnow().isoformat()})
                print(f"    ✗ Upload failed: {output[:100]}")
            
            tracker["collected"][clip_id] = vm["name"]
            save_tracker(tracker)
    
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Collected: {total_collected} new clips")
    print(f"Uploaded to B2: {total_uploaded} new clips")
    print(f"Total uploaded (all time): {len(tracker['uploaded'])}")
    print(f"Errors: {len(tracker['errors'])}")
    
    save_tracker(tracker)

if __name__ == "__main__":
    main()
