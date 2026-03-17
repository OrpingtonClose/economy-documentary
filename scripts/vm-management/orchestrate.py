#!/usr/bin/env python3
"""
Master orchestration: distributes scenes across 6 VMs, runs TTS + video generation in parallel.

Workflow per VM:
1. Copy scenes_parsed.json + scene_prompts.json + generation scripts
2. Run TTS for assigned scenes
3. Run video generation for assigned scenes
4. Report back

Usage: python3 orchestrate.py
"""

import json
import subprocess
import os
import time
import sys

# VM configuration
VMS = [
    {"id": "32971953", "host": "ssh6.vast.ai", "port": 11952, "location": "Oklahoma"},
    {"id": "32971954", "host": "ssh6.vast.ai", "port": 11954, "location": "Montana"},
    {"id": "32971955", "host": "ssh4.vast.ai", "port": 11954, "location": "Czechia"},
    {"id": "32971956", "host": "ssh6.vast.ai", "port": 11956, "location": "Massachusetts"},
    {"id": "32971957", "host": "ssh7.vast.ai", "port": 11956, "location": "UK-1"},
    {"id": "32971958", "host": "ssh8.vast.ai", "port": 11958, "location": "UK-2"},
]

SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR -i ~/.ssh/id_ed25519"

# Scene distribution: 42 scenes across 6 VMs = 7 each
SCENE_ASSIGNMENTS = {
    "32971953": (1, 7),    # Scenes 1-7
    "32971954": (8, 14),   # Scenes 8-14
    "32971955": (15, 21),  # Scenes 15-21
    "32971956": (22, 28),  # Scenes 22-28
    "32971957": (29, 35),  # Scenes 29-35
    "32971958": (36, 42),  # Scenes 36-42
}

LOCAL_FILES = [
    "/home/user/workspace/scenes_parsed.json",
    "/home/user/workspace/scene_prompts.json",
    "/home/user/workspace/generate_tts.py",
    "/home/user/workspace/generate_video.py",
]

def ssh_cmd(vm, command, timeout=600):
    """Run a command on a VM via SSH."""
    full_cmd = f"ssh {SSH_OPTS} -p {vm['port']} root@{vm['host']} \"{command}\""
    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return result

def scp_to_vm(vm, local_path, remote_path):
    """Copy a file to a VM."""
    cmd = f"scp {SSH_OPTS} -P {vm['port']} {local_path} root@{vm['host']}:{remote_path}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return result.returncode == 0

def deploy_files(vm):
    """Deploy all necessary files to a VM."""
    print(f"  Deploying files to VM {vm['id']} ({vm['location']})...")
    
    # Create workspace directories
    ssh_cmd(vm, "mkdir -p /workspace/scripts /workspace/tts_output /workspace/video_output")
    
    for local_file in LOCAL_FILES:
        filename = os.path.basename(local_file)
        remote_path = f"/workspace/scripts/{filename}"
        if scp_to_vm(vm, local_file, remote_path):
            print(f"    Copied {filename}")
        else:
            print(f"    FAILED to copy {filename}")
            return False
    return True

def start_tts_generation(vm, start_scene, end_scene):
    """Start TTS generation on a VM (backgrounded)."""
    cmd = (
        f"cd /workspace/LTX-2 && source .venv/bin/activate && "
        f"nohup python3 /workspace/scripts/generate_tts.py "
        f"--scenes /workspace/scripts/scenes_parsed.json "
        f"--output-dir /workspace/tts_output "
        f"--model-path /workspace/models/qwen-tts-voicedesign "
        f"--start-scene {start_scene} --end-scene {end_scene} "
        f"> /workspace/tts_log.txt 2>&1 &"
    )
    ssh_cmd(vm, cmd, timeout=30)
    print(f"  VM {vm['id']}: TTS started for scenes {start_scene}-{end_scene}")

def start_video_generation(vm, start_scene, end_scene):
    """Start video generation on a VM (backgrounded)."""
    cmd = (
        f"cd /workspace/LTX-2 && source .venv/bin/activate && "
        f"nohup python3 /workspace/scripts/generate_video.py "
        f"--prompts /workspace/scripts/scene_prompts.json "
        f"--output-dir /workspace/video_output "
        f"--tts-dir /workspace/tts_output "
        f"--checkpoint /workspace/models/ltx23/ltx-2.3-22b-dev.safetensors "
        f"--gemma-root /workspace/models/gemma3 "
        f"--start-scene {start_scene} --end-scene {end_scene} "
        f"--height 512 --width 768 --num-steps 30 "
        f"> /workspace/video_log.txt 2>&1 &"
    )
    ssh_cmd(vm, cmd, timeout=30)
    print(f"  VM {vm['id']}: Video generation started for scenes {start_scene}-{end_scene}")

def check_tts_status(vm):
    """Check if TTS is still running and get progress."""
    result = ssh_cmd(vm, "pgrep -f generate_tts.py > /dev/null 2>&1 && echo RUNNING || echo DONE; tail -3 /workspace/tts_log.txt 2>/dev/null", timeout=15)
    return result.stdout.strip()

def check_video_status(vm):
    """Check if video gen is still running and get progress."""
    result = ssh_cmd(vm, "pgrep -f generate_video.py > /dev/null 2>&1 && echo RUNNING || echo DONE; tail -3 /workspace/video_log.txt 2>/dev/null", timeout=15)
    return result.stdout.strip()

def check_all_status():
    """Check status of all VMs."""
    for vm in VMS:
        start, end = SCENE_ASSIGNMENTS[vm["id"]]
        print(f"\nVM {vm['id']} ({vm['location']}) — Scenes {start}-{end}:")
        
        # Check TTS
        tts_status = check_tts_status(vm)
        print(f"  TTS: {tts_status}")
        
        # Check video
        vid_status = check_video_status(vm)
        print(f"  Video: {vid_status}")
        
        # Count completed files
        result = ssh_cmd(vm, "ls /workspace/tts_output/scene_*/scene_*_narration.wav 2>/dev/null | wc -l; ls /workspace/video_output/scene_*/scene_*_video.mp4 2>/dev/null | wc -l", timeout=15)
        counts = result.stdout.strip().split('\n')
        tts_done = counts[0] if counts else "?"
        vid_done = counts[1] if len(counts) > 1 else "?"
        print(f"  Completed: {tts_done} TTS / {vid_done} videos")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        check_all_status()
        return
    
    if len(sys.argv) > 1 and sys.argv[1] == "video":
        # Start video generation (after TTS is done)
        print("Starting video generation on all VMs...")
        for vm in VMS:
            start, end = SCENE_ASSIGNMENTS[vm["id"]]
            start_video_generation(vm, start, end)
        return
    
    # Full deployment
    print("=" * 60)
    print("DEPLOYING TO ALL 6 VMs")
    print("=" * 60)
    
    for vm in VMS:
        deploy_files(vm)
    
    print("\n" + "=" * 60)
    print("STARTING TTS GENERATION (Phase 1)")
    print("=" * 60)
    
    for vm in VMS:
        start, end = SCENE_ASSIGNMENTS[vm["id"]]
        start_tts_generation(vm, start, end)
    
    print("\nTTS generation started on all VMs.")
    print("Run 'python3 orchestrate.py status' to check progress.")
    print("Once TTS is done, run 'python3 orchestrate.py video' to start video generation.")

if __name__ == "__main__":
    main()
