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
    VMAllocated,
    VMDeallocated,
    JobStarted,
    JobCompleted,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestProvisioner")

# Mock vast.ai executable bash script content
MOCK_VASTAI_SCRIPT = """#!/bin/bash
if [[ "$*" == *"login"* ]]; then
  echo "Logged in successfully."
elif [[ "$*" == *"search offers"* ]]; then
  echo "offer_id: 1234, gpu_name: RTX 4090, vram: 24, price: 0.45"
  echo "offer_id: 5678, gpu_name: RTX A6000, vram: 48, price: 0.85"
elif [[ "$*" == *"create instance"* ]]; then
  echo "Started instance vm_instance_1 on offer 1234."
elif [[ "$*" == *"destroy instance"* ]]; then
  echo "Destroyed instance vm_instance_1."
elif [[ "$*" == *"show instances"* ]]; then
  echo "instance_id: vm_instance_1, status: running, ip: localhost, port: 8880"
else
  echo "mock_vastai: unknown command $*"
fi
"""

def setup_mock_vastai(helper: VMTestHelper):
    logger.info("Setting up mock vastai CLI inside VM...")
    # Escape single quotes and write
    escaped_script = MOCK_VASTAI_SCRIPT.replace("'", "'\\''")
    helper.run_in_vm(f"sudo tee /usr/local/bin/vastai << 'EOF'\
{escaped_script}\
EOF")
    helper.run_in_vm("sudo chmod +x /usr/local/bin/vastai")

def get_gsa_events(run_id: str) -> list:
    resp = httpx.get("http://localhost:8000/", headers={"X-Run-ID": run_id})
    assert resp.status_code == 200
    # Wait, GSA might not expose direct events list unless we read SQLite db
    # Let's read SQLite db directly
    from event_store import EventStore
    store = EventStore(log_dir
<truncated 9989 bytes>