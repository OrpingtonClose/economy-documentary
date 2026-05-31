"import os
import sys
import time
import pytest
import httpx
import subprocess
from pathlib import Path
from pytest_bdd import scenarios, given, when, then, parsers

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import PipelineStarted, BudgetSet, UpdateScript, ScriptBlock
from event_store import EventStore

scenarios('features/real_scenario_generation.feature')

class HostScenarioHelper:
    def __init__(self, agent_port: int = 8001):
        self.agent_port = agent_port
        self.gsa_process = None
        self.agent_process = None

    def start_gsa(self):
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/Users/orpington/documentary-pipeline"
        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / "server/.venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        for _ in range(15):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("GSA failed to start on host")

    def start_scenario_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/Users/orpington/documentary-pipeline"
        
        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / "server/.venv/bin/uvicorn"), "agents.sc
<truncated 3777 bytes>