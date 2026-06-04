import os
import sys
import asyncio
import pytest
import httpx
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import make_agent_app, bash_command

@pytest.mark.anyio
async def test_post_handler_rejects_with_409_when_busy():
    import shutil
    try:
        shutil.rmtree("/tmp/documentary-pipeline")
    except Exception:
        pass  # directory deletion error ignored
    os.makedirs("/tmp/documentary-pipeline", exist_ok=True)

    from agent_base import event_store
    event_store._init_db()

    app = make_agent_app("test_agent")

    # Mock execute_agent_turn to sleep, keeping the task active/running
    async def mock_execute_agent_turn(*args, **kwargs):
        await asyncio.sleep(2)
        return [], "mocked response"

    with patch("agent_base.execute_agent_turn", side_effect=mock_execute_agent_turn):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Send first POST request (this will run asynchronously and block for 2 seconds)
            task1 = asyncio.create_task(client.post("/", content="First Prompt"))
            
            # Yield control to let fastapi process the first request and set active_task
            await asyncio.sleep(0.2)

            # Send second POST request (this should immediately see the active task running and return 409)
            resp2 = await client.post("/", content="Second Prompt")
            assert resp2.status_code == 409
            assert resp2.text == "Agent is busy"

            # Clean up task1
            await task1

@pytest.mark.anyio
async def test_bash_command_cancels_process_group():
    captured_proc = None
    original_create_subprocess_shell = asyncio.create_subprocess_shell

    async def mock_create_subprocess_shell(*args, **kwargs):
        nonlocal captured_proc
        proc = await original_create_subprocess_shell(*args, **kwargs)
        captured_proc = proc
        return proc

    with patch("asyncio.create_subprocess_shell", side_effect=mock_create_subprocess_shell):
        task = asyncio.create_task(bash_command(None, "sleep 10"))
        
        # Let the process start
        await asyncio.sleep(0.5)
        
        # Cancel the task
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Check that the process and its process group are dead
        assert captured_proc is not None
        
        # Give the OS a tiny moment to clean up process resources
        await asyncio.sleep(0.2)
        
        try:
            pgid = os.getpgid(captured_proc.pid)
            # Try to send signal 0 to the process group. If the process group still exists,
            # this will not raise an error. If it is dead, it will raise ProcessLookupError.
            os.killpg(pgid, 0)
            assert False, "Process group still exists, but it should have been killed"
        except ProcessLookupError:
            # This is expected since the process group was killed
            pass  # Process group was killed successfully
