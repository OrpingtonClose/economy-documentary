"import pytest
from fastapi.testclient import TestClient
import shutil
import tempfile
from pathlib import Path

from global_state_agent import app, build_global_state
import global_state_agent
from effects import PipelineStarted, UpdateScript, ScriptBlock
from event_store import EventStore


@pytest.fixture
def temp_log_dir():
    # Use a temporary directory for each test run to avoid DB conflicts
    temp_dir = tempfile.mkdtemp(prefix="gsa_test_")
    yield temp_dir
    shutil.rmtree(temp_dir)


def test_gsa_get_state(temp_log_dir):
    # Swap out log directory in global_state_agent to use the temporary one
    global_state_agent.event_store = EventStore(log_dir=temp_log_dir)

    run_id = "test_run_gsa_1"
    client = TestClient(app)

    # 1. Access before any event exists should return empty default state
    response = client.get(f"/?run_id={run_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == run_id
    assert data["otio"]["total_slots"] == 0
    assert data["latest_sequence"] == 0

    # 2. Inject some events to event store directly
    event1 = PipelineStarted(
        run_id=run_id,
        agent="scenario",
        max_run_budget_usd=15.0,
    )
    global_state_agent.event_store.append(run_id, event1, otio_hash_before="")

    event2 = UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[
            ScriptBlock(
                scene_num=1,
                block_id="intro",
                speaker="V1",
                text="Hello world narration",
                duration_sec=8.5,
            )
        ],
    )
    # Rebuild hash simulation
    global_state_agent.event_store.append(run_id, event2, otio_hash_before="hash1")

    # 3. Query GSA again and check replay
    response = client.get(f"/?run_id={run_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["latest_sequence"] == 2
    assert d
<truncated 729 bytes>