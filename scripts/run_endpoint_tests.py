#!/usr/bin/env python3
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
    ReconciliationComplete,
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
            "kind": "update_script",
            "blocks": [
                {
                    "scene_num": 1,
                    "block_id": "intro",
                    "speaker": "V2",
                    "text": "Hello world narration.",
                    "duration_sec": 6.5
                }
            ]
        },
        "confidence": 10
    },
    # Audio UA-3
    ("audio", "test_ua3"): "Queue a TTS job for block intro.",
    ("parser", "test_ua3"): {
        "chain_of_thought": "Queue a TTS job for block intro.",
        "effect": {
            "kind": "queue_job",
            "job_id": "tts_job_intro",
            "job_type": "tts",
            "scene_num": 1,
            "block_id": "intro",
            "slot_id": "A1:1:intro",
            "params": {"voice": "V1", "text": "Hello world narration."}
        },
        "confidence": 10
    },
    # Audio UA-4
    ("audio", "test_ua4"): "Measured duration is within tolerance, adjust duration.",
    ("parser", "test_ua4"): {
        "chain_of_thought": "Measured duration is within tolerance, adjust duration.",
        "effect": {
            "kind": "duration_adjusted",
            "block_id": "intro",
            "slot_id": "A1:1:intro",
            "scene_num": 1,
            "voice_role": "V1",
            "scripted_sec": 6.5,
            "measured_sec": 6.8
        },
        "confidence": 10
    },
    # Audio UA-5
    ("audio", "test_ua5"): "Duration is outside tolerance, requeue job.",
    ("parser", "test_ua5"): {
        "chain_of_thought": "Duration is outside tolerance, requeue job.",
        "effect": {
            "kind": "job_requeued",
            "job_id": "tts_job_intro",
            "reason": "Duration too short",
            "new_params": {"speed": 0.9}
        },
        "confidence": 10
    },
    # Audio UA-6
    ("audio", "test_ua6"): "Reconciliation failed after 5 attempts.",
    ("parser", "test_ua6"): {
        "chain_of_thought": "Block has exceeded max attempts, reconciliation failed.",
        "effect": {
            "kind": "reconciliation_failed",
            "blocks_total": 1,
            "blocks_passed": 0,
            "blocks_failed": 1,
            "failures": [
                {
                    "block_id": "intro",
                    "scene_num": 1,
                    "phrase_idx": 0,
                    "voice": "V1",
                    "scripted_sec": 6.0,
                    "measured_sec": 3.0,
                    "delta_sec": -3.0,
                    "ratio": 0.5,
                    "message": "unrecoverable",
                    "attempt_number": 5
                }
            ],
            "worst_delta_sec": -3.0,
            "failure_type": "duration_unrecoverable"
        },
        "confidence": 10
    },
    # Audio UA-7
    ("audio", "test_ua7"): "All slots reconciled, emit reconciliation complete.",
    ("parser", "test_ua7"): {
        "chain_of_thought": "All slots reconciled, emit reconciliation complete.",
        "effect": {
            "kind": "reconciliation_complete",
            "blocks_total": 1,
            "blocks_passed": 1,
            "blocks_failed": 0,
            "worst_delta_sec": 0.3,
            "total_measured_sec": 6.8
        },
        "confidence": 10
    },
    # Video UA-8
    ("video", "test_ua8"): "Audio reconciled, queue video job.",
    ("parser", "test_ua8"): {
        "chain_of_thought": "Reconciliation complete, queue LTX job.",
        "effect": {
            "kind": "queue_job",
            "job_id": "ltx_job_intro",
            "job_type": "ltx",
            "scene_num": 1,
            "block_id": "intro",
            "slot_id": "A1:1:intro",
            "params": {"prompt": "Wide shot of skyscrapers."}
        },
        "confidence": 10
    },
    # Video UA-9
    ("video", "test_ua9"): "LTX job completed, merge video into OTIO.",
    ("parser", "test_ua9"): {
        "chain_of_thought": "LTX job completed, merge video into OTIO.",
        "effect": {
            "kind": "merge_into_otio",
            "job_id": "ltx_job_intro",
            "block_id": "intro",
            "scene_num": 1,
            "slot_id": "A1:1:intro",
            "artifact_uri": "/tmp/video.mp4",
            "track_name": "V1_Video",
            "duration_sec": 6.8
        },
        "confidence": 10
    },
    # Provisioner UA-10
    ("provisioner", "test_ua10"): "Allocate VM for pending jobs.",
    ("parser", "test_ua10"): {
        "chain_of_thought": "Pending job found, allocate VM.",
        "effect": {
            "kind": "vm_allocated",
            "instance_id": "vm_instance_1",
            "role": "tts",
            "offer_id": "1234",
            "worker_url": "http://localhost:8880",
            "gpu_type": "RTX 4090",
            "cost_per_hour": 0.45
        },
        "confidence": 10
    },
    # Provisioner UA-11
    ("provisioner", "test_ua11"): "Dispatch pending job to VM.",
    ("parser", "test_ua11"): {
        "chain_of_thought": "VM active, dispatch job.",
        "effect": {
            "kind": "job_started",
            "job_id": "tts_job_intro",
            "vm_instance_id": "vm_instance_1"
        },
        "confidence": 10
    },
    # Provisioner UA-12
    ("provisioner", "test_ua12"): "VM loading for too long, deallocate it.",
    ("parser", "test_ua12"): {
        "chain_of_thought": "VM loading for too long, deallocate it.",
        "effect": {
            "kind": "vm_deallocated",
            "instance_id": "vm_instance_1",
            "reason": "provision_failed"
        },
        "confidence": 10
    },
    # Provisioner UA-13
    ("provisioner", "test_ua13"): "No pending jobs, destroy idle VM.",
    ("parser", "test_ua13"): {
        "chain_of_thought": "No pending jobs, destroy idle VM.",
        "effect": {
            "kind": "vm_deallocated",
            "instance_id": "vm_instance_1",
            "reason": "job_done"
        },
        "confidence": 10
    },
    # Assembly UA-14
    ("assembly", "test_ua14"): "Assemble final documentary video.",
    ("parser", "test_ua14"): {
        "chain_of_thought": "Assemble final documentary video.",
        "effect": {
            "kind": "pipeline_complete",
            "output_path": "/tmp/final_documentary.mp4",
            "duration_sec": 6.8
        },
        "confidence": 10
    },
}

