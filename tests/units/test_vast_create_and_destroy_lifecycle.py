import os
import sys
import time
import subprocess
import json
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from models.vm_state import VMState

def test_vast_create_and_destroy_lifecycle():
    print('\n▶️  [STARTING TEST] test_vast_create_and_destroy_lifecycle')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")

    try:
        socket.create_connection(("vast.ai", 80))
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")
        
    # Find cheapest offer
    cmd_search = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers", "rentable=true num_gpus=1", "-o", "price", "--raw"]
    res = subprocess.run(cmd_search, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("CRITICAL FAILURE: Failed to fetch search offers from Vast.ai API.")
        
    try:
        offers = json.loads(res.stdout.strip())
        offer_id = offers[0]["id"]
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")
        
    print(f"Renting cheapest Vast.ai offer: {offer_id}")
    cmd_create = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "create", "instance", str(offer_id), "--image", "ubuntu:22.04", "--disk", "10", "--raw"]
    create_res = subprocess.run(cmd_create, capture_output=True, text=True)
    if create_res.returncode != 0 or not create_res.stdout.strip():
        if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:
            raise RuntimeError("Vast.ai account lacks credit; aborting live VM lease lifecycle test.")
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
    
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM successfully leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}. Output: {create_res.stdout}.")
        
    try:
        # Poll status until "running"
        print(f"Waiting for VM instance {instance_id} to boot...")
        start_time = time.time()
        booted = False
        while True:
            cmd_show = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "show", "instances", "--raw"]
            show_res = subprocess.run(cmd_show, capture_output=True, text=True)
            if show_res.returncode == 0:
                try:
                    instances = json.loads(show_res.stdout.strip())
                    inst_info = next((inst for inst in instances if str(inst["id"]) == str(instance_id)), None)
                    if inst_info:
                        status = inst_info.get("status", "")
                        actual_status = inst_info.get("actual_status", "")
                        print(f"VM status: {status}, actual_status: {actual_status}")
                        if status == "running" or actual_status == "running":
                            booted = True
                            
                            # Parse connection details and validate using production VMState model
                            ssh_host = inst_info.get("ssh_host", "") or inst_info.get("ssh_ipaddr", "") or "127.0.0.1"
                            ssh_port = int(inst_info.get("ssh_port", 0) or 0)
                            gpu_name = inst_info.get("gpu_name", "")
                            vram_gb = float(inst_info.get("gpu_ram", 0.0) or 0.0)
                            price_per_hour = float(inst_info.get("dph_total", 0.0) or 0.0)
                            disk_gb = float(inst_info.get("disk_space", 0.0) or 0.0)
                            
                            vm_state_obj = VMState(
                                instance_id=str(instance_id),
                                status="running",
                                ssh_host=ssh_host,
                                ssh_port=ssh_port,
                                gpu_name=gpu_name,
                                vram_gb=vram_gb,
                                price_per_hour=price_per_hour,
                                disk_gb=disk_gb,
                                worker_url=f"http://{ssh_host}:{ssh_port}"
                            )
                            assert vm_state_obj.instance_id == str(instance_id)
                            assert vm_state_obj.status == "running"
                            break
                except Exception as e:
                    print(f"Error parsing show instances: {e}")
            time.sleep(5)
        assert booted, "VM instance failed to reach running status in time"
    finally:
        print(f"Cleaning up and destroying Vast.ai instance {instance_id}...")
        cmd_destroy = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "destroy", "instance", str(instance_id)]
        subprocess.run(cmd_destroy, capture_output=True)
        print("Teardown finished.")
