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
    DurationAdjusted,
    MergeIntoOTIO,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestAssembly")

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

def test_assembly_all():
    clear_event_store()

    helper = VMTestHelper(vm_name="test-assembly-vm", agent_port=8005)

    try:
        helper.start_gsa()
        helper.boot_vm()
        helper.setup_ssh_tunnel()

        # Install ffmpeg inside the VM
        logger.info("Installing ffmpeg in the VM...")
        helper.run_in_vm("sudo apt-get update")
        helper.run_in_vm("sudo apt-get install -y ffmpeg")

        # Generate stub video and audio files inside the VM
        logger.info("Generating stub video and audio files inside VM /tmp/...")
        helper.run_in_vm("ffmpeg -y -f lavfi -i color=c=blue:s=320x240:d=5 /tmp/video.mp4")
        helper.run_in_vm("ffmpeg -y -f lavfi -i sine=frequency=1000:duration=5 /tmp/audio.wav")

        helper.start_agent_in_vm("assembly")

        # ---------------------------------------------------------------------------
        # SCENARIO 1: Pipeline Assembly and Ffmpeg Muxing (UA-14)
        # ---------------------------------------------------------------------------
        logger.info("\
--- Running S
<truncated 2677 bytes>