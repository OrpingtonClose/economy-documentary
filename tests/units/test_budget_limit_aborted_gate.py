import os
import sys
import time
import httpx
import json
import socket
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, BudgetSet, VMAllocated, VMDeallocated

def test_budget_limit_aborted_gate():
    print('\n▶️  [STARTING TEST] test_budget_limit_aborted_gate')
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

    # 1. Lease a real VM on Vast.ai to allow live destruction behavior
    cmd_search = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers", "rentable=true num_gpus=1", "-o", "price", "--raw"]
    res = subprocess.run(cmd_search, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("CRITICAL FAILURE: Failed to fetch search offers from Vast.ai API.")
        
    try:
        offers = json.loads(res.stdout.strip())
        offer_id = offers[0]["id"]
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")

    print(f"Renting spot VM offer {offer_id} for budget test...")
    cmd_create = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "create", "instance", str(offer_id), "--image", "ubuntu:22.04", "--disk", "10", "--raw"]
    create_res = subprocess.run(cmd_create, capture_output=True, text=True)
    if create_res.returncode != 0:
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
    
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM successfully leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}.")

    try:
        # 2. Run GSA and Provisioner with real boundaries (capabilities=[])
        with IntegrationHarness(required_agents=["gsa", "provisioner"], capabilities=[]) as harness:
            db_dir = harness.temp_dir.name
            gsa_port = harness.ports["gsa"]
            
            event_store = EventStore(log_dir=db_dir)
            event_store._init_db()
            
            # Seed budget set to $0.01 (extremely low limit to force cost cap violation)
            event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
            event_store.append(BudgetSet(agent="operator", budget_usd=0.01, reason="run_start"), "")
            
            # Seed the real VM allocation (so the Provisioner agent knows it exists and is active)
            event_store.append(VMAllocated(
                agent="provisioner",
                instance_id=str(instance_id),
                role="tts",
                offer_id=str(offer_id),
                worker_url="http://127.0.0.1:8880",
                gpu_type="RTX 4090",
                cost_per_hour=0.40
            ), "")
            
            # Exercise the cost accumulation logic (Condition 5) by seeding a deallocated VM with $0.02 cost
            # Cumulative spent_usd becomes $0.02, which is > budget_cap_usd ($0.01)
            event_store.append(VMDeallocated(
                agent="provisioner",
                instance_id="dummy_vm",
                reason="job_done",
                final_cost=0.02,
                runtime_sec=180.0
            ), "")
            
            # Poll GSA to verify cost accumulation has indeed crossed the budget and set budget.exceeded to True
            exceeded = False
            for _ in range(20):
                try:
                    resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
                    state = resp.json()
                    if state["budget"]["exceeded"] is True:
                        exceeded = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            assert exceeded, "GSA cost accumulation failed to flag budget exceeded status!"
            
            # Do NOT send manual wakeup POST. Allow the Provisioner's autonomous background loop
            # to poll GSA, detect the budget violation, and destroy the VM automatically!
            destroyed = False
            start_poll = time.time()
            while time.time() - start_poll < 60:  # 60s timeout
                events = event_store.replay()
                deallocated_events = [
                    e.effect for e in events 
                    if e.effect.kind == "vm_deallocated" and str(e.effect.instance_id) == str(instance_id)
                ]
                if len(deallocated_events) >= 1:
                    destroyed = True
                    break
                time.sleep(1)
                
            assert destroyed, "Provisioner background loop failed to automatically detect budget violation and destroy the VM!"
            print("✓ Budget gate cost accumulation and autonomous VM deallocation verified.")
    finally:
        # Cleanup
        print(f"Ensuring Vast.ai instance {instance_id} is destroyed...")
        cmd_destroy = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "destroy", "instance", str(instance_id)]
        subprocess.run(cmd_destroy, capture_output=True)
        print("Teardown complete.")
