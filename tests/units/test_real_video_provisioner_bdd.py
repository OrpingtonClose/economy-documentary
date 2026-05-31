"import os
import sys
import time
import pytest
import httpx
import subprocess
from pathlib import Path
from pytest_bdd import scenarios, given, when, then

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    QueueJob,
    VMAllocated,
    VMDeallocated,
    JobCompleted,
)
from event_store import EventStore

scenarios('features/real_video_provisioner.feature')

class HostVideoHelper:
    def __init__(self, agent_port: int = 8003):
        self.agent_port = agent_port
        self.gsa_process = None
        self.agent_process = None
        self.api_key = None
        self.allocated_instance_ids = []

        # Read VAST_API_KEY from server/.env
        env_path = PROJECT_ROOT / "server" / ".env"
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    if line.startswith("VAST_API_KEY="):
                        self.api_key = line.split("=")[1].strip()

    def start_gsa(self):
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/Users/orpington/documentary-pipeline"

        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        # Wait for GSA
        for _ in range(15):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("GSA failed to start on h
<truncated 8807 bytes>