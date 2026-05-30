import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock
import shutil
import tempfile
import time

from agent_base import make_agent_app, AgentPayload
import agent_base
from effects import UpdateScript, ScriptBlock, NoOp
from event_store import EventStore


@pytest.fixture
def temp_log_dir():
    temp_dir = tempfile.mkdtemp(prefix="agent_base_test_")
    yield temp_dir
    shutil.rmtree(temp_dir)


def test_agent_health_endpoint(temp_log_dir):
    # Swap out logs directory
    agent_base.event_store = EventStore(log_dir=temp_log_dir)

    app = make_agent_app("scenario")
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["agent"] == "scenario"
    assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_agent_post_endpoint(temp_log_dir):
    agent_base.event_store = EventStore(log_dir=temp_log_dir)

    app = make_agent_app("scenario")
    client = TestClient(app)

    run_id = "test_agent_run_1"

    # Mock the execute_agent_turn function so we don't call the actual LLM / DeepSeek API
    mock_effects = [
        UpdateScript(
            run_id=run_id,
            agent="scenario",
            blocks=[
                ScriptBlock(
                    scene_num=1,
                    block_id="b1",
                    speaker="narrator",
                    text="Scaffold test text",
                    duration_sec=5.0,
                )
            ],
        )
    ]

    with patch("agent_base.execute_agent_turn", new_callable=AsyncMock) as mock_turn:
        mock_turn.return_value = mock_effects

        payload = {
            "run_id": run_id,
            "notification_type": "instruction",
            "context": {"slot_id": "A1:1:b1"},
        }

        response = client.post("/", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["effects_extracted"] == ["update_script"]
        assert data["agent"] == "scenario"

        mock_turn.assert_called_once_with(
            run_id=run_id,
            role="scenario",
            gsa_url="http://localhost:8000/",
            notification_type="instruction",
            context={"slot_id": "A1:1:b1"},
        )
