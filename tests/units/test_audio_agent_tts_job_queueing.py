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
from effects import PipelineStarted, UpdateScript, ScriptBlock, QueueAudioJob
from agent_base import execute_agent_turn
from config_schema import PipelineConfig

def test_audio_agent_tts_job_queueing():
    '''
    Scenario: Queueing narration jobs via live LLM reasoning
      Given a script block is appended to the event store and GSA is running locally
      When the Audio Agent is run via execute_agent_turn with the live DeepSeek API
      Then the agent must query the DeepSeek API and reflect its reasoning in latest_monologues
      And it must emit a QueueAudioJob event for the narration slot (A1:)
      And the event must not contain a job_type attribute
    '''
    print('
▶️  [STARTING TEST] test_audio_agent_tts_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")

    try:
        httpx.get("https://api.deepseek.com/", timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

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
        
        # Verify GSA is running and responding live
        gsa_check = httpx.get(gsa_url)
        assert gsa_check.status_code == 200
        
        # Verify GSA has slot "A1:1:s1_b1" in state "scripted"
        gsa_state_before = gsa_check.json()
        assert gsa_state_before["otio"]["slots"]["A1:1:s1_b1"]["status"] == "scripted"
        
        # Execute the production agent turn directly to query GSA and DeepSeek (SC-05)
        config = PipelineConfig(capabilities=[], log_dir=db_dir)
        
        import agent_base
        agent_base.latest_monologues.clear()
        
        # Setup spy to track live HTTP requests to DeepSeek API (Condition 3 spy)
        original_send = httpx.AsyncClient.send
        called_deepseek = []
        
        async def spy_send(self, request, *args, **kwargs):
            if "deepseek.com" in str(request.url):
                called_deepseek.append(request)
            return await original_send(self, request, *args, **kwargs)
            
        httpx.AsyncClient.send = spy_send
        
        try:
            effects = asyncio.run(execute_agent_turn(
                role="audio",
                gsa_url=gsa_url,
                notification_type="instruction",
                config=config
            ))
        finally:
            httpx.AsyncClient.send = original_send
            
        # Assert that the live DeepSeek API was actually contacted during execute_agent_turn (Condition 3 spy)
        assert len(called_deepseek) >= 1, "The live DeepSeek API was not contacted during execute_agent_turn!"
        
        # Assert that the live DeepSeek API was queried and latest_monologues contains the reasoning
        assert "audio" in agent_base.latest_monologues
        monologue = agent_base.latest_monologues["audio"]
        assert monologue, "Reasoning monologue is empty!"
        assert monologue != "I am the audio agent. Currently my status is healthy."
        assert not monologue.strip().startswith("effect:"), "Dry-run structured effect detected in monologue! Live DeepSeek model reasoning must be used."
        
        # Verify monologue is actually from DeepSeek API by checking for expected content structure (SC-05)
        assert any(w in monologue.lower() for w in ["dopamine", "audio", "voice", "narrator", "tts", "speak"]), "Reasoning in latest_monologues is not semantic or does not derive from the script!"
        
        # Append the emitted effects to the event store
        for effect in list(effects):
            event_store.append(effect, "")
            
        # Verify event store contains QueueAudioJob using the new split class
        events = event_store.replay()
        queue_audio_events = [e.effect for e in events if e.effect.kind == "queue_audio_job"]
        assert len(queue_audio_events) >= 1
        qa_event = queue_audio_events[0]
        assert isinstance(qa_event, QueueAudioJob)
        assert qa_event.slot_id.startswith("A1:")
        assert not hasattr(qa_event, "job_type")
        
        # Double check GSA state reflection and verify the queued job details
        gsa_state_after = httpx.get(gsa_url).json()
        jobs_list = list(gsa_state_after["jobs"]["jobs"].values())
        matching_job = next((j for j in jobs_list if j.get("slot_id") == "A1:1:s1_b1" and j.get("job_type") == "tts"), None)
        assert matching_job is not None, "GSA does not reflect the queued TTS job in state!"
        print("✓ Audio Agent TTS Job queueing verified via direct execute_agent_turn.")
