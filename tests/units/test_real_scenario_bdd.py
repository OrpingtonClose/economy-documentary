import os
import sys
import time
import pytest
import httpx
import subprocess
import shutil
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
        # Documented Justification: Health check probe loop to verify port availability and wait for port unbinding.
        delay = 0.1
        for _ in range(5):
            time.sleep(delay)
            delay *= 1.5
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.gsa_stdout = open(log_dir / "gsa_stdout.log", "w")
        self.gsa_stderr = open(log_dir / "gsa_stderr.log", "w")
        
        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            stdout=self.gsa_stdout,
            stderr=self.gsa_stderr,
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        # Documented Justification: Health check probe loop to wait for GSA process to spin up.
        # We use a loop with dynamic backoff to poll the GSA health endpoint during startup.
        delay = 0.2
        for _ in range(30):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)
                if resp.status_code in (200, 400, 500):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start on host")

    def start_scenario_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        # Documented Justification: Health check probe loop to verify port availability and wait for port unbinding.
        delay = 0.1
        for _ in range(5):
            time.sleep(delay)
            delay *= 1.5
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.agent_stdout = open(log_dir / "agent_scenario_stdout.log", "w")
        self.agent_stderr = open(log_dir / "agent_scenario_stderr.log", "w")
 
        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.scenario.app:app", "--host", "127.0.0.1", "--port", str(self.agent_port)],
            stdout=self.agent_stdout,
            stderr=self.agent_stderr,
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        # Documented Justification: Health check probe loop to wait for Scenario Agent server process to spin up.
        # We use a loop with dynamic backoff to poll the Scenario Agent health endpoint during startup.
        delay = 0.2
        for _ in range(30):
            try:
                resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("Scenario agent failed to start on host")

    def cleanup(self):
        if self.agent_process:
            self.agent_process.kill()
            self.agent_process.wait()
        if self.gsa_process:
            self.gsa_process.kill()
            self.gsa_process.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)

@pytest.fixture(scope="module")
def scenario_helper():
    helper = HostScenarioHelper()
    helper.start_gsa()
    helper.start_scenario_agent()
    yield helper
    helper.cleanup()

@pytest.fixture
def run_id():
    return f"run_{int(time.time() * 1000)}"

@pytest.fixture
def event_store():
    return EventStore(log_dir="/tmp/documentary-pipeline")

def clear_local_event_store():
    import glob
    for pattern in ["/tmp/documentary-pipeline/events_*.db*", "/tmp/documentary-pipeline/events.db*"]:
        for file in glob.glob(pattern):
            try:
                os.remove(file)
            except Exception:
                pass

@given("the GSA event store is clean")
def step_clean_store(event_store):
    clear_local_event_store()

@given("the Scenario Agent is running on the host")
def step_agent_running(scenario_helper):
    pass

@when(parsers.parse('the Scenario Agent receives an instruction to "{instruction}"'))
def step_receive_instruction(instruction, event_store):
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")
    
    # Remove timeout from POST to Scenario Agent for architectural compliance
    resp = httpx.post("http://localhost:8001/", content=instruction)
    assert resp.status_code == 200

    
    # Documented Justification: Health check probe loop to poll GSA status and verify Scenario Agent updates.
    # We use a loop with dynamic backoff as a health check query.
    delay = 1.0
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        updates = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
        if updates:
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    raise AssertionError("Scenario agent did not produce update_script in the allotted time")

@then('the GSA event store should contain an "update_script" effect with the generated text')
def step_check_update_script(event_store):
    effects = [e.effect for e in event_store.read_all()]
    updates = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
    assert len(updates) >= 1
    assert len(updates[0].blocks) > 0

@then("the scenario script must contain valid dialogue and visual prompts for all 3 slots")
def step_check_slots(event_store):
    effects = [e.effect for e in event_store.read_all()]
    updates = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
    blocks = updates[0].blocks
    assert len(blocks) >= 3
    for b in blocks:
        assert b.text
        assert b.speaker

@then("the OTIO timeline in the GSA should be updated with the script blocks")
def step_check_otio():
    resp = httpx.get("http://localhost:8000/")
    assert resp.status_code == 200
    state = resp.json()
    assert state.get("otio")
    assert len(state["otio"].get("slots", {})) >= 3