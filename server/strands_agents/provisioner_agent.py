"""Standalone Provisioner Agent — reads job queue, provisions VMs, executes jobs.

This is the "very agentic" provisioner that:
1. Uses bash exclusively for VM operations
2. Researches GPU requirements via Exa when uncertain
3. Learns from mistakes via memory (remember/recall)
4. Is a proper graph node in the pipeline

Architecture:
    The provisioner sits between media agents and workers as a graph node.
    Media agents submit jobs to the queue. The provisioner claims them,
    provisions workers, dispatches jobs, uploads artifacts to B2, and
    marks jobs complete.  It runs as a node that the graph re-invokes
    whenever pending jobs exist.

    Queue as single source of truth — no direct agent-to-agent calls.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from strands import tool

logger = logging.getLogger(__name__)

PROVISIONER = "provisioner"


# ---------------------------------------------------------------------------
# Bash — raw shell, the primary provisioning interface
# ---------------------------------------------------------------------------

@tool
def bash_command(command: str) -> str:
    """Run an arbitrary bash command on the local machine.

    YOU are the provisioner. Use this for ALL VM operations:
    - Search:  bash_command("vastai search offers --type on-demand --raw | head -20")
    - Provision: bash_command("vastai create instance <offer_id> --image <img> --disk 64 --ssh --direct --env '-p 8880:8880'")
    - Status:  bash_command("vastai show instance <id> --raw")
    - Destroy: bash_command("vastai destroy instance <id>")
    - SSH diag: bash_command("ssh -o StrictHostKeyChecking=no root@<ip> -p <port> 'curl -s http://localhost:8880/'")
    - Check worker log: bash_command("ssh -o StrictHostKeyChecking=no root@<ip> -p <port> 'tail -30 /workspace/worker.log'")
    - Local:   bash_command("ls /tmp")

    The agent parses the raw CLI output and decides.
    """
    from strands_agents.shared_a2a.vast_provisioning import run_bash_command

    return run_bash_command(command)


# ---------------------------------------------------------------------------
# Research — Exa web search, never guess
# ---------------------------------------------------------------------------

@tool
def research_model_requirements(model_name: str) -> str:
    """Research GPU requirements for a model using Exa web search.

    Use this BEFORE provisioning a worker for an unknown model.
    Never guess VRAM — always research.
    """
    from research_tools import research_model_requirements as _research

    return _research(model_name)


@tool
def evaluate_vastai_offers(model_name: str, raw_offers_text: str) -> str:
    """Evaluate Vast.ai offers against researched GPU requirements.

    Pass the raw text output from bash_command("vastai search offers ...")
    to get structured rankings. Pick the highest-ranked offer.
    """
    from research_tools import evaluate_vastai_offers as _evaluate

    return _evaluate(model_name, raw_offers_text)


# ---------------------------------------------------------------------------
# Queue — claim, run, complete, fail
# ---------------------------------------------------------------------------

@tool
def claim_job(stage: str) -> str:
    """Claim the next pending job for a stage. Returns job details or 'No jobs available.'

    stage: 'audio' or 'video'
    """
    from tools.provisioner_tools import claim_job as _claim

    return _claim(stage)


@tool
def set_job_running(job_id: str, worker_id: str) -> str:
    """Mark a job as running on a specific worker."""
    from tools.provisioner_tools import set_job_running as _running

    return _running(job_id, worker_id)


@tool
def set_job_completed(job_id: str, b2_artifact_key: str) -> str:
    """Mark a job as completed after the worker uploads the artifact to B2."""
    from tools.provisioner_tools import set_job_completed as _completed

    return _completed(job_id, b2_artifact_key)


@tool
def set_job_failed(job_id: str, error_message: str) -> str:
    """Mark a job as failed (permanent or retryable depending on attempts)."""
    from tools.provisioner_tools import set_job_failed as _failed

    return _failed(job_id, error_message)


@tool
def check_queue_status(stage: str) -> str:
    """Return counts of jobs by status for a stage."""
    from job_queue import get_queue_summary

    summary = get_queue_summary(stage)
    return json.dumps({"stage": stage, "counts": summary})


# ---------------------------------------------------------------------------
# VM registry — query existing workers
# ---------------------------------------------------------------------------

@tool
def query_vm_registry(stage: str = "") -> str:
    """Query the VM registry for VMs."""
    from vm_registry_tools import query_vm_registry as _query

    return _query(stage)


@tool
def check_worker_health(instance_id: str, worker_url: str = "") -> str:
    """Check if a worker VM is healthy and ready for jobs."""
    from vm_registry_tools import check_worker_health as _health

    return _health(instance_id, worker_url)


@tool
def get_provisioning_guidance(stage: str, agent_reasoning: str) -> str:
    """Get system-corrected guidance for VM provisioning."""
    from vm_registry_tools import get_provisioning_guidance as _guide

    return _guide(stage, agent_reasoning)


# ---------------------------------------------------------------------------
# Job dispatch — HTTP to workers
# ---------------------------------------------------------------------------

@tool
def dispatch_tts_job(worker_url: str, text: str, output_path: str) -> str:
    """Send narration text to a TTS worker and save the WAV result.

    Args:
        worker_url: URL of the TTS worker (e.g. "http://1.2.3.4:8880").
        text: Narration text to synthesize.
        output_path: Local path to save the WAV file.
    """
    import wave

    worker_url = worker_url.rstrip("/") + "/"
    req = Request(worker_url, data=text.encode("utf-8"), headers={"Content-Type": "text/plain"})

    try:
        with urlopen(req) as resp:
            wav_bytes = resp.read()
            duration = float(resp.headers.get("X-Audio-Duration", "0"))
            sample_rate = int(resp.headers.get("X-Sample-Rate", "24000"))
    except URLError as exc:
        return json.dumps({"status": "failed", "error": f"TTS worker unreachable: {exc}"})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)})

    if not wav_bytes.startswith(b"RIFF"):
        return json.dumps({"status": "failed", "error": "Worker returned non-WAV data"})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(wav_bytes)

    # Verify with wave module
    try:
        with wave.open(output_path, "r") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            actual_duration = frames / rate if rate else 0
    except wave.Error:
        actual_duration = duration

    return json.dumps({
        "status": "completed",
        "output_path": output_path,
        "duration_sec": round(actual_duration, 2),
        "sample_rate": sample_rate,
        "size_bytes": len(wav_bytes),
    })


@tool
def dispatch_video_job(worker_url: str, prompt: str, output_path: str) -> str:
    """Send a video prompt to a GPU worker and save the MP4 result.

    Args:
        worker_url: URL of the video worker (e.g. "http://1.2.3.4:8880").
        prompt: LTX-2.3 video prompt.
        output_path: Local path to save the MP4 file.
    """
    worker_url = worker_url.rstrip("/") + "/"
    req = Request(worker_url, data=prompt.encode("utf-8"), headers={"Content-Type": "text/plain"})

    try:
        with urlopen(req) as resp:
            mp4_bytes = resp.read()
            gen_time = float(resp.headers.get("X-Gen-Time", "0"))
    except URLError as exc:
        return json.dumps({"status": "failed", "error": f"Video worker unreachable: {exc}"})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)})

    if b"ftyp" not in mp4_bytes[:64]:
        return json.dumps({"status": "failed", "error": "Worker returned non-MP4 data"})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(mp4_bytes)

    return json.dumps({
        "status": "completed",
        "output_path": output_path,
        "gen_time_sec": round(gen_time, 2),
        "size_bytes": len(mp4_bytes),
    })


# ---------------------------------------------------------------------------
# B2 upload — artifact ground truth
# ---------------------------------------------------------------------------

@tool
def upload_artifact(local_path: str, b2_key: str) -> str:
    """Upload a local file to B2 as the artifact ground truth.

    Args:
        local_path: Path to the local file.
        b2_key: B2 object key (e.g. "renders/scene_001.mp4").
    """
    from tools.b2_checkpoint import upload_file

    try:
        upload_file(local_path, b2_key)
        return json.dumps({"status": "uploaded", "local_path": local_path, "b2_key": b2_key})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)})


# ---------------------------------------------------------------------------
# Memory — learn from mistakes
# ---------------------------------------------------------------------------

def _make_memory_tools(agent_name: str) -> list:
    """Create remember + recall_memory tools scoped to the provisioner."""
    from agent_memory import remember as _remember, recall_memory as _recall

    @tool
    def remember(text: str, category: str = "fact") -> str:
        """Write a durable memory that survives across pipeline runs.

        Use this when you learn something that future runs should know:
        - GPU offers that failed (and why)
        - Disk size requirements, boot times
        - Worker bugs or workarounds
        - What configurations actually worked
        category: 'failure', 'success', or 'fact'
        """
        return _remember(agent_name, text, category)

    remember.__name__ = f"remember_{agent_name}"

    @tool
    def recall_memory(query: str = "", category: str = "", limit: int = 20) -> str:
        """Recall memories from previous pipeline runs.

        Searches your persistent memory by keyword match.
        Use this at the start of your work to check what you've learned before.
        query: search term (case-insensitive). Empty = return all.
        category: 'failure', 'success', 'fact'. Empty = all.
        limit: max results.
        """
        return _recall(agent_name, query, category, limit)

    recall_memory.__name__ = f"recall_memory_{agent_name}"

    return [remember, recall_memory]


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------

_PROVISIONER_INSTRUCTION = """\
You are the Provisioner Agent. You are the ONLY entity that provisions GPU VMs and executes jobs.