# ---------------------------------------------------------------------------
# Background HTTP Mock Servers
# ---------------------------------------------------------------------------

mock_llm_app = FastAPI()

def make_openai_response(content: str):
    import time
    return {
        "id": "chatcmpl-mock-12345",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "deepseek-chat",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }]
    }

LAST_SEEN_RUN_ID = None

@mock_llm_app.post("/v1/chat/completions")
async def mock_chat_completions(request: Request):
    global LAST_SEEN_RUN_ID
    body = await request.json()
    messages = body.get("messages", [])
    
    # 1. Determine run_id and role
    run_id = None
    role = None
    
    for msg in messages:
        content = msg.get("content", "")
        if "test_ua" in content:
            m = re.search(r"(test_ua\d+)", content)
            if m:
                run_id = m.group(1)
        
        # Look for agent persona
        if "Scenario Agent" in content or "Scenario agent" in content:
            role = "scenario"
        elif "Audio Agent" in content or "Audio agent" in content:
            role = "audio"
        elif "Video Agent" in content or "Video agent" in content:
            role = "video"
        elif "Assembly agent" in content or "Assembly Agent" in content:
            role = "assembly"
        elif "Provisioner Agent" in content or "Provisioner agent" in content:
            role = "provisioner"
        elif "expert document parser" in content:
            role = "parser"
            
    if role == "parser":
        user_content = messages[-1].get("content", "")
        m = re.search(r"(test_ua\d+)", user_content)
        if m:
            run_id = m.group(1)

    if run_id is not None:
        LAST_SEEN_RUN_ID = run_id
    else:
        run_id = LAST_SEEN_RUN_ID
        
    print(f"[MOCK LLM] role={role}, run_id={run_id}, LAST_SEEN_RUN_ID={LAST_SEEN_RUN_ID}")


    # Dynamic Simulation logic for Integration Tests (test_ua15 and test_ua16)
    if run_id in ("test_ua15", "test_ua16"):
        store = EventStore(log_dir=TEMP_LOG_DIR)
        try:
            events = store.read_all(run_id)
        except Exception:
            events = []
        kinds = [e.effect.kind for e in events]
        
        if role == "parser":
            user_content = messages[-1].get("content", "")
            effect_dict = None
            if "writing the narration" in user_content:
                duration = 6.5 if run_id == "test_ua15" else 6.0
                effect_dict = {
                    "kind": "update_script",
                    "blocks": [
                        {
                            "scene_num": 1,
                            "block_id": "intro",
                            "speaker": "V1",
                            "text": "Hello world narration.",
                            "duration_sec": duration
                        }
                    ]
                }
            elif "Rewriting the script" in user_content:
                effect_dict = {
                    "kind": "update_script",
                    "blocks": [
                        {
                            "scene_num": 1,
                            "block_id": "intro",
                            "speaker": "V2",
                            "text": "Hello world narration.",
                            "duration_sec": 6.5
                        }
                    ]
                }
            elif "reconciliation complete" in user_content:
                effect_dict = {
                    "kind": "reconciliation_complete",
                    "blocks_total": 1,
                    "blocks_passed": 1,
                    "blocks_failed": 0,
                    "worst_delta_sec": 0.3,
                    "total_measured_sec": 6.8
                }
            elif "adjust duration" in user_content:
                has_rewrite = any(e.effect.kind == "update_script" and any(b.speaker == "V2" for b in e.effect.blocks) for e in events)
                speaker = "V2" if (run_id == "test_ua16" and has_rewrite) else "V1"
                effect_dict = {
                    "kind": "duration_adjusted",
                    "block_id": "intro",
                    "slot_id": "A1:1:intro",
                    "scene_num": 1,
                    "voice_role": speaker,
                    "scripted_sec": 6.5,
                    "measured_sec": 6.8
                }
            elif "Queue a TTS job" in user_content:
                if run_id == "test_ua16":
                    has_rewrite = any(e.effect.kind == "update_script" and any(b.speaker == "V2" for b in e.effect.blocks) for e in events)
                    job_id = "tts_job_v2" if has_rewrite else f"tts_job_{sum(1 for e in events if e.effect.kind == 'queue_job' and e.effect.job_type == 'tts') + 1}"
                    voice = "V2" if has_rewrite else "V1"
                else:
                    job_id = "tts_job_intro"
                    voice = "V1"
                effect_dict = {
                    "kind": "queue_job",
                    "job_id": job_id,
                    "job_type": "tts",
                    "scene_num": 1,
                    "block_id": "intro",
                    "slot_id": "A1:1:intro",
                    "params": {"voice": voice, "text": "Hello world narration."}
                }
            elif "outside tolerance" in user_content:
                queue_count = sum(1 for e in events if e.effect.kind == "queue_job" and e.effect.job_type == "tts")
                effect_dict = {
                    "kind": "job_requeued",
                    "job_id": f"tts_job_{queue_count}",
                    "reason": "Duration too short",
                    "new_params": {"speed": 0.9}
                }
            elif "failed after 5 attempts" in user_content:
                effect_dict = {
                    "kind": "reconciliation_failed",
                    "blocks_total": 1,
                    "blocks_passed": 0,
                    "blocks_failed": 1,
                    "worst_delta_sec": -3.0,
                    "failure_type": "duration_unrecoverable",
                    "failures": [
                        {
                            "block_id": "intro",
                            "scene_num": 1,
                            "phrase_idx": 0,
                            "voice": "V1",
                            "scripted_sec": 6.0,
                            "measured_sec": 3.0,
                            "delta_sec": -3.0,
                            "ratio": 0.5,
                            "message": "unrecoverable",
                            "attempt_number": 5
                        }
                    ],
                    "worst_delta_sec": -3.0,
                    "failure_type": "duration_unrecoverable"
                }
            elif "Allocate VM" in user_content:
                effect_dict = {
                    "kind": "vm_allocated",
                    "instance_id": "vm_instance_1",
                    "role": "tts",
                    "offer_id": "1234",
                    "worker_url": "http://localhost:8880",
                    "gpu_type": "RTX 4090",
                    "cost_per_hour": 0.45
                }
            elif "Dispatch pending" in user_content:
                queued = [e.effect.job_id for e in events if e.effect.kind == "queue_job"]
                pending = []
                for jid in queued:
                    queue_reqs = 1 + sum(1 for e in events if e.effect.kind == "job_requeued" and e.effect.job_id == jid)
                    starts = sum(1 for e in events if e.effect.kind == "job_started" and e.effect.job_id == jid)
                    if queue_reqs > starts:
                        pending.append(jid)
                job_id = pending[0] if pending else "unknown_job"
                effect_dict = {
                    "kind": "job_started",
                    "job_id": job_id,
                    "vm_instance_id": "vm_instance_1"
                }
            elif "destroy idle" in user_content or "destroy" in user_content:
                effect_dict = {
                    "kind": "vm_deallocated",
                    "instance_id": "vm_instance_1",
                    "reason": "job_done"
                }
            elif "Audio reconciled" in user_content:
                effect_dict = {
                    "kind": "queue_job",
                    "job_id": "ltx_job_intro",
                    "job_type": "ltx",
                    "scene_num": 1,
                    "block_id": "intro",
                    "slot_id": "A1:1:intro",
                    "params": {"prompt": "Wide shot of skyscrapers."}
                }
            elif "merge video" in user_content:
                effect_dict = {
                    "kind": "merge_into_otio",
                    "job_id": "ltx_job_intro",
                    "block_id": "intro",
                    "scene_num": 1,
                    "slot_id": "A1:1:intro",
                    "artifact_uri": "/tmp/video.mp4",
                    "track_name": "V1_Video",
                    "duration_sec": 6.8
                }
            elif "Assemble final" in user_content:
                effect_dict = {
                    "kind": "pipeline_complete",
                    "output_path": "/tmp/final_documentary.mp4",
                    "duration_sec": 6.8
                }
                
            wrapped = {
                "chain_of_thought": "Extracting structured effect from dynamic simulation logic.",
                "effect": effect_dict,
                "confidence": 10
            }
            return make_openai_response(json.dumps(wrapped))
        else:
            prose = f"I am the {role} agent for {run_id}."
            if run_id == "test_ua15":
                if role == "scenario":
                    prose = "I am writing the narration script for scene 1. Script contains block intro."
                elif role == "audio":
                    if "duration_adjusted" in kinds:
                        prose = "All slots reconciled, emit reconciliation complete."
                    elif "job_completed" in kinds:
                        prose = "Measured duration is within tolerance, adjust duration."
                    else:
                        prose = "Queue a TTS job for block intro."
                elif role == "provisioner":
                    if "pipeline_complete" in kinds:
                        prose = "No pending jobs, destroy idle VM."
                    elif "vm_allocated" not in kinds:
                        prose = "Allocate VM for pending jobs."
                    elif "queue_job" in kinds:
                        prose = "Dispatch pending job to VM."
                    else:
                        prose = "Allocate VM for pending jobs."
                elif role == "video":
                    if any(e.effect.kind == "job_completed" and e.effect.job_id == "ltx_job_intro" for e in events):
                        prose = "LTX job completed, merge video into OTIO."
                    else:
                        prose = "Audio reconciled, queue video job."
                elif role == "assembly":
                    prose = "Assemble final documentary video."
            else: # test_ua16
                requeue_count = sum(1 for e in events if e.effect.kind == "job_requeued")
                has_rewrite = any(e.effect.kind == "update_script" and any(b.speaker == "V2" for b in e.effect.blocks) for e in events)
                if role == "scenario":
                    if "reconciliation_failed" in kinds:
                        prose = "Rewriting the script block intro with speaker V2."
                    else:
                        prose = "I am writing the narration script for scene 1. Script contains block intro."
                elif role == "audio":
                    if has_rewrite:
                        if "duration_adjusted" in kinds:
                            prose = "All slots reconciled, emit reconciliation complete."
                        elif any(e.effect.kind == "job_completed" and e.effect.job_id == "tts_job_v2" for e in events):
                            prose = "Measured duration is within tolerance, adjust duration."
                        else:
                            prose = "Queue a TTS job for block intro."
                    else:
                        queue_count = sum(1 for e in events if e.effect.kind == "queue_job" and e.effect.job_type == "tts")
                        has_completion_for_current = any(e.effect.kind == "job_completed" and e.effect.job_id == f"tts_job_{queue_count}" for e in events)
                        has_requeue_for_current = any(e.effect.kind == "job_requeued" and e.effect.job_id == f"tts_job_{queue_count}" for e in events)
                        if has_completion_for_current and not has_requeue_for_current:
                            if requeue_count < 5:
                                prose = "Duration is outside tolerance, requeue job."
                            else:
                                prose = "Reconciliation failed after 5 attempts."
                        else:
                            prose = f"Queue a TTS job for block intro, attempt {queue_count+1}."
                elif role == "provisioner":
                    if "pipeline_complete" in kinds:
                        prose = "No pending jobs, destroy idle VM."
                    elif "vm_allocated" not in kinds:
                        prose = "Allocate VM for pending jobs."
                    elif "queue_job" in kinds:
                        prose = "Dispatch pending job to VM."
                    else:
                        prose = "Allocate VM for pending jobs."
                elif role == "video":
                    if any(e.effect.kind == "job_completed" and e.effect.job_id == "ltx_job_intro" for e in events):
                        prose = "LTX job completed, merge video into OTIO."
                    else:
                        prose = "Audio reconciled, queue video job."
                elif role == "assembly":
                    prose = "Assemble final documentary video."
            
            return make_openai_response(prose)

    # Otherwise, check parser and non-parser static paths
    if role == "parser":
        user_content = messages[-1].get("content", "")
        # Detect agent role from user prose content
        if "writing the narration" in user_content:
            agent_role = "scenario"
        elif "Rewriting the script" in user_content:
            agent_role = "scenario"
        elif "Queue a TTS job" in user_content:
            agent_role = "audio"
        elif "within tolerance" in user_content:
            agent_role = "audio"
        elif "outside tolerance" in user_content:
            agent_role = "audio"
        elif "failed after 5 attempts" in user_content:
            agent_role = "audio"
        elif "complete" in user_content:
            agent_role = "audio"
        elif "queue video job" in user_content:
            agent_role = "video"
        elif "merge video" in user_content:
            agent_role = "video"
        elif "Allocate VM" in user_content:
            agent_role = "provisioner"
        elif "Dispatch pending" in user_content:
            agent_role = "provisioner"
        elif "loading for too long" in user_content:
            agent_role = "provisioner"
        elif "destroy idle" in user_content:
            agent_role = "provisioner"
        elif "Assemble final" in user_content:
            agent_role = "assembly"
        else:
            agent_role = "unknown"
            
        mock_val = MOCK_COMPLETIONS.get(("parser", run_id))
        return make_openai_response(json.dumps(mock_val))
    
    prose = MOCK_COMPLETIONS.get((role, run_id), f"I am the {role} agent for {run_id}.")
    return make_openai_response(prose)

