import os
import sys
import time
import httpx
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, QueueVideoJob, DurationAdjusted
from agent_base import execute_agent_turn
from config_schema import PipelineConfig

def test_video_agent_ltx_job_queueing():
    '''
    Scenario: Queueing video jobs via live LLM reasoning
      Given a script block is appended to the event store and GSA is running locally
      When the Video Agent is run via execute_agent_turn with the live DeepSeek API
      Then the agent must query the DeepSeek API and reflect its reasoning in latest_monologues
      And it must emit a QueueVideoJob event for the visual slot (V1:)
      And the event must not contain a job_type attribute
    '''
    print('\n▶️  [STARTING TEST] test_video_agent_ltx_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")

    try:
        httpx.get("https://api.deepseek.com/")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

    # Explicitly verify the live DeepSeek API connection and key functionality
    with open(deepseek_key_path) as f:
        api_key = f.read().strip()
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = httpx.post("https://api.deepseek.com/chat/completions", headers=headers, json={
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Respond with 'live_deepseek_connection_verified'"}],
        "max_tokens": 10
    })
    assert resp.status_code == 200, "DeepSeek live API request failed!"
    assert "live_deepseek_connection_verified" in resp.text.lower()

    # Start real GSA service in integration harness
    with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:
        assert "DryRunModel" not in harness.capabilities, "DryRunModel must not be present to ensure production DeepSeek execution!"
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
        event_store.append(DurationAdjusted(
            agent="audio",
            block_id="s1_b1",
            slot_id="A1:1:s1_b1",
            scene_num=1,
            voice_role="narrator",
            scripted_sec=3.0,
            measured_sec=3.0
        ), "")
        
        # Verify physical database interaction directly via sqlite3 to confirm it is not mocked
        db_file = os.path.join(db_dir, "events.db")
        assert os.path.exists(db_file), "Database file must exist physically on disk!"
        import sqlite3
        conn = sqlite3.connect(db_file)
        res = conn.execute("PRAGMA journal_mode").fetchone()
        assert res[0].lower() == "wal", "Database must be in WAL mode!"
        # Verify that script blocks were physically written to the event log table
        rows = conn.execute("SELECT kind, effect_json FROM events ORDER BY seq").fetchall()
        assert len(rows) >= 3, "Physical database must contain the seeded events!"
        conn.close()
        
        # Verify GSA is running and responding live
        gsa_check = httpx.get(gsa_url)
        assert gsa_check.status_code == 200
        
        # Verify GSA has slot "V1:1:s1_b1" in state "measured" (aligned with measured audio)
        gsa_state_before = gsa_check.json()
        assert gsa_state_before["otio"]["slots"]["V1:1:s1_b1"]["status"] == "measured"
        
        # Execute the production agent turn directly to query GSA and DeepSeek (SC-06)
        config = PipelineConfig(capabilities=[], log_dir=db_dir)
        
        import agent_base
        agent_base.latest_monologues.clear()
        
        effects = asyncio.run(execute_agent_turn(
            role="video",
            gsa_url=gsa_url,
            notification_type="instruction",
            config=config
        ))
        
        # Assert that the live DeepSeek API was queried and latest_monologues contains the reasoning
        assert "video" in agent_base.latest_monologues
        monologue = agent_base.latest_monologues["video"]
        assert monologue, "Reasoning monologue is empty!"
        assert monologue != "I am the video agent. Currently my status is healthy."
        assert not monologue.strip().startswith("effect:"), "Dry-run structured effect detected in monologue! Live DeepSeek model reasoning must be used."
        
        # Verify monologue is actually from DeepSeek API by checking for expected content structure (SC-06)
        assert any(w in monologue.lower() for w in ["dopamine", "video", "visual", "scene", "motivation", "ltx"]), "Reasoning in latest_monologues is not semantic or does not derive from the script!"
        
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
        
        # Double check GSA state reflection and verify the queued job details
        gsa_state_after = httpx.get(gsa_url).json()
        jobs_list = list(gsa_state_after["jobs"]["jobs"].values())
        matching_job = next((j for j in jobs_list if j.get("slot_id") == "V1:1:s1_b1" and j.get("job_type") == "ltx"), None)
        assert matching_job is not None, "GSA does not reflect the queued LTX job in state!"
        print("✓ Video Agent LTX Job queueing verified via direct execute_agent_turn.")
