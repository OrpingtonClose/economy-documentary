import os
import sys
import time
import pytest
import httpx
import subprocess
from pathlib import Path
from pytest_bdd import scenarios, given, when, then, parsers

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    UpdateScript,
    ScriptBlock,
    ReconciliationFailed,
    ReconciliationFailureDetail,
)
from event_store import EventStore

scenarios('features/real_self_correction.feature')

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
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"
        
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
        
        # Poll GSA startup
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start on host")

    def start_scenario_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"
        
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
        
        # Poll Agent startup
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)
                if resp.status_code == 200:
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
        
        import shutil
        try:
            # shutil.rmtree("/tmp/documentary-pipeline")
            pass
        except Exception:
            pass

@pytest.fixture
def scenario_helper():
    helper = HostScenarioHelper()
    helper.start_gsa()
    helper.start_scenario_agent()
    yield helper
    helper.cleanup()

@pytest.fixture
def event_store():
    return EventStore(log_dir="/tmp/documentary-pipeline")

def clear_local_event_store():
    import shutil
    db_dir = "/tmp/documentary-pipeline"
    try:
        shutil.rmtree(db_dir)
    except Exception:
        pass
    os.makedirs(db_dir, exist_ok=True)

@given("the GSA event store contains a failed audio reconciliation event for a slot")
def step_failed_reconciliation(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")
    
    # Create script block
    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text=(
            "Dopamine is not a pleasure chemical. That is a widespread misconception that has persisted for decades. "
            "In reality, dopamine is the chemical of anticipation, motivation, and the pursuit of rewards. It drives "
            "us to seek out new experiences, learn new things, and stay focused on goals. For ADHD brains, this reward "
            "system is regulated differently, making ordinary tasks feel far more difficult to start because the "
            "baseline dopamine level is lower."
        ),
        duration_sec=3.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")

    # Fail reconciliation
    detail = ReconciliationFailureDetail(
        block_id="s1_b1",
        scene_num=1,
        phrase_idx=0,
        voice="V1_Narrator",
        scripted_sec=3.0,
        measured_sec=4.16,
        delta_sec=1.16,
        ratio=1.387,
        message="measured 4.16s vs target 3.0s, delta 1.16s exceeds tolerance",
        attempt_number=1
    )
    event_store.append(
        ReconciliationFailed(
            agent="audio",
            blocks_total=1,
            blocks_passed=0,
            blocks_failed=1,
            failures=[detail],
            worst_delta_sec=1.16,
            failure_type="duration_mismatch"
        ),
        "initial_hash"
    )

@given("the Scenario Agent is running on the host")
def step_scenario_agent_running(scenario_helper):
    pass

@when("the Scenario Agent receives a wakeup instruction")
def step_wake_scenario(scenario_helper):
    # Remove timeout from POST to Scenario Agent for architectural compliance
    resp = httpx.post(f"http://localhost:{scenario_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then("the Scenario Agent should read the event log and detect the duration failure")
def step_detect_failure():
    pass

@then("it should rewrite the script block text to be shorter")
def step_rewrite_shorter():
    pass

@then('it should append a revised "update_script" effect to the GSA')
def step_check_revised_script(event_store):
    delay = 2.0
    for _ in range(120):
        effects = [e.effect for e in event_store.read_all()]
        scenario_scripts = [e for e in effects if e.kind == "update_script" and e.agent == "scenario"]
        if len(scenario_scripts) >= 2:
            original_text = scenario_scripts[0].blocks[0].text
            revised_block = scenario_scripts[-1].blocks[0]
            assert len(revised_block.text) < len(original_text)
            return
        time.sleep(delay)
    raise AssertionError("Scenario agent did not append a revised update_script")
