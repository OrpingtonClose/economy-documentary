"import os
import sys
import time
import pytest
import uvicorn
import shutil
import tempfile
import threading
import httpx
from pathlib import Path

# Setup python path to import server modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

import agent_base
import global_state_agent
from event_store import EventStore
from effects import (
    PipelineStarted,
    BudgetSet,
    UpdateScript,
    ScriptBlock,
    QueueJob,
    JobCompleted,
    DurationAdjusted,
    JobRequeued,
    VMAllocated,
)

# Active log directory
TEMP_LOG_DIR = None

@pytest.fixture(scope="module", autouse=True)
def setup_log_dir():
    global TEMP_LOG_DIR
    TEMP_LOG_DIR = tempfile.mkdtemp(prefix="doc_pipeline_test_")
    # Redirect log dir for test runtime
    agent_base.LOG_DIR = TEMP_LOG_DIR
    global_state_agent.LOG_DIR = TEMP_LOG_DIR
    agent_base.event_store = EventStore(log_dir=TEMP_LOG_DIR)
    global_state_agent.event_store = EventStore(log_dir=TEMP_LOG_DIR)
    yield
    shutil.rmtree(TEMP_LOG_DIR)

# Global test case registry to return appropriate mock LLM completions
MOCK_COMPLETIONS = {
    # Scenario UA-1
    ("scenario", "test_ua1"): "I am writing the narration script for scene 1. Script contains block intro.",
    ("parser", "test_ua1"): {
        "chain_of_thought": "Extracting UpdateScript block.",
        "effect": {
            "kind": "update_script",
            "blocks": [
                {
                    "scene_num": 1,
                    "block_id": "intro",
                    "speaker": "V1",
                    "text": "Hello world narration.",
                    "duration_sec": 6.5
                }
            ]
        },
        "confidence": 10
    },
    # Scenario UA-2
    ("scenario", "test_ua2"): "Rewriting the script block intro with speaker V2.",
    ("parser", "test_ua2"): {
        "chain_of_thought": "
<truncated 38863 bytes>