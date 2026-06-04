import os
import sys
import time
import pytest
from pathlib import Path
import httpx
import unittest.mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import execute_agent_turn

@pytest.mark.anyio
async def test_agent_search_tool_execution():
    # Make sure log dir exists
    os.makedirs("/tmp/documentary-pipeline", exist_ok=True)
    
    # Selective mock for GSA endpoint
    original_get = httpx.AsyncClient.get
    
    async def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        if "127.0.0.1:8000" in url_str or "127.0.0.1:8000" in url_str:
            return httpx.Response(
                200,
                json={
                    "otio": {"slots": {}},
                    "state": {"current_phase": "init"},
                    "jobs": {"jobs": {}}
                }
            )
        # Pass through to the real network for actual search tools and fetching
        return await original_get(self, url, *args, **kwargs)
    
    # Use patch to mock httpx.AsyncClient.get
    with unittest.mock.patch("httpx.AsyncClient.get", new=mock_get):
        # Trigger turn for scenario agent
        effects = await execute_agent_turn(
            role="scenario",
            gsa_url="http://127.0.0.1:8000/",
            context={"instruction": "Please search the web using search_web_brave for the capital of France and explain your findings in detail."}
        )

        
        # Verify the turn ran and produced a debug log file
        log_file = Path(f"/tmp/documentary-pipeline/agent_debug_scenario.log")
        assert log_file.exists()
        
        # Read the debug log file to verify the response contains details
        with open(log_file, "r") as f:
            content = f.read()
            
        # Check that the agent executed the Brave Search or referenced it in the log
        assert "Paris" in content or "Brave" in content
        print("\n=== Agent Debug Log Output ===")
        print(content[-1500:])

