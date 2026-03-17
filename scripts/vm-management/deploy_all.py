#!/usr/bin/env python3
"""
Deploy LTX-2.3 generation pipeline to all 10 VMs in parallel.
Steps per VM:
1. Upload scripts (v6_generate_v3.py, v6_encode_prompts.py, frameio_upload.py, etc.)
2. Upload clip plan + embeddings cache
3. Run setup (install deps, download model)
4. Start generation with assigned clip range
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SSH_KEY = os.path.expanduser("~/.ssh/vast_v3")
WORKSPACE = "/home/user/workspace"

# Load VM info
with open(f"{WORKSPACE}/vm_info.json") as f:
    VMS = json.load(f)

# Load missing clips and split across VMs
with open(f"{WORKSPACE}/missing_clips.json") as f:
    missing = json.load(f)

n_vms = len(VMS)
chunk_size = len(missing) // n_vms
remainder = len(missing) % n_vms

assignments = {}
start = 0
for i, vm in enumerate(sorted(VMS, key=lambda v: v['label'])):
    end = start + chunk_size + (1 if i < remainder else 0)
    clip_list = missing[start:end]
    assignments[vm['id']] = {
        'vm': vm,
        'clips': clip_list,
        'clip_ids': [f"clip{c:03d}" if c < 100 else f"clip{c}" for c in clip_list],
    }
    start = end

def ssh_cmd(host, port, cmd, timeout=600):
    """Run SSH command and return output."""
    result = subprocess.run(
        ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=15", f"root@{host}", "-i", SSH_KEY, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.stdout, result.stderr, result.returncode

def scp_file(host, port, local, remote):
    """SCP file to VM."""
    result = subprocess.run(
        ["scp", "-P", str(port), "-o", "StrictHostKeyChecking=no",
         "-i", SSH_KEY, local, f"root@{host}:{remote}"],
        capture_output=True, text=True, timeout=300
    )
    return result.returncode == 0

def setup_vm(vm_id, assignment):
    vm = assignment['vm']
    label = vm['label']
    host = vm['ssh_host']
    port = vm['ssh_port']
    clips = assignment['clips']
    
    log = lambda msg: print(f"[{label}] {msg}", flush=True)
    
    if vm['status'] != 'running':
        log(f"SKIP - not running (status: {vm['status']})")
        return False
    
    try:
        # Test SSH
        log("Testing SSH...")
        out, err, rc = ssh_cmd(host, port, "echo OK && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", timeout=30)
        if rc != 0:
            log(f"SSH failed: {err[:100]}")
            return False
        log(f"Connected: {out.strip()}")
        
        # Upload scripts
        log("Uploading scripts...")
        scripts = [
            "v6_generate_v3.py", "v6_encode_prompts.py", "frameio_upload.py",
            "frameio_tokens.json", "v5_clip_plan.json", "vm_setup.sh"
        ]
        for s in scripts:
            local_path = f"{WORKSPACE}/{s}"
            if os.path.exists(local_path):
                if not scp_file(host, port, local_path, f"/root/{s}"):
                    log(f"  Failed to upload {s}")
                    
        log("Scripts uploaded")
        
        # Create the clip assignment file for this VM
        # We'll write a JSON with just the clip numbers this VM should generate
        assign_json = json.dumps(clips)
        ssh_cmd(host, port, f"echo '{assign_json}' > /root/my_clips.json")
        
        # Run setup (install deps, download model, text encoder)
        log("Running setup (this takes 10-20 min for model download)...")
        ssh_cmd(host, port, 
            "bash /root/vm_setup.sh " + label,
            timeout=1800)  # 30 min timeout for model download
        
        log("Setup complete, verifying model...")
        out, _, _ = ssh_cmd(host, port, 
            "ls -la /root/models/ltx-2.3-22b-dev.safetensors 2>&1; ls /root/models/text_encoder/config.json 2>&1")
        log(f"Model check: {out.strip()[:200]}")
        
        return True
        
    except Exception as e:
        log(f"ERROR: {e}")
        return False

# Run setup on all VMs in parallel
print(f"\n=== Deploying to {n_vms} VMs ===")
print(f"Total remaining clips: {len(missing)}")
for vm_id, a in sorted(assignments.items(), key=lambda x: x[1]['vm']['label']):
    vm = a['vm']
    clips = a['clips']
    print(f"  {vm['label']}: {len(clips)} clips [{clips[0]}-{clips[-1]}]")

print(f"\nStarting parallel setup...")

threads = []
results = {}

for vm_id, assignment in assignments.items():
    def worker(vid=vm_id, a=assignment):
        results[vid] = setup_vm(vid, a)
    t = threading.Thread(target=worker)
    threads.append(t)
    t.start()

# Wait for all
for t in threads:
    t.join()

print(f"\n=== Setup Results ===")
success = sum(1 for v in results.values() if v)
print(f"{success}/{n_vms} VMs setup successfully")
for vm_id, ok in results.items():
    label = assignments[vm_id]['vm']['label']
    print(f"  {label}: {'✓' if ok else '✗'}")
