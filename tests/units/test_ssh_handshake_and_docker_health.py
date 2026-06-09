import os
import sys
import time
import subprocess
import json
import socket
from pathlib import Path

def test_ssh_handshake_and_docker_health():
    print('\n▶️  [STARTING TEST] test_ssh_handshake_and_docker_health')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")

    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")

    # spot lease of GPU (SC-04)
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
    if create_res.returncode != 0:
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
    
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}. Output: {create_res.stdout}.")

    try:
        # Poll status until "running"
        print(f"Waiting for VM instance {instance_id} to boot...")
        start_time = time.time()
        ssh_host, ssh_port = None, None
        while time.time() - start_time < 300:
            cmd_show = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "show", "instances", "--raw"]
            show_res = subprocess.run(cmd_show, capture_output=True, text=True)
            if show_res.returncode == 0:
                try:
                    instances = json.loads(show_res.stdout.strip())
                    inst_info = next((inst for inst in instances if str(inst["id"]) == str(instance_id)), None)
                    if inst_info:
                        status = inst_info.get("status", "")
                        actual_status = inst_info.get("actual_status", "")
                        if status == "running" or actual_status == "running":
                            ssh_host = inst_info.get("ssh_host", "") or inst_info.get("ssh_ipaddr", "")
                            ssh_port = int(inst_info.get("ssh_port", 0) or 0)
                            if ssh_host and ssh_port:
                                break
                except Exception as e:
                    print(f"Error parsing show instances: {e}")
            time.sleep(5)
        
        assert ssh_host and ssh_port, "VM instance failed to reach running status with SSH port"
        
        # Verify SSH handshake and transfer the actual production worker agent (scripts/vm_agent.py) to VM
        print(f"Connecting to VM via SSH at {ssh_host}:{ssh_port}...")
        
        # Function to run SSH command
        def run_ssh(cmd_str, timeout=60):
            args = [
                "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "PasswordAuthentication=no", "-p", str(ssh_port), f"root@{ssh_host}",
                cmd_str
            ]
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

        # Poll until SSH is reachable
        ssh_ready = False
        for _ in range(30):
            res_ping = run_ssh("echo ready")
            if res_ping.returncode == 0 and "ready" in res_ping.stdout:
                ssh_ready = True
                break
            time.sleep(2)
        assert ssh_ready, "SSH port opened but handshake timed out"

        # Install actual production VM agent dependencies
        print("Installing FastAPI and uvicorn on VM...")
        run_ssh("apt-get update -y && apt-get install -y python3-pip")
        run_ssh("pip3 install fastapi uvicorn pydantic-ai")

        # Copy the actual production script scripts/vm_agent.py to the VM via SCP
        print("Copying actual scripts/vm_agent.py to remote VM...")
        local_agent_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "vm_agent.py")
        cmd_scp = [
            "scp", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PasswordAuthentication=no", "-P", str(ssh_port), local_agent_path, f"root@{ssh_host}:/workspace/vm_agent.py"
        ]
        scp_res = subprocess.run(cmd_scp, capture_output=True, text=True)
        assert scp_res.returncode == 0, f"SCP failed: {scp_res.stderr}"

        # Start the actual production agent inside the remote VM on port 8880
        print("Starting actual vm_agent.py on remote VM...")
        run_ssh("nohup python3 /workspace/vm_agent.py --port 8880 > /workspace/agent.log 2>&1 &")

        # Query local HTTP GET to worker URL inside the container (SC-04)
        ssh_success = False
        ssh_err = ""
        # Try up to 10 times for uvicorn server to start inside VM
        for _ in range(10):
            ssh_res = run_ssh("curl -i -s http://127.0.0.1:8880/")
            if ssh_res.returncode == 0:
                stdout = ssh_res.stdout
                assert "HTTP/1.1 200 OK" in stdout or "200" in stdout.split('\n')[0]
                assert "Content-Type: text/plain" in stdout or "content-type: text/plain" in stdout
                assert "healthy and active" in stdout
                ssh_success = True
                print("✓ SSH handshake and actual docker worker health verified successfully.")
                break
            else:
                ssh_err = ssh_res.stderr + "\n" + ssh_res.stdout
                time.sleep(3)
        assert ssh_success, f"Remote worker health check failed: {ssh_err}"
    finally:
        print(f"Destroying Vast.ai instance {instance_id}...")
        cmd_destroy = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "destroy", "instance", str(instance_id)]
        subprocess.run(cmd_destroy, capture_output=True)
        print("Teardown finished.")