mock_worker_app = FastAPI()

@mock_worker_app.get("/")
async def worker_health():
    return {"status": "healthy", "agent": "worker", "ready": True, "worker_type": "tts"}

@mock_worker_app.post("/")
async def worker_dispatch(request: Request):
    return {"status": "ok", "message": "job dispatched"}

# ---------------------------------------------------------------------------
# Server Boot Helpers
# ---------------------------------------------------------------------------

def run_in_thread(server):
    server.run()

def start_servers():
    from agents.scenario.app import app as scenario_app
    from agents.audio.app import app as audio_app
    from agents.video.app import app as video_app
    from agents.assembly.app import app as assembly_app
    from agents.provisioner.app import app as provisioner_app
    
    # Start GSA (8000)
    gsa_config = uvicorn.Config(global_state_agent.app, host="127.0.0.1", port=8000, log_level="warning")
    gsa_server = uvicorn.Server(gsa_config)
    t1 = threading.Thread(target=run_in_thread, args=(gsa_server,), daemon=True)
    t1.start()
    
    # Start Scenario Agent (8001)
    scenario_config = uvicorn.Config(scenario_app, host="127.0.0.1", port=8001, log_level="warning")
    scenario_server = uvicorn.Server(scenario_config)
    t_scenario = threading.Thread(target=run_in_thread, args=(scenario_server,), daemon=True)
    t_scenario.start()
    
    # Start Audio Agent (8002)
    audio_config = uvicorn.Config(audio_app, host="127.0.0.1", port=8002, log_level="warning")
    audio_server = uvicorn.Server(audio_config)
    t_audio = threading.Thread(target=run_in_thread, args=(audio_server,), daemon=True)
    t_audio.start()
    
    # Start Video Agent (8003)
    video_config = uvicorn.Config(video_app, host="127.0.0.1", port=8003, log_level="warning")
    video_server = uvicorn.Server(video_config)
    t_video = threading.Thread(target=run_in_thread, args=(video_server,), daemon=True)
    t_video.start()
    
    # Start Assembly Agent (8005)
    assembly_config = uvicorn.Config(assembly_app, host="127.0.0.1", port=8005, log_level="warning")
    assembly_server = uvicorn.Server(assembly_config)
    t_assembly = threading.Thread(target=run_in_thread, args=(assembly_server,), daemon=True)
    t_assembly.start()
    
    # Start Provisioner (8081)
    provisioner_config = uvicorn.Config(provisioner_app, host="127.0.0.1", port=8081, log_level="warning")
    provisioner_server = uvicorn.Server(provisioner_config)
    t_provisioner = threading.Thread(target=run_in_thread, args=(provisioner_server,), daemon=True)
    t_provisioner.start()
    
    # Start Mock LLM (8080)
    llm_config = uvicorn.Config(mock_llm_app, host="127.0.0.1", port=8080, log_level="warning")
    llm_server = uvicorn.Server(llm_config)
    t2 = threading.Thread(target=run_in_thread, args=(llm_server,), daemon=True)
    t2.start()
    
    # Start Mock Worker (8880)
    worker_config = uvicorn.Config(mock_worker_app, host="127.0.0.1", port=8880, log_level="warning")
    worker_server = uvicorn.Server(worker_config)
    t3 = threading.Thread(target=run_in_thread, args=(worker_server,), daemon=True)
    t3.start()
    
    # Give servers a moment to boot
    time.sleep(2.0)
    
    # Configure environment variables to point to mock LLM
    os.environ["DEEPSEEK_BASE_URL"] = "http://127.0.0.1:8080/v1"
    
    return [
        gsa_server,
        scenario_server,
        audio_server,
        video_server,
        assembly_server,
        provisioner_server,
        llm_server,
        worker_server,
    ]

