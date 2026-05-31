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
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/Users/orpington/documentary-pipeline"
        
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
        for _ in range(30):
            try:
                resp = httpx.get("http://localhost:8000/runs/health_probe", timeout=1.0)
                if resp.status_code in (200, 400, 500):
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
        for _ in range(30):
            try:
                resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("Scenario agent failed to start on host")

    def cleanup(self):
        if self.agent_process:
            self.agent_process.terminate()
            self.agent_process.wait()
        if self.gsa_process:
            self.gsa_process.terminate()
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
    return EventStore(log_dir="/Users/orpington/documentary-pipeline")

def clear_local_event_store():
    import glob
    for file in glob.glob("/Users/orpington/documentary-pipeline/events_*.db*"):
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
def step_receive_instruction(run_id, instruction, event_store):
    event_store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    event_store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    
    resp = httpx.post("http://localhost:8001/", json={
        "run_id": run_id,
        "notification_type": "instruction",
        "context": {"instruction": instruction}
    }, timeout=60.0)
    assert resp.status_code == 200
    
    start_time = time.time()
    while time.time() - start_time < 60.0:
        effects = [e.effect for e in event_store.read_all(run_id)]
        updates = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
        if updates:
            return
        time.sleep(1.0)
    raise TimeoutError("Scenario agent timed out producing update_script")

@then('the GSA event store should contain an "update_script" effect with the generated text')
def step_check_update_script(run_id, event_store):
    effects = [e.effect for e in event_store.read_all(run_id)]
    updates = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
    assert len(updates) >= 1
    assert len(updates[0].blocks) > 0

@then("the scenario script must contain valid dialogue and visual prompts for all 3 slots")
def step_check_slots(run_id, event_store):
    effects = [e.effect for e in event_store.read_all(run_id)]
    updates = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
    blocks = updates[0].blocks
    assert len(blocks) >= 3
    for b in blocks:
        assert b.text
        assert b.speaker

@then("the OTIO timeline in the GSA should be updated with the script blocks")
def step_check_otio(run_id):
    resp = httpx.get(f"http://localhost:8000/runs/{run_id}", timeout=5.0)
    assert resp.status_code == 200
    state = resp.json()
    assert state.get("otio")
    assert len(state["otio"].get("slots", {})) >= 3