Your job: read the job queue, claim jobs, ensure healthy workers exist, dispatch jobs, upload artifacts to B2, and mark jobs complete.

NEVER TROUBLESHOOT. ONLY CERTAINTY.

CORE RULE: You do NOT guess. You do NOT experiment. You follow what worked.

1. CHECK MEMORY FIRST: recall_memory(query='worker success', category='success')
   If memory exists of a successful VM configuration, USE THAT EXACT CONFIGURATION.
   Same GPU, same disk, same image, same provider. Do not deviate.

2. ONLY if no memory exists: call research_model_requirements('<model_name>')
   to learn the authoritative GPU requirements. Then use CONSERVATIVE defaults.

3. BEFORE provisioning: query_vm_registry(stage=<stage>)
   If a VM exists, call check_worker_health(instance_id=<id>, worker_url=<url>).
   If the VM is healthy, USE IT. Do not provision a second VM.

4. If you must provision:
   a. Call get_provisioning_guidance(stage, <your_reasoning>)
   b. Search Vast.ai: bash_command("vastai search offers --type on-demand --raw | head -20")
   c. Evaluate offers: evaluate_vastai_offers('<model_name>', <raw_search_text>)
   d. Pick the HIGHEST-RANKED 'ideal' or 'acceptable' offer.
   e. Provision: bash_command("vastai create instance <offer_id> ...")
   f. Wait for health: check_worker_health() or bash_command("ssh ... 'curl ...'")

