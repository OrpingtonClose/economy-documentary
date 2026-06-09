import os

ts_path = "/Users/orpington/Documents/economy-documentary-work/tests/units/test_scenario_agent_live_prompt_turn.py"

with open(ts_path, "r", encoding="utf-8") as f:
    content = f.read()

target = 'assert len(gsa_resp["otio"]["slots"]) >= 1'
replacement = """assert len(gsa_resp["otio"]["slots"]) >= 1
        
        # Verify event store contains UpdateScript and check its blocks
        events = event_store.replay()
        update_script_events = [e for e in events if e.kind == "update_script"]
        assert len(update_script_events) >= 1
        us_event = update_script_events[0]
        assert len(us_event.blocks) >= 1"""

if target in content:
    content = content.replace(target, replacement)
    with open(ts_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("Successfully updated test_scenario_agent_live_prompt_turn.py")
else:
    print("Could not find target in test_scenario_agent_live_prompt_turn.py")
