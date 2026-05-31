"import os
import sys
import time
import httpx
import logging
from pathlib import Path
from vm_test_helper import VMTestHelper

# Setup python path to import server modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    UpdateScript,
    ScriptBlock,
    QueueJob,
    JobCompleted,
    JobRequeued,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestAudio")

def get_gsa_events(run_id: str) -> list:
    from event_store import EventStore
    store = EventStore(log_dir="/tmp/documentary-pipeline")
    return [e.effect for e in store.read_all(run_id)]

def clear_event_store():
    import glob
    for file in glob.glob("/tmp/documentary-pipeline/events_*.db*"):
        try:
            os.remove(file)
        except Exception:
            pass

def test_audio_all():
    clear_event_store()

    helper = VMTestHelper(vm_name="test-audio-vm", agent_port=8002)

    try:
        helper.start_gsa()
        helper.boot_vm()
        helper.setup_ssh_tunnel()
        helper.start_agent_in_vm("audio")

        # ---------------------------------------------------------------------------
        # SCENARIO 1: Queue TTS (UA-3)
        # ---------------------------------------------------------------------------
        logger.info("\
--- Running SCENARIO 1: Queue TTS ---")
        run_id = "test_ua3"
        from event_store import EventStore
        store = EventStore(log_dir="/tmp/documentary-pipeline")
        
        store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
        store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
        store.append(run_id, UpdateScript(
            run_id=run_id,
            agent="scenario",
            blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duratio
<truncated 6514 bytes>