import sys
from pathlib import Path

# Add tests/mocks directory to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from mock_base import create_mock_agent_app
from effects import UpdateScript, ScriptBlock

async def scenario_turn(run_id, state, event_store):
    slots = state.get("otio", {}).get("slots", {})
    if not slots:
        return UpdateScript(
            run_id=run_id,
            agent="scenario",
            blocks=[
                ScriptBlock(
                    scene_num=1,
                    block_id="intro",
                    speaker="V1",
                    text="Today we look at the economy and its documentaries.",
                    duration_sec=6.5
                )
            ]
        )
    return None

app = create_mock_agent_app("scenario", scenario_turn)
