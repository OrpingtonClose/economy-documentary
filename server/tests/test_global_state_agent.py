import pytest
from fastapi.testclient import TestClient
import shutil
import tempfile
from pathlib import Path

from global_state_agent import app, build_global_state
import global_state_agent
from effects import PipelineStarted, UpdateScript, ScriptBlock, BudgetSet
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
    event_start = PipelineStarted(
        run_id=run_id,
        agent="scenario",
    )
    global_state_agent.event_store.append(run_id, event_start, otio_hash_before="")

    event_budget = BudgetSet(
        run_id=run_id,
        agent="scenario",
        budget_usd=10.0,
        reason="run_start",
    )
    global_state_agent.event_store.append(run_id, event_budget, otio_hash_before="")

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
    assert data["latest_sequence"] == 3
    assert data["budget"]["budget_cap_usd"] == 10.0

    assert data["otio"]["total_slots"] == 1
    assert "A1:1:intro" in data["otio"]["slots"]
    assert data["otio"]["slots"]["A1:1:intro"]["text"] == "Hello world narration"
    assert data["otio"]["slots"]["A1:1:intro"]["status"] == "scripted"

    # Test bad request without run_id
    response = client.get("/")
    assert response.status_code == 400
    assert "detail" in response.json()

    # Test using X-Run-ID header
    response = client.get("/", headers={"X-Run-ID": run_id})
    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
