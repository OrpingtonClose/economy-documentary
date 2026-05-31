"import os
import sys
import glob
import time
import asyncio
import httpx
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional, Callable, Union, List
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from event_store import EventStore
from effects import Effect, NoOp

LOG_DIR = "/tmp/documentary-pipeline"
event_store = EventStore(log_dir=LOG_DIR)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MockAgentBase")

def get_active_runs() -> list[str]:
    active = []
    if not os.path.exists(LOG_DIR):
        return []
    for path in glob.glob(os.path.join(LOG_DIR, "events_*.db")):
        filename = os.path.basename(path)
        run_id = filename[7:-3]
        active.append(run_id)
    return active

async def get_gsa_state(run_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get("http://localhost:8000/", headers={"X-Run-ID": run_id})
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"GSA returned {resp.status_code}")

async def append_event(run_id: str, effect: Effect):
    otio_hash = "initial_hash"
    try:
        state = await get_gsa_state(run_id)
        slots = state.get("otio", {}).get("slots", {})
        sorted_slots = sorted(slots.items())
        otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
    except Exception:
        pass
    event_store.append(run_id, effect, otio_hash)

class AgentPayload(BaseModel):
    run_id: str
    notification_type: str
    context: Optional[dict] = None

class AgentResponse(BaseModel):
    status: str
    effects_extracted: list[str] = Field(default_factory=list)
    agent: str
    timestamp: float = Field(default_factory=time.time)

class AgentHealthResponse(Ba
<truncated 3058 bytes>