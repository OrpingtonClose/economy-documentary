import os
import sys
import time
import httpx
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, QueueVideoJob
from agent_base import execute_agent_turn
from config_schema import PipelineConfig

def test_video_agent_ltx_job_queueing():
    print('\n▶️  [STARTING TEST] test_video_agent_ltx_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")

    try:
        httpx.get("https://api.deepseek.com/", timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

    # Start real GSA service in integration harness
    with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        gsa_url = f"http://127.0.0.1:{gsa_port}/"
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Dopamine drives motivation.", duration_sec=3.0)
        ]
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Verify GSA is running and responding live
        gsa_check = httpx.get(gsa_url)
        assert gsa_check.status_code == 200
        
        # Execute the production agent turn directly to query GSA and DeepSeek (SC-06)
        config = PipelineConfig(capabilities=[], log_dir=db_dir)
        
        effects = asyncio.run(execute_agent_turn(
            role="video",
            gsa_url=gsa_url,
            notification_type="instruction",
            config=config
        ))
        
        # Append the emitted effects to the event store
        for effect in list(effects):
            event_store.append(effect, "")
            
        # Verify event store contains QueueVideoJob using the new split class
        events = event_store.replay()
        queue_video_events = [e.effect for e in events if e.effect.kind == "queue_video_job"]
        assert len(queue_video_events) >= 1
        qv_event = queue_video_events[0]
        assert isinstance(qv_event, QueueVideoJob)
        assert qv_event.slot_id.startswith("V1:")
        assert not hasattr(qv_event, "job_type")
        
        # Double check GSA state reflection
        gsa_state = httpx.get(gsa_url).json()
        assert len(gsa_state["jobs"]["jobs"]) >= 1
        print("✓ Video Agent LTX Job queueing verified via direct execute_agent_turn.")
