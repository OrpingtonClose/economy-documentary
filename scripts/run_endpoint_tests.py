"#!/usr/bin/env python3
"""Standalone HTTP Endpoint Simulation Tests for V7.1.

No pytest framework is used. All agent interactions, unit agent validations, 
and multi-agent end-to-end scenarios are driven purely over HTTP boundaries.
"""

import os
import sys
import time
import re
import json
import shutil
import tempfile
import threading
import traceback
import httpx
import uvicorn
from pathlib import Path
from fastapi import FastAPI, Request

# Setup python path to import server modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
    ReconciliationFailed,
    ReconciliationFailureDetail,
    MergeIntoOTIO,
)

TEMP_LOG_DIR = None

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
        "chain_of_thought": "Extracting revised UpdateScript block with updated speaker.",
        "effect": {
            "kin
<truncated 55167 bytes>