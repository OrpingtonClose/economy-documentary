import sys
from pathlib import Path

# Add tests/mocks directory to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from mock_base import create_mock_agent_app
from effects import PipelineComplete

async def assembly_turn(run_id, state, event_store):
    slots = state.get("otio", {}).get("slots", {})
    current_phase = state.get("state", {}).get("current_phase", "init")
    all_delivered = len(slots) > 0 and all(s["status"] == "delivered" for s in slots.values())
    
    if all_delivered and current_phase != "done":
        return PipelineComplete(
            run_id=run_id,
            agent="assembly",
            output_path="/tmp/final_documentary.mp4",
            duration_sec=6.8
        )
    return None

app = create_mock_agent_app("assembly", assembly_turn)
