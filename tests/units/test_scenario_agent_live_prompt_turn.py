import os
import sys
import httpx
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock

def test_scenario_agent_live_prompt_turn():
    print('\n▶️  [STARTING TEST] test_scenario_agent_live_prompt_turn')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")

    try:
        httpx.get("https://api.deepseek.com/", timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

    # Initialize IntegrationHarness with production model (capabilities=[] to exclude DryRunModel)
    with IntegrationHarness(required_agents=["gsa", "scenario"], capabilities=[]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        scenario_port = harness.ports["scenario"]
        
        # Assert harness is initialized with the production model capability (i.e. no DryRunModel simulator)
        assert "DryRunModel" not in harness.capabilities, "DryRunModel must not be present to ensure production DeepSeek execution!"
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        
        # Prompt Scenario Agent to partition a short text into blocks about global interest rates
        prompt = "Create a script with 2 blocks about global interest rates."
        
        # Perform HTTP POST request to scenario agent (live boundary LLM reasoning query)
        resp = httpx.post(f"http://127.0.0.1:{scenario_port}/", content=prompt, timeout=None)
        assert resp.status_code == 200
        
        # Verify the response is not the default dry run monologue text (proving live DeepSeek call)
        assert "Dopamine drives motivation" not in resp.text, "Dry-run response detected! Live DeepSeek model must be used."
        
        # Check that response reflects reasoning/topics from our interest rate prompt
        assert any(word in resp.text.lower() for word in ["interest", "rate", "global", "economy", "bank", "central"]), f"Agent response did not reflect prompt reasoning: {resp.text}"
        
        # Verify that the response was parsed into valid ScriptBlock models and appended to EventStore as UpdateScript
        events = event_store.replay()
        update_script_events = [e.effect for e in events if e.effect.kind == "update_script"]
        assert len(update_script_events) >= 1
        us_event = update_script_events[0]
        
        assert isinstance(us_event, UpdateScript)
        assert len(us_event.blocks) >= 1
        
        # Assert each block is a valid ScriptBlock instance conforming to schema specifications (SC-01)
        for block in us_event.blocks:
            assert isinstance(block, ScriptBlock)
            assert block.scene_num >= 1
            assert len(block.block_id) > 0
            assert len(block.speaker) > 0
            assert len(block.text) > 0
            assert block.duration_sec > 0.0
            
        print("✓ Scenario Agent live prompt turn and ScriptBlock model validation verified.")