def wait_for_agent(agent_port: int, timeout: float = 15.0):
    start = time.time()
    url = f"http://127.0.0.1:{agent_port}/"
    while time.time() - start < timeout:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code == 200:
                health = resp.json()
                if health.get("status") in ("healthy", "idle", "error"):
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"Agent on port {agent_port} did not complete turn within timeout.")

# ---------------------------------------------------------------------------
# Unit Agent Tests (UA-1 to UA-14)
# ---------------------------------------------------------------------------

def test_scenario_agent_first_draft():
    run_id = "test_ua1"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    
    resp = httpx.post("http://127.0.0.1:8001/", json={
        "run_id": run_id,
        "notification_type": "instruction",
        "context": {"instruction": "Write a short documentary about Lacan."}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8001)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "update_script" in kinds

    gsa_resp = httpx.get("http://127.0.0.1:8000/", headers={"X-Run-ID": run_id})
    assert gsa_resp.status_code == 200
    slots = gsa_resp.json()["otio"]["slots"]
    assert "A1:1:intro" in slots
    assert slots["A1:1:intro"]["status"] == "scripted"


def test_scenario_agent_rewrite_response():
    run_id = "test_ua2"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    
    store.append(run_id, ReconciliationFailed(
        run_id=run_id,
        agent="audio",
        blocks_total=1,
        blocks_passed=0,
        blocks_failed=1,
        worst_delta_sec=3.0,
        failure_type="duration_unrecoverable",
        failures=[ReconciliationFailureDetail(
            block_id="intro",
            scene_num=1,
            phrase_idx=0,
            voice="V1",
            scripted_sec=6.0,
            measured_sec=3.0,
            delta_sec=-3.0,
            ratio=0.5,
            message="unrecoverable"
        )]
    ), "")
    
    resp = httpx.post("http://127.0.0.1:8001/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8001)
    
    events = store.read_all(run_id)
    scenario_updates = [e.effect for e in events if e.effect.kind == "update_script" and e.effect.agent == "scenario"]
    assert len(scenario_updates) >= 2
    assert scenario_updates[-1].blocks[0].speaker == "V2"


def test_audio_agent_queues_tts():
    run_id = "test_ua3"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    
    resp = httpx.post("http://127.0.0.1:8002/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8002)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "queue_job" in kinds
    jobs = [e.effect for e in events if e.effect.kind == "queue_job"]
    assert jobs[0].job_type == "tts"


def test_audio_agent_validates_duration_pass():
    run_id = "test_ua4"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, QueueJob(run_id=run_id, agent="audio", job_id="tts_job_intro", job_type="tts", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
    store.append(run_id, JobCompleted(run_id=run_id, agent="provisioner", job_id="tts_job_intro", artifact_uri="/tmp/audio.wav", duration_sec=6.8, vm_instance_id="vm_instance_1"), "")
    
    resp = httpx.post("http://127.0.0.1:8002/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8002)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "duration_adjusted" in kinds


def test_audio_agent_validates_duration_fail_requeue():
    run_id = "test_ua5"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.0)]
    ), "")
    store.append(run_id, QueueJob(run_id=run_id, agent="audio", job_id="tts_job_intro", job_type="tts", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
    store.append(run_id, JobCompleted(run_id=run_id, agent="provisioner", job_id="tts_job_intro", artifact_uri="/tmp/audio.wav", duration_sec=3.0, vm_instance_id="vm_instance_1"), "")
    
    resp = httpx.post("http://127.0.0.1:8002/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8002)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "job_requeued" in kinds


def test_audio_agent_escalation_on_attempts():
    run_id = "test_ua6"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.0)]
    ), "")
    
    for i in range(1, 6):
        store.append(run_id, QueueJob(run_id=run_id, agent="audio", job_id=f"tts_job_{i}", job_type="tts", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
        store.append(run_id, JobCompleted(run_id=run_id, agent="provisioner", job_id=f"tts_job_{i}", artifact_uri="/tmp/audio.wav", duration_sec=3.0, vm_instance_id="vm_instance_1"), "")
        store.append(run_id, JobRequeued(run_id=run_id, agent="audio", job_id=f"tts_job_{i}", reason="too short"), "")
        
    resp = httpx.post("http://127.0.0.1:8002/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8002)
    
    events = store.read_all(run_id)
    failures = [e.effect for e in events if e.effect.kind == "reconciliation_failed"]
    assert len(failures) == 1
    assert failures[0].failure_type == "duration_unrecoverable"


def test_audio_agent_reconciliation_complete():
    run_id = "test_ua7"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, DurationAdjusted(run_id=run_id, agent="audio", block_id="intro", slot_id="A1:1:intro", scene_num=1, voice_role="V1", scripted_sec=6.5, measured_sec=6.8), "")
    
    resp = httpx.post("http://127.0.0.1:8002/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8002)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "reconciliation_complete" in kinds


def test_video_agent_queues_ltx():
    run_id = "test_ua8"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, DurationAdjusted(run_id=run_id, agent="audio", block_id="intro", slot_id="A1:1:intro", scene_num=1, voice_role="V1", scripted_sec=6.5, measured_sec=6.8), "")
    store.append(run_id, ReconciliationComplete(run_id=run_id, agent="audio", blocks_total=1, blocks_passed=1, blocks_failed=0, worst_delta_sec=0.3, total_measured_sec=6.8), "")
    
    resp = httpx.post("http://127.0.0.1:8003/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8003)
    
    events = store.read_all(run_id)
    jobs = [e.effect for e in events if e.effect.kind == "queue_job"]
    assert len(jobs) == 1
    assert jobs[0].job_type == "ltx"


def test_video_agent_merges_video():
    run_id = "test_ua9"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, DurationAdjusted(run_id=run_id, agent="audio", block_id="intro", slot_id="A1:1:intro", scene_num=1, voice_role="V1", scripted_sec=6.5, measured_sec=6.8), "")
    store.append(run_id, ReconciliationComplete(run_id=run_id, agent="audio", blocks_total=1, blocks_passed=1, blocks_failed=0, worst_delta_sec=0.3, total_measured_sec=6.8), "")
    store.append(run_id, QueueJob(run_id=run_id, agent="video", job_id="ltx_job_intro", job_type="ltx", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
    store.append(run_id, JobCompleted(run_id=run_id, agent="provisioner", job_id="ltx_job_intro", artifact_uri="/tmp/video.mp4", duration_sec=6.8, vm_instance_id="vm_instance_1"), "")
    
    resp = httpx.post("http://127.0.0.1:8003/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8003)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "merge_into_otio" in kinds


def test_provisioner_agent_allocates_vm():
    run_id = "test_ua10"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, QueueJob(run_id=run_id, agent="audio", job_id="tts_job_intro", job_type="tts", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
    
    resp = httpx.post("http://127.0.0.1:8081/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8081)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "vm_allocated" in kinds


def test_provisioner_agent_dispatches_job():
    run_id = "test_ua11"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, QueueJob(run_id=run_id, agent="audio", job_id="tts_job_intro", job_type="tts", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
    store.append(run_id, VMAllocated(run_id=run_id, agent="provisioner", instance_id="vm_instance_1", role="tts", offer_id="1234", worker_url="http://localhost:8880", gpu_type="RTX 4090", cost_per_hour=0.45), "")
    
    resp = httpx.post("http://127.0.0.1:8081/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8081)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "job_started" in kinds


def test_provisioner_agent_handles_stuck_vm():
    run_id = "test_ua12"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, QueueJob(run_id=run_id, agent="audio", job_id="tts_job_intro", job_type="tts", scene_num=1, block_id="intro", slot_id="A1:1:intro"), "")
    
    old_time = time.time() - 1000.0
    allocated_ev = VMAllocated(
        run_id=run_id,
        agent="provisioner",
        instance_id="vm_instance_1",
        role="tts",
        offer_id="1234",
        worker_url="http://localhost:8880",
        gpu_type="RTX 4090",
        cost_per_hour=0.45
    )
    allocated_ev.timestamp = old_time
    store.append(run_id, allocated_ev, "")
    
    resp = httpx.post("http://127.0.0.1:8081/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8081)
    
    events = store.read_all(run_id)
    deallocations = [e.effect for e in events if e.effect.kind == "vm_deallocated"]
    assert len(deallocations) == 1
    assert deallocations[0].reason == "provision_failed"


def test_provisioner_agent_deallocates_idle_vms():
    run_id = "test_ua13"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, VMAllocated(run_id=run_id, agent="provisioner", instance_id="vm_instance_1", role="tts", offer_id="1234", worker_url="http://localhost:8880", gpu_type="RTX 4090", cost_per_hour=0.45), "")
    
    resp = httpx.post("http://127.0.0.1:8081/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8081)
    
    events = store.read_all(run_id)
    deallocations = [e.effect for e in events if e.effect.kind == "vm_deallocated"]
    assert len(deallocations) == 1
    assert deallocations[0].reason == "job_done"


def test_assembly_agent_success():
    run_id = "test_ua14"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    store.append(run_id, UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[ScriptBlock(scene_num=1, block_id="intro", speaker="V1", text="Hello world.", duration_sec=6.5)]
    ), "")
    store.append(run_id, DurationAdjusted(run_id=run_id, agent="audio", block_id="intro", slot_id="A1:1:intro", scene_num=1, voice_role="V1", scripted_sec=6.5, measured_sec=6.8), "")
    store.append(run_id, MergeIntoOTIO(
        run_id=run_id,
        agent="video",
        job_id="ltx_job_intro",
        block_id="intro",
        scene_num=1,
        slot_id="A1:1:intro",
        artifact_uri="/tmp/video.mp4",
        track_name="V1_Video",
        duration_sec=6.8
    ), "")
    
    resp = httpx.post("http://127.0.0.1:8005/", json={
        "run_id": run_id,
        "notification_type": "wake",
        "context": {}
    }, timeout=5.0)
    assert resp.status_code == 200
    wait_for_agent(8005)
    
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "pipeline_complete" in kinds

# ---------------------------------------------------------------------------
# Multi-Agent Integration Tests
# ---------------------------------------------------------------------------

def test_integration_happy_path():
    run_id = "test_ua15"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    
    start_time = time.time()
    timeout = 150.0
    
    tts_completed = False
    ltx_completed = False
    
    while time.time() - start_time < timeout:
        events = store.read_all(run_id)
        kinds = [e.effect.kind for e in events]
        
        if "pipeline_complete" in kinds and "vm_deallocated" in kinds:
            break
            
        if "job_started" in kinds and not tts_completed:
            started_jobs = [e.effect.job_id for e in events if e.effect.kind == "job_started"]
            if "tts_job_intro" in started_jobs:
                store.append(run_id, JobCompleted(
                    run_id=run_id,
                    agent="provisioner",
                    job_id="tts_job_intro",
                    artifact_uri="/tmp/audio.wav",
                    duration_sec=6.8,
                    vm_instance_id="vm_instance_1"
                ), "")
                tts_completed = True
                print("--- INTEGRATION HAPPY: TTS Job Completed ---")
                
        if "job_started" in kinds and not ltx_completed:
            started_jobs = [e.effect.job_id for e in events if e.effect.kind == "job_started"]
            if "ltx_job_intro" in started_jobs:
                store.append(run_id, JobCompleted(
                    run_id=run_id,
                    agent="provisioner",
                    job_id="ltx_job_intro",
                    artifact_uri="/tmp/video.mp4",
                    duration_sec=6.8,
                    vm_instance_id="vm_instance_1"
                ), "")
                ltx_completed = True
                print("--- INTEGRATION HAPPY: LTX Job Completed ---")
                
        time.sleep(1.0)
        
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    assert "pipeline_complete" in kinds
    assert "vm_deallocated" in kinds


def test_integration_audio_drift():
    run_id = "test_ua16"
    store = agent_base.event_store
    store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")
    
    start_time = time.time()
    timeout = 150.0
    completed_jobs = set()
    
    while time.time() - start_time < timeout:
        events = store.read_all(run_id)
        kinds = [e.effect.kind for e in events]
        
        if "pipeline_complete" in kinds and "vm_deallocated" in kinds:
            break
            
        started_events = [e.effect for e in events if e.effect.kind == "job_started"]
        for job in started_events:
            job_id = job.job_id
            completed_count = sum(1 for e in events if e.effect.kind == "job_completed" and e.effect.job_id == job_id)
            started_count = sum(1 for e in events if e.effect.kind == "job_started" and e.effect.job_id == job_id)
            if started_count > completed_count:
                if job_id.startswith("tts_job_") and job_id != "tts_job_v2":
                    store.append(run_id, JobCompleted(
                        run_id=run_id,
                        agent="provisioner",
                        job_id=job_id,
                        artifact_uri="/tmp/audio.wav",
                        duration_sec=3.0,
                        vm_instance_id="vm_instance_1"
                    ), "")
                    print(f"--- INTEGRATION DRIFT: Completed {job_id} with short audio (3.0s) (started={started_count}, completed={completed_count+1}) ---")
                elif job_id == "tts_job_v2":
                    store.append(run_id, JobCompleted(
                        run_id=run_id,
                        agent="provisioner",
                        job_id=job_id,
                        artifact_uri="/tmp/audio.wav",
                        duration_sec=6.8,
                        vm_instance_id="vm_instance_1"
                    ), "")
                    print(f"--- INTEGRATION DRIFT: Completed {job_id} with V2 clean audio (6.8s) ---")
                elif job_id == "ltx_job_intro":
                    store.append(run_id, JobCompleted(
                        run_id=run_id,
                        agent="provisioner",
                        job_id=job_id,
                        artifact_uri="/tmp/video.mp4",
                        duration_sec=6.8,
                        vm_instance_id="vm_instance_1"
                    ), "")
                    print(f"--- INTEGRATION DRIFT: Completed {job_id} with video (6.8s) ---")
                    
        time.sleep(1.0)
        
    events = store.read_all(run_id)
    kinds = [e.effect.kind for e in events]
    
    assert "reconciliation_failed" in kinds
    scenario_updates = [e.effect for e in events if e.effect.kind == "update_script" and e.effect.agent == "scenario"]
    assert len(scenario_updates) >= 2
    assert scenario_updates[-1].blocks[0].speaker == "V2"
    
    assert "pipeline_complete" in kinds
    assert "vm_deallocated" in kinds

# ---------------------------------------------------------------------------
# Main Test Runner
# ---------------------------------------------------------------------------

def run_test(name, func):
    print(f"\nRunning {name}...", end="", flush=True)
    try:
        func()
        print(" \033[92m[PASSED]\033[0m")
        return True
    except Exception as e:
        print(" \033[91m[FAILED]\033[0m")
        traceback.print_exc()
        return False

def main():
    global TEMP_LOG_DIR
    TEMP_LOG_DIR = tempfile.mkdtemp(prefix="doc_pipeline_endpoint_test_")
    
    # Configure logging and event store
    agent_base.LOG_DIR = TEMP_LOG_DIR
    global_state_agent.LOG_DIR = TEMP_LOG_DIR
    agent_base.event_store = EventStore(log_dir=TEMP_LOG_DIR)
    global_state_agent.event_store = EventStore(log_dir=TEMP_LOG_DIR)
    
    print("Starting background FastAPI agent and mock servers...")
    servers = start_servers()
    
    tests = [
        ("UA-1: Scenario Agent Initial Draft", test_scenario_agent_first_draft),
        ("UA-2: Scenario Agent Rewrite Response", test_scenario_agent_rewrite_response),
        ("UA-3: Audio Agent Queues TTS", test_audio_agent_queues_tts),
        ("UA-4: Audio Agent Within Tolerance (Pass)", test_audio_agent_validates_duration_pass),
        ("UA-5: Audio Agent Out of Tolerance (Requeue)", test_audio_agent_validates_duration_fail_requeue),
        ("UA-6: Audio Agent Escalation on Attempts", test_audio_agent_escalation_on_attempts),
        ("UA-7: Audio Agent Reconciliation Complete", test_audio_agent_reconciliation_complete),
        ("UA-8: Video Agent Queues LTX Video", test_video_agent_queues_ltx),
        ("UA-9: Video Agent Merges Video", test_video_agent_merges_video),
        ("UA-10: Provisioner Allocates VM", test_provisioner_agent_allocates_vm),
        ("UA-11: Provisioner Dispatches Job", test_provisioner_agent_dispatches_job),
        ("UA-12: Provisioner Handles Stuck VM", test_provisioner_agent_handles_stuck_vm),
        ("UA-13: Provisioner Deallocates Idle VM", test_provisioner_agent_deallocates_idle_vms),
        ("UA-14: Assembly Agent validates and muxes", test_assembly_agent_success),
        ("Scenario 1: E2E Happy Path Integration Test", test_integration_happy_path),
        ("Scenario 2: E2E Audio Drift Integration Test", test_integration_audio_drift),
    ]
    
    success_count = 0
    failed_tests = []
    
    try:
        for name, func in tests:
            if run_test(name, func):
                success_count += 1
            else:
                failed_tests.append(name)
    finally:
        print("\nShutting down background servers...")
        for s in servers:
            s.should_exit = True
        shutil.rmtree(TEMP_LOG_DIR)
        
    print("\n================ TEST SUMMARY ================")
    print(f"Total: {len(tests)}, Passed: {success_count}, Failed: {len(failed_tests)}")
    if failed_tests:
        print("\nFailed Tests:")
        for name in failed_tests:
            print(f"- {name}")
        sys.exit(1)
    else:
        print("\n\033[92mAll tests passed successfully!\033[0m")
        sys.exit(0)

if __name__ == "__main__":
    main()
2