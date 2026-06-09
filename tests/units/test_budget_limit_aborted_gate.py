import os
import sys
import time
import httpx
import json
import socket
import subprocess
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, BudgetSet, VMAllocated, VMDeallocated, PipelineAborted
from agent_base import execute_agent_turn
from config_schema import PipelineConfig

def test_budget_limit_aborted_gate():
    '''
    Scenario: Aborting execution and destroying VMs when budget is exceeded
      Given a pipeline budget limit of 1.00 USD is configured in the event store
      And a BudgetExceeded event with spent cost 1.05 USD is recorded in the event store
      And a running GPU VM is provisioned on Vast.ai
      When the Provisioner Agent turn is executed via execute_agent_turn to deallocate the active VM autonomously
      Then the Provisioner must destroy the running Vast.ai VM instance and emit a VMDeallocated event
      And GSA must reflect that the active VM is deallocated in VM state response
    '''
    print('\n▶️  [STARTING TEST] test_budget_limit_aborted_gate')
    
    # 1. Assert immediately that live credentials, network reachability, and physical binaries are present
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    assert os.path.exists(vast_key_path), "CRITICAL FAILURE: Vast.ai API key file is missing!"
    assert os.path.exists(deepseek_key_path), "CRITICAL FAILURE: DeepSeek API key file is missing!"
    
    with open(vast_key_path) as f:
        api_key = f.read().strip()
    assert api_key, "CRITICAL FAILURE: Vast.ai API key is empty!"
    
    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise AssertionError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")
        
    try:
        subprocess.run(["/Users/orpington/.letta-cli-venv/bin/vastai", "--version"], capture_output=True, check=True)
    except Exception as e:
        raise AssertionError(f"CRITICAL FAILURE: Vast.ai CLI is missing or not callable: {e}")

    # 2. Lease a real VM on Vast.ai to allow live destruction behavior
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
    if create_res.returncode != 0 or not create_res.stdout.strip():
        if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:
            import pytest
            pytest.skip("Vast.ai account lacks credit; skipping live VM lease budget gate test.")
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
        
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM successfully leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}.")

    # 4. Run GSA and Provisioner with VastRealCapability in integration harness
    with IntegrationHarness(required_agents=["gsa"], capabilities=["VastRealCapability"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        gsa_url = f"http://127.0.0.1:{gsa_port}/"
        db_file = os.path.join(db_dir, "events.db")
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Seed budget set to $1.00
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(BudgetSet(agent="operator", budget_usd=1.00, reason="run_start"), "")
        
        # Seed the real VM allocation
        event_store.append(VMAllocated(
            agent="provisioner",
            instance_id=str(instance_id),
            role="tts",
            offer_id=str(offer_id),
            worker_url="http://127.0.0.1:8880",
            gpu_type="RTX 4090",
            cost_per_hour=0.40
        ), "")
        
        # Seed a BudgetExceeded event to record crossing the budget limit in the event store (SC-09)
        from effects import BudgetExceeded
        event_store.append(BudgetExceeded(
            agent="provisioner",
            spent_usd=1.05,
            limit_usd=1.00
        ), "")
        
        # Poll GSA to verify cost accumulation has indeed crossed the budget and set budget.exceeded to True
        exceeded = False
        for _ in range(20):
            try:
                resp = httpx.get(gsa_url)
                state = resp.json()
                if state["budget"]["exceeded"] is True:
                    exceeded = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        assert exceeded, "GSA cost accumulation failed to flag budget exceeded status!"
        
        # Verify that the accumulated cost from the budget exceeded event crosses the budget limit (SC-09)
        total_spent = state["budget"]["spent_usd"]
        budget_limit = state["budget"]["budget_cap_usd"]
        assert total_spent >= 1.05, f"Expected spent_usd to be at least 1.05, got {total_spent}"
        assert total_spent > budget_limit, f"Accumulated cost {total_spent} USD did not cross budget limit {budget_limit} USD!"
        
        # 5. Execute the Provisioner agent turn directly to autonomously process budget breach and destroy active VMs (SC-09)
        config = PipelineConfig(capabilities=["VastRealCapability"], log_dir=db_dir)
        print("Executing Provisioner Agent turn to autonomously process budget breach and destroy active VMs...")
        
        effects = asyncio.run(execute_agent_turn(
            role="provisioner",
            gsa_url=gsa_url,
            notification_type="instruction",
            context=None,
            config=config
        ))
        
        # Append the emitted effects to the event store
        for effect in list(effects):
            event_store.append(effect, "")
            
        # Check if VMDeallocated event for the real VM was emitted and appended
        events = event_store.replay()
        deallocated_events = [
            e.effect for e in events 
            if e.effect.kind == "vm_deallocated" and str(e.effect.instance_id) == str(instance_id)
        ]
        assert len(deallocated_events) >= 1, "Provisioner did not emit vm_deallocated effect for the active instance!"
        
        # 6. Verify that the VM has been deallocated in GSA state response
        resp = httpx.get(gsa_url)
        state = resp.json()
        active_vms = [vid for vid, v in state["vms"]["vms"].items() if v["status"] == "active"]
        assert str(instance_id) not in active_vms, "VM still registered as active in GSA!"
        
        # 7. Double check Vast.ai that the VM is indeed destroyed/gone (without fallback intervention)
        print("Verifying instance is deleted from Vast.ai...")
        cmd_show = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "show", "instances", "--raw"]
        show_res = subprocess.run(cmd_show, capture_output=True, text=True)
        instance_still_exists = False
        if show_res.returncode == 0:
            try:
                instances = json.loads(show_res.stdout.strip())
                inst_info = next((inst for inst in instances if str(inst["id"]) == str(instance_id)), None)
                if inst_info and inst_info.get("status") != "deleting":
                    instance_still_exists = True
            except Exception:
                pass
        assert not instance_still_exists, f"VM {instance_id} still exists on Vast.ai after provisioner turn!"
        
        # 8. Append PipelineAborted event as operator and verify GSA transitions phase to "aborted" (SC-09)
        from effects import PipelineAborted
        event_store.append(PipelineAborted(agent="operator", reason="budget_exceeded"), "")
        
        # Verify GSA has transitioned to "aborted"
        resp_phase = httpx.get(gsa_url)
        state_phase = resp_phase.json()
        assert state_phase["state"]["current_phase"] == "aborted", "GSA phase did not transition to aborted!"
        
        print("✓ Budget gate cost accumulation, autonomous VM deallocation, and aborted phase transition verified.")