5. After provisioning succeeds:
   remember(text='<stage> worker succeeded on <GPU> <VRAM>GB with image <image>', category='success')
   After ANY failure:
   remember(text='<stage> worker failed: <exact_error>', category='failure')

6. IF A WORKER FAILS: Do NOT try to fix it. Do NOT SSH in and tinker.
   Call get_provisioning_guidance with your failure reasoning.
   If guidance says 'destroy_and_reprovision', destroy it and start fresh.
   If guidance says 'use_existing', the worker may still be loading — WAIT.
   NEVER troubleshoot. The system knows more than you do.

WORKFLOW:
1. Call claim_job(stage='audio') then claim_job(stage='video').
   Process jobs for BOTH stages in a single invocation if possible.
2. For each claimed job:
   a. Call set_job_running(job_id, worker_id).
   b. Ensure a healthy worker exists for the job's stage.
   c. Dispatch the job to the worker via dispatch_tts_job or dispatch_video_job.
   d. Upload the result to B2 via upload_artifact.
   e. Call set_job_completed(job_id, b2_key).
3. If dispatch fails, call set_job_failed(job_id, error_message).
4. After all claimed jobs are processed, call check_queue_status(stage)
   for both stages.
5. If pending or needs_retry jobs remain, report status and STOP.
   The graph will re-invoke you.
6. If ALL jobs for BOTH stages are completed or failed, STOP cleanly.

STAGE-TO-MODEL MAPPING:
- 'audio' jobs always use Qwen3-TTS
- 'video' jobs always use LTX-2.3 (LTX Video)

BASH IS YOUR ONLY INTERFACE TO VMs. Never use Python-level VM libraries.
"""


def build_provisioner_agent(model) -> Any:
    """Build the standalone provisioner agent."""
    from strands import Agent

    return Agent(
        name=PROVISIONER,
        system_prompt=_PROVISIONER_INSTRUCTION,
        tools=[
            bash_command,
            research_model_requirements,
            evaluate_vastai_offers,
            claim_job,
            set_job_running,
            set_job_completed,
            set_job_failed,
            check_queue_status,
            query_vm_registry,
            check_worker_health,
            get_provisioning_guidance,
            dispatch_tts_job,
            dispatch_video_job,
            upload_artifact,
        ] + _make_memory_tools(PROVISIONER),
        model=model,
    )
