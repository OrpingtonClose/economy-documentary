"""Pipeline v4: Event-sourced, OTIO-derived graph with orchestrator agent.

Architecture:
- Event log (JSONL) is the ONLY source of truth
- OTIO and job queue are read models rebuilt from events
- Orchestrator agent decides which agent runs next
- Agents produce free text; instructor parses into typed Effect
- Bash is the agent's tool; effects record OUTCOMES
- No timeouts anywhere
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from datetime import datetime
from typing import Any

import opentimelineio as otio
from openai import OpenAI

from effects import (
    Effect,
    GenerateNarrationAudio,
    JobCompleted,
    JobFailed,
    JobQuestionAnswered,
    JobQuestionReceived,
    JobRequeued,
    JobStarted,
    NoOp,
    QAFailed,
    QAPassed,
    RenderVideoSegment,
    UpdateScript,
    VMAllocated,
    VMDeallocated,
    VMProvisionFailed,
)
from effect_parser import parse_agent_text_multi
from event_store import EventStore
from projection_handler import apply_event
from queue_projection import (
    get_completed_jobs,
    get_pending_jobs,
    get_queue_summary,
    project_queue,
)
from qa_gates_v4 import (
    qa_audio_completeness,
    qa_duration_align,
    qa_stills_judge,
    qa_video_artifact_probe,
)


# ---------------------------------------------------------------------------
# DeepSeek client
# ---------------------------------------------------------------------------

_DEEPSEEK_API_KEY = ""
_deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
if os.path.exists(_deepseek_key_path):
    with open(_deepseek_key_path) as _f:
        _DEEPSEEK_API_KEY = _f.read().strip()

_VAST_API_KEY = ""
_vast_key_path = "/Users/orpington/api_keys/LLMS/vast_api_key.txt"
if os.path.exists(_vast_key_path):
    with open(_vast_key_path) as _f:
        _VAST_API_KEY = _f.read().strip()

_DS_CLIENT = OpenAI(api_key=_DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")


# ---------------------------------------------------------------------------
# Agent prompts
# ---------------------------------------------------------------------------

AGENT_PROMPTS: dict[str, str] = {
    "orchestrator": """You are the pipeline orchestrator. You maintain global state awareness.

Your job:
1. Read the current world state (OTIO + queue)
2. Decide which agent should run next
3. Suggest what that agent should do

Respond in this format:

NEXT_AGENT: <agent_id>
REASON: <why this agent>
PROMPT_HINT: <what to tell the agent>

Agents: scenario, audio, video, assembly, provisioner

Rules:
- If no script exists → scenario
- If script exists but no audio jobs → audio
- If audio jobs pending/running → provisioner (to provision VMs)
- If audio complete but no video jobs → video
- If video jobs pending/running → provisioner
- If all media complete → assembly
- If output exists → DONE
""",
    "scenario": """You are a documentary scriptwriter.

Write a 30-second documentary script. Include:
- Narration text (3 versions: V1 primary, V2 alternate, V3 third take)
- Visual notes describing shots
- A dopamine hook for the opening
- Pronunciation hints for tricky words
- Duration estimate

Write naturally — prose, paragraphs, whatever feels right.
If the script already exists and looks good, say "NoOp: script already exists."
""",
    "audio": """You are an audio producer for documentaries.

Write which narration audio jobs you want created. Include:
- Scene number
- Voice (V1, V2, V3)
- The exact narration text to synthesize

Write naturally — prose, paragraphs, lists, whatever feels right.
If no script exists or jobs already exist, say "NoOp: waiting."
""",
    "video": """You are a video director for documentaries.

Write which video clips you want rendered. Include:
- Scene number
- Visual description (prompt for the video generator)
- Duration

Write naturally — prose, paragraphs, lists, whatever feels right.
If no script exists or jobs already exist, say "NoOp: waiting."
""",
    "assembly": """You are a video editor who assembles documentaries.

Write what you want to merge and render. Include:
- Audio clip paths and scene numbers
- Video clip paths and scene numbers
- Any ffmpeg commands needed

Write naturally — prose, paragraphs, lists, whatever feels right.
If audio/video is not ready, say "NoOp: waiting for media."
""",
    "provisioner": """You are a DevOps engineer who provisions VMs on Vast.ai.

You have ONE tool: bash. Use it freely.

When you provision a VM, describe what you did so the pipeline can record it.
When you destroy a VM, describe what you did.

If no action is needed, say "NoOp: nothing to provision."
""",
}


# ---------------------------------------------------------------------------
# State projection
# ---------------------------------------------------------------------------

def project_state(event_log_path: str, timeline_path: str) -> tuple[otio.schema.Timeline, dict, dict]:
    """Read event log and project both OTIO and queue state.

    Returns: (timeline, queue_jobs, events_list)
    """
    event_store = EventStore(event_log_path)
    records = event_store.read_all()
    events = [r.effect for r in records]

    # Project OTIO
    if os.path.exists(timeline_path):
        try:
            timeline = otio.schema.Timeline.from_json_file(timeline_path)
        except Exception:
            timeline = otio.schema.Timeline(name="documentary")
            stack = otio.schema.Stack(name="tracks")
            timeline.tracks = stack
            stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
            stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))
    else:
        timeline = otio.schema.Timeline(name="documentary")
        stack = otio.schema.Stack(name="tracks")
        timeline.tracks = stack
        stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
        stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))

    for effect in events:
        timeline = apply_event(timeline, effect)

    # Project queue
    queue_jobs = project_queue(events)

    return timeline, queue_jobs, events


def build_state_summary(timeline: otio.schema.Timeline, queue_jobs: dict) -> str:
    """Build a human-readable state summary for the orchestrator."""
    meta = timeline.metadata.get("documentary", {})
    lines = []

    has_script = bool(meta.get("narration_v1"))
    lines.append(f"Script exists: {has_script}")
    if has_script:
        lines.append(f"  V1: {meta.get('narration_v1', '')[:80]}...")
        lines.append(f"  Duration: {meta.get('duration_sec', 30)}s")

    audio_summary = get_queue_summary(queue_jobs, "audio")
    video_summary = get_queue_summary(queue_jobs, "video")
    lines.append(f"Audio queue: {audio_summary}")
    lines.append(f"Video queue: {video_summary}")

    pending_audio = get_pending_jobs(queue_jobs, "audio")
    pending_video = get_pending_jobs(queue_jobs, "video")
    lines.append(f"Pending audio jobs: {len(pending_audio)}")
    lines.append(f"Pending video jobs: {len(pending_video)}")

    completed_audio = get_completed_jobs(queue_jobs, "audio")
    completed_video = get_completed_jobs(queue_jobs, "video")
    lines.append(f"Completed audio jobs: {len(completed_audio)}")
    lines.append(f"Completed video jobs: {len(completed_video)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent calling
# ---------------------------------------------------------------------------

async def call_agent(agent_id: str, prompt: str) -> str:
    """Call an agent via direct LLM API (free text, no instructor)."""
    result = _DS_CLIENT.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": AGENT_PROMPTS.get(agent_id, "")},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )
    return str(result.choices[0].message.content)


# ---------------------------------------------------------------------------
# Bash execution (not an effect — agents use bash freely)
# ---------------------------------------------------------------------------

def run_bash(command: str) -> dict[str, Any]:
    """Execute a bash command. Returns result dict."""
    print(f"  [BASH] {command[:100]}...")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": -1}


# ---------------------------------------------------------------------------
# VM operations (via bash, recorded as effects)
# ---------------------------------------------------------------------------

def destroy_orphan_vms(event_store: EventStore) -> None:
    """Find and destroy orphan VMs with documentary-* labels, recording VMDeallocated effects."""
    print("[CLEANUP] Checking for orphan VMs...")
    result = run_bash("vastai show instances --raw 2>/dev/null || echo '[]'")
    if result["returncode"] != 0:
        print(f"  [CLEANUP] vastai failed: {result['stderr'][:200]}")
        return

    try:
        instances = json.loads(result["stdout"])
        if not isinstance(instances, list):
            instances = []
    except json.JSONDecodeError:
        instances = []

    destroyed = 0
    for inst in instances:
        inst_id = inst.get("id")
        label = inst.get("label", "")
        if inst_id and label and label.startswith("documentary-"):
            run_bash(f"vastai destroy instance {inst_id}")
            event_store.append(
                VMDeallocated(
                    agent_id="pipeline",
                    instance_id=str(inst_id),
                    reason="orphan cleanup at startup",
                ),
                otio_hash_before="",
            )
            destroyed += 1

    print(f"[CLEANUP] Destroyed {destroyed} orphan VM(s)")


# ---------------------------------------------------------------------------
# Worker dispatch — REAL workers only
# ---------------------------------------------------------------------------

async def _post_to_vm(client, worker_url: str, text: str) -> str:
    """POST text to a VM agent and return the response text."""
    resp = await client.post(
        f"{worker_url.rstrip('/')}/",
        content=text.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    resp.raise_for_status()
    return resp.text


def _parse_vm_response(response: str) -> tuple[str, str]:
    """Parse a VM agent response for markers.

    Returns (response_type, content) where response_type is one of:
    "result", "question", "error", or "unknown".
    """
    stripped = response.strip()
    if stripped.upper().startswith("RESULT:"):
        return "result", stripped[7:].strip()
    if stripped.upper().startswith("QUESTION:"):
        return "question", stripped[9:].strip()
    if stripped.upper().startswith("ERROR:"):
        return "error", stripped[6:].strip()
    # Fallback: if response contains a workspace path, treat as result
    if "/workspace/output/" in stripped:
        return "result", stripped
    return "unknown", stripped


def _extract_artifact_path(response_text: str, job_id: str, stage: str) -> str:
    """Try to find an artifact path in the VM response."""
    import re
    m = re.search(r"/workspace/output/[^\s\n]+\.(wav|mp4)", response_text)
    if m:
        return m.group(0)
    return f"/workspace/output/{job_id}.{'wav' if stage == 'audio' else 'mp4'}"


def _download_artifact(instance_id: str, remote_path: str, output_dir: str) -> str:
    """Download an artifact from a VM. Returns local path or empty string on failure."""
    if not instance_id or not remote_path:
        return ""
    artifact_name = os.path.basename(remote_path)
    local_dir = os.path.join(output_dir, "artifacts")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, artifact_name)
    if os.path.exists(local_path):
        return local_path
    download_cmd = f"vastai copy {instance_id}:{remote_path} {local_path}"
    result = run_bash(download_cmd)
    if result["returncode"] == 0 and os.path.exists(local_path):
        return local_path
    print(f"  [DOWNLOAD] Failed: {result['stderr'][:200]}")
    return ""


async def dispatch_pending_jobs(
    queue_jobs: dict,
    event_store: EventStore,
    output_dir: str,
    script_meta: dict,
) -> bool:
    """Dispatch jobs to real workers via HTTP, handling collaboration turns.

    Processes three kinds of jobs:
    - pending / needs_retry: send initial instruction
    - running with question_answer: send the answer to a VM's question

    Returns True if any work was dispatched.
    """
    import httpx

    acted = False

    # Discover worker URLs and instance IDs from VM effects
    records = event_store.read_all()
    worker_urls: dict[str, str] = {}  # stage -> worker_url
    worker_instances: dict[str, str] = {}  # worker_url -> instance_id
    for r in records:
        if r.effect.effect_type == "VMAllocated":
            eff = r.effect
            if hasattr(eff, "worker_url") and eff.worker_url:
                worker_instances[eff.worker_url] = getattr(eff, "instance_id", "")
                if "tts" in (eff.gpu_type or "").lower() or "audio" in str(getattr(eff, "label", "")).lower():
                    worker_urls["audio"] = eff.worker_url
                else:
                    worker_urls["video"] = eff.worker_url

    # Build script context once
    script_context = ""
    if script_meta.get("has_script"):
        script_context = (
            f"SCRIPT CONTEXT:\n"
            f"Scene: {script_meta.get('scene_num', 1)}\n"
            f"Duration: {script_meta.get('duration_sec', 30)}s\n"
            f"Dopamine hook: {script_meta.get('dopamine_hook', '')[:100]}...\n"
            f"Visual notes: {script_meta.get('visual_notes', '')[:100]}...\n"
            f"Pronunciation: {script_meta.get('pronunciation_hints', '')[:100]}...\n\n"
        )

    async with httpx.AsyncClient() as client:
        for job in queue_jobs.values():
            worker_url = worker_urls.get(job.stage)
            if not worker_url:
                continue

            instance_id = worker_instances.get(worker_url, "")

            # -----------------------------------------------------------------
            # Case 1: job has a pending answer — send it to the VM
            # -----------------------------------------------------------------
            if job.status == "running" and getattr(job, "question_answer", ""):
                answer_text = getattr(job, "question_answer", "")
                print(f"  [WORKER] Sending answer to {job.job_id} at {worker_url}")
                try:
                    agent_response = await _post_to_vm(client, worker_url, answer_text)
                    print(f"  [WORKER] {job.job_id} response: {agent_response[:200]}...")
                    resp_type, resp_content = _parse_vm_response(agent_response)

                    if resp_type == "result":
                        artifact_path = _extract_artifact_path(agent_response, job.job_id, job.stage)
                        local_path = _download_artifact(instance_id, artifact_path, output_dir)
                        event_store.append(
                            JobCompleted(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                artifact_path=artifact_path,
                                local_artifact_path=local_path,
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} completed → {artifact_path}")
                        acted = True
                    elif resp_type == "question":
                        event_store.append(
                            JobQuestionReceived(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                question=resp_content,
                                worker_url=worker_url,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} asked another question")
                        acted = True
                    elif resp_type == "error":
                        event_store.append(
                            JobFailed(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                error_message=resp_content,
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} failed: {resp_content[:200]}")
                        acted = True
                    else:
                        # Unknown response — treat as error but allow retry
                        event_store.append(
                            JobFailed(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                error_message=f"Unparseable VM response: {agent_response[:300]}",
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        acted = True
                except Exception as exc:
                    print(f"  [WORKER] {job.job_id} failed: {exc}")
                    event_store.append(
                        JobFailed(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            error_message=str(exc)[:500],
                            stage=job.stage,
                        ),
                        otio_hash_before="",
                    )
                    acted = True
                continue

            # -----------------------------------------------------------------
            # Case 2: job is waiting for an answer — skip until answered
            # -----------------------------------------------------------------
            if job.status == "running" and getattr(job, "pending_question", ""):
                print(f"  [WORKER] {job.job_id} awaiting answer — skipping")
                continue

            # -----------------------------------------------------------------
            # Case 3: pending / needs_retry — send initial instruction
            # -----------------------------------------------------------------
            if job.status not in ("pending", "needs_retry"):
                continue

            print(f"  [WORKER] Dispatching {job.job_id} to {worker_url}")

            # Mark started
            event_store.append(
                JobStarted(
                    agent_id="pipeline",
                    job_id=job.job_id,
                    worker_id=worker_url,
                    instance_id=instance_id,
                    stage=job.stage,
                ),
                otio_hash_before="",
            )

            try:
                if job.stage == "audio":
                    instruction = (
                        f"We are producing a documentary scene.\n\n"
                        f"{script_context}"
                        f"Please generate narration audio for this scene.\n"
                        f"The narration text is:\n"
                        f"\"{job.payload.get('text', '')}\"\n\n"
                        f"Use the Qwen3-TTS model (runner at repo/scripts/run_qwen3_tts.py). "
                        f"Save the output to /workspace/output/{job.job_id}.wav.\n\n"
                        f"If anything is unclear, ask. If generation fails, troubleshoot. "
                        f"Report RESULT: with the file path when done."
                    )
                else:
                    instruction = (
                        f"We are producing a documentary scene.\n\n"
                        f"{script_context}"
                        f"Please generate a video clip for this scene.\n"
                        f"The visual description is:\n"
                        f"\"{job.payload.get('prompt', '')}\"\n\n"
                        f"Target duration: {job.payload.get('duration_sec', 5)} seconds.\n"
                        f"Use the LTX-2.3 model (runner at repo/scripts/run_ltx_2_3.py). "
                        f"Save the output to /workspace/output/{job.job_id}.mp4.\n\n"
                        f"If anything is unclear, ask. If generation fails, troubleshoot. "
                        f"Report RESULT: with the file path when done."
                    )

                agent_response = await _post_to_vm(client, worker_url, instruction)
                print(f"  [WORKER] {job.job_id} response: {agent_response[:200]}...")
                resp_type, resp_content = _parse_vm_response(agent_response)

                if resp_type == "result":
                    artifact_path = _extract_artifact_path(agent_response, job.job_id, job.stage)
                    local_path = _download_artifact(instance_id, artifact_path, output_dir)
                    event_store.append(
                        JobCompleted(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            artifact_path=artifact_path,
                            local_artifact_path=local_path,
                            stage=job.stage,
                        ),
                        otio_hash_before="",
                    )
                    print(f"  [WORKER] {job.job_id} completed → {artifact_path}")
                    acted = True
                elif resp_type == "question":
                    event_store.append(
                        JobQuestionReceived(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            question=resp_content,
                            worker_url=worker_url,
                        ),
                        otio_hash_before="",
                    )
                    print(f"  [WORKER] {job.job_id} asked: {resp_content[:200]}")
                    acted = True
                elif resp_type == "error":
                    event_store.append(
                        JobFailed(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            error_message=resp_content,
                            stage=job.stage,
                        ),
                        otio_hash_before="",
                    )
                    print(f"  [WORKER] {job.job_id} failed: {resp_content[:200]}")
                    acted = True
                else:
                    # Unknown response — try to extract artifact path anyway
                    artifact_path = _extract_artifact_path(agent_response, job.job_id, job.stage)
                    local_path = _download_artifact(instance_id, artifact_path, output_dir)
                    if local_path:
                        event_store.append(
                            JobCompleted(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                artifact_path=artifact_path,
                                local_artifact_path=local_path,
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} completed → {artifact_path}")
                        acted = True
                    else:
                        event_store.append(
                            JobFailed(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                error_message=f"Unparseable VM response: {agent_response[:300]}",
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        acted = True

            except Exception as exc:
                print(f"  [WORKER] {job.job_id} failed: {exc}")
                event_store.append(
                    JobFailed(
                        agent_id="pipeline",
                        job_id=job.job_id,
                        error_message=str(exc)[:500],
                        stage=job.stage,
                    ),
                    otio_hash_before="",
                )
                acted = True

    return acted


# ---------------------------------------------------------------------------
# Assembly — REAL ffmpeg
# ---------------------------------------------------------------------------

def _ensure_local_artifact(job, output_dir: str) -> str | None:
    """Return a local path for a job's artifact, downloading from VM if needed."""
    # Prefer already-downloaded local path
    local = getattr(job, "local_artifact_path", "")
    if local and os.path.exists(local):
        return local

    # Try to download from VM
    instance_id = getattr(job, "instance_id", "")
    remote = job.artifact_path
    if not instance_id or not remote:
        print(f"  [ASSEMBLY] No VM info for {job.job_id}")
        return None

    artifact_name = os.path.basename(remote)
    local_dir = os.path.join(output_dir, "artifacts")
    os.makedirs(local_dir, exist_ok=True)
    local = os.path.join(local_dir, artifact_name)

    if os.path.exists(local):
        return local

    download_cmd = f"vastai copy {instance_id}:{remote} {local}"
    result = run_bash(download_cmd)
    if result["returncode"] == 0 and os.path.exists(local):
        print(f"  [ASSEMBLY] Downloaded {job.job_id} → {local}")
        return local
    else:
        print(f"  [ASSEMBLY] Download failed for {job.job_id}: {result['stderr'][:200]}")
        return None


def run_assembly(output_dir: str, queue_jobs: dict) -> bool:
    """Assemble final video from completed audio/video clips using ffmpeg.

    Returns True if assembly was attempted.
    """
    output_path = os.path.join(output_dir, "output", "documentary.mp4")
    if os.path.exists(output_path):
        return False

    completed_audio = get_completed_jobs(queue_jobs, "audio")
    completed_video = get_completed_jobs(queue_jobs, "video")

    if not completed_audio or not completed_video:
        return False

    os.makedirs(os.path.join(output_dir, "output"), exist_ok=True)

    audio_path = _ensure_local_artifact(completed_audio[0], output_dir)
    video_path = _ensure_local_artifact(completed_video[0], output_dir)

    if not audio_path:
        print(f"  [ASSEMBLY] Audio not available locally: {completed_audio[0].job_id}")
        return False
    if not video_path:
        print(f"  [ASSEMBLY] Video not available locally: {completed_video[0].job_id}")
        return False

    cmd = (
        f"ffmpeg -y -i {video_path} -i {audio_path} "
        f"-c:v copy -c:a aac -shortest {output_path}"
    )
    result = run_bash(cmd)

    if result["returncode"] == 0 and os.path.exists(output_path):
        print(f"  [ASSEMBLY] Created {output_path}")
        return True
    else:
        print(f"  [ASSEMBLY] ffmpeg failed: {result['stderr'][:200]}")
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def consult_orchestrator(state_summary: str, brief: str) -> dict[str, str]:
    """Ask the orchestrator agent what to do next.

    Returns: {"next_agent": str, "reason": str, "prompt_hint": str}
    """
    prompt = f"""Current state:
{state_summary}

Brief: {brief}

What should the pipeline do next?"""

    response = await call_agent("orchestrator", prompt)
    print(f"  [ORCHESTRATOR] {response[:200]}...")

    # Parse the orchestrator response
    next_agent = ""
    reason = ""
    prompt_hint = ""

    for line in response.splitlines():
        if line.startswith("NEXT_AGENT:"):
            next_agent = line.split(":", 1)[1].strip().lower()
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif line.startswith("PROMPT_HINT:"):
            prompt_hint = line.split(":", 1)[1].strip()

    # Fallback: if parsing failed, use simple heuristic
    if not next_agent:
        if "scenario" in response.lower():
            next_agent = "scenario"
        elif "audio" in response.lower():
            next_agent = "audio"
        elif "video" in response.lower():
            next_agent = "video"
        elif "assembly" in response.lower():
            next_agent = "assembly"
        elif "provisioner" in response.lower():
            next_agent = "provisioner"
        elif "done" in response.lower():
            next_agent = "done"

    return {
        "next_agent": next_agent,
        "reason": reason,
        "prompt_hint": prompt_hint,
    }


async def answer_pending_questions(queue_jobs: dict, event_store: EventStore, brief: str) -> bool:
    """Find jobs where the worker asked a question and generate answers.

    Uses the orchestrator to decide how to answer each question.
    Appends JobQuestionAnswered events.
    Returns True if any answers were produced.
    """
    acted = False
    for job in queue_jobs.values():
        question = getattr(job, "pending_question", "")
        if not question or job.status != "running":
            continue

        print(f"  [COLLAB] {job.job_id} question: {question[:200]}...")

        # Build a collaboration prompt for the orchestrator
        collab_prompt = (
            f"A worker agent asked a question about a job it is processing.\n\n"
            f"Job: {job.job_id} (stage={job.stage})\n"
            f"Question from worker: {question}\n\n"
            f"Documentary brief: {brief}\n\n"
            f"Please provide a concise, actionable answer for the worker. "
            f"The worker is a smart agent that can troubleshoot — give it the "
            f"information it needs, not a script to run."
        )

        try:
            response = await call_agent("orchestrator", collab_prompt)
            answer = response.strip()
            if answer:
                event_store.append(
                    JobQuestionAnswered(
                        agent_id="orchestrator",
                        job_id=job.job_id,
                        answer=answer,
                    ),
                    otio_hash_before="",
                )
                print(f"  [COLLAB] {job.job_id} answer: {answer[:200]}...")
                acted = True
        except Exception as exc:
            print(f"  [COLLAB] {job.job_id} failed to get answer: {exc}")

    return acted


def run_qa_gates(queue_jobs: dict, event_store: EventStore) -> bool:
    """Run deterministic QA gates on completed artifacts.

    For each completed job without a corresponding QAPassed/QAFailed event
    that happened AFTER the most recent JobCompleted, run the appropriate gate.
    Returns True if any QA was run.
    """
    acted = False
    records = list(event_store.read_all())

    # Find the latest JobCompleted index for each job
    latest_completion_idx: dict[str, int] = {}
    for i, r in enumerate(records):
        if r.effect.effect_type == "JobCompleted" and r.effect.job_id:
            latest_completion_idx[r.effect.job_id] = i

    # Find QA events that happened after the latest completion
    qa_done: set[str] = set()
    for i, r in enumerate(records):
        if r.effect.effect_type in ("QAPassed", "QAFailed"):
            job_id = r.effect.job_id
            if job_id in latest_completion_idx and i > latest_completion_idx[job_id]:
                qa_done.add(job_id)

    for job in queue_jobs.values():
        if job.status != "completed":
            continue
        if job.job_id in qa_done:
            continue

        local_path = getattr(job, "local_artifact_path", "") or job.artifact_path
        if not local_path or not os.path.exists(local_path):
            print(f"  [QA] {job.job_id} skipped — no local artifact")
            continue

        print(f"  [QA] Running gates for {job.job_id}")

        if job.stage == "audio":
            result = qa_audio_completeness(
                scene_id=job.job_id,
                audio_path=local_path,
            )
            if result["verdict"] == "pass":
                event_store.append(
                    QAPassed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict=result["reason"] if "reason" in result else "audio complete",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} passed audio completeness")
            else:
                event_store.append(
                    QAFailed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict=result.get("reason", "audio QA failed"),
                        comments=[result.get("reason", "")],
                        suggested_fix="Retry audio generation with same or adjusted text",
                    ),
                    otio_hash_before="",
                )
                event_store.append(
                    JobRequeued(
                        agent_id="qa",
                        job_id=job.job_id,
                        comments=[result.get("reason", "")],
                        suggested_fix="Retry audio generation",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} FAILED audio: {result.get('reason', '')}")
            acted = True

        elif job.stage == "video":
            # Run video probe + stills judge
            probe = qa_video_artifact_probe(
                scene_id=job.job_id,
                video_path=local_path,
            )
            stills = qa_stills_judge(
                scene_id=job.job_id,
                video_path=local_path,
            )

            issues: list[str] = []
            if probe["verdict"] == "fail":
                issues.append(probe.get("error", "probe failed"))
            if stills["verdict"] == "fail":
                issues.append(stills.get("reason", "stills judge failed"))

            if not issues:
                event_store.append(
                    QAPassed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict="video probe and motion check passed",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} passed video gates")
            else:
                event_store.append(
                    QAFailed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict="; ".join(issues),
                        comments=issues,
                        suggested_fix="Retry video generation with adjusted prompt or duration",
                    ),
                    otio_hash_before="",
                )
                event_store.append(
                    JobRequeued(
                        agent_id="qa",
                        job_id=job.job_id,
                        comments=issues,
                        suggested_fix="Retry video generation",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} FAILED video: {'; '.join(issues)}")
            acted = True

    return acted


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(
    brief: str,
    output_dir: str,
    max_cycles: int = 50,
) -> str:
    """Run the pipeline."""
    os.makedirs(output_dir, exist_ok=True)
    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")
    event_log_path = os.path.join(output_dir, "events.jsonl")

    event_store = EventStore(event_log_path)

    # Startup cleanup
    destroy_orphan_vms(event_store)

    print(f"[PIPELINE] Starting: {brief[:60]}")

    for cycle in range(max_cycles):
        print(f"\n[CYCLE {cycle + 1}]")

        # Project state from events
        timeline, queue_jobs, events = project_state(event_log_path, timeline_path)
        state_summary = build_state_summary(timeline, queue_jobs)
        print(f"  State summary:\n    {state_summary.replace(chr(10), chr(10) + '    ')}")

        # Check completion
        audio_summary = get_queue_summary(queue_jobs, "audio")
        video_summary = get_queue_summary(queue_jobs, "video")
        output_path = os.path.join(output_dir, "output", "documentary.mp4")

        audio_complete = (
            audio_summary.get("completed", 0) > 0
            and audio_summary.get("pending", 0) == 0
            and audio_summary.get("running", 0) == 0
            and audio_summary.get("needs_retry", 0) == 0
        )
        video_complete = (
            video_summary.get("completed", 0) > 0
            and video_summary.get("pending", 0) == 0
            and video_summary.get("running", 0) == 0
            and video_summary.get("needs_retry", 0) == 0
        )
        has_output = os.path.exists(output_path)

        if audio_complete and video_complete and has_output:
            print("[PIPELINE] Complete!")
            return f"Pipeline complete in {cycle + 1} cycles."

        # Consult orchestrator
        orch = await consult_orchestrator(state_summary, brief)
        next_agent = orch["next_agent"]

        if next_agent == "done" or not next_agent:
            print("[PIPELINE] Orchestrator says done. Stopping.")
            return f"Pipeline stopped after {cycle + 1} cycles."

        print(f"  Orchestrator: run {next_agent} ({orch['reason']})")

        # Build agent prompt
        agent_prompt = AGENT_PROMPTS.get(next_agent, "")
        if orch["prompt_hint"]:
            agent_prompt += f"\n\nHint: {orch['prompt_hint']}"
        agent_prompt += f"\n\n{state_summary}\n\nBrief: {brief}"

        # Run agent
        try:
            response = await call_agent(next_agent, agent_prompt)
            print(f"  Response: {response[:200]}...")

            effects = parse_agent_text_multi(next_agent, response)
            print(f"  Raw effects: {[e.effect_type for e in effects]}")

            # Validate effects before storing
            valid_effects = []
            for effect in effects:
                if effect.effect_type == "UpdateScript" and not effect.narration_v1:
                    print(f"  Validation: UpdateScript missing narration_v1 — skipping")
                    continue
                if effect.effect_type == "GenerateNarrationAudio" and not effect.text:
                    print(f"  Validation: GenerateNarrationAudio missing text — skipping")
                    continue
                if effect.effect_type == "RenderVideoSegment" and not effect.prompt:
                    print(f"  Validation: RenderVideoSegment missing prompt — skipping")
                    continue
                valid_effects.append(effect)

            # Retry parsing if no valid effects and response wasn't NoOp
            if not valid_effects and effects and not all(e.effect_type == "NoOp" for e in effects):
                print(f"  Retrying parse...")
                for attempt in range(2):
                    effects = parse_agent_text_multi(next_agent, response)
                    valid_effects = []
                    for effect in effects:
                        if effect.effect_type == "UpdateScript" and not effect.narration_v1:
                            continue
                        if effect.effect_type == "GenerateNarrationAudio" and not effect.text:
                            continue
                        if effect.effect_type == "RenderVideoSegment" and not effect.prompt:
                            continue
                        valid_effects.append(effect)
                    if valid_effects:
                        print(f"  Retry {attempt+1} succeeded: {[e.effect_type for e in valid_effects]}")
                        break

            # Store effects in event log
            for effect in valid_effects:
                # For VM effects, execute bash first, then record outcome
                if effect.effect_type == "VMAllocated":
                    mode = "tts" if "tts" in (effect.gpu_type or "").lower() else "ltx"
                    onstart = (
                        "cd /workspace && "
                        "apt-get update -qq && apt-get install -y -qq git curl wget ffmpeg && "
                        "git clone --depth 1 --branch strands-migration https://github.com/OrpingtonClose/economy-documentary.git repo && "
                        f"bash repo/scripts/vm_onstart_{mode}.sh '{_DEEPSEEK_API_KEY}' '{_VAST_API_KEY}'"
                    )
                    cmd = (
                        f"vastai create instance {effect.offer_id} "
                        f"--image pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime "
                        f"--disk 150 --ssh --direct --label documentary-{mode} "
                        f"--onstart-cmd '{onstart}'"
                    )
                    bash_result = run_bash(cmd)
                    if bash_result["returncode"] == 0:
                        import re
                        m = re.search(r"new_contract['\"]?\s*:\s*(\d+)", bash_result["stdout"])
                        if m:
                            effect.instance_id = m.group(1)
                        # Get worker URL from instance status
                        if effect.instance_id:
                            status_result = run_bash(f"vastai show instance {effect.instance_id} --raw")
                            if status_result["returncode"] == 0:
                                try:
                                    import json
                                    inst_info = json.loads(status_result["stdout"])
                                    public_ip = inst_info.get("public_ipaddr", "")
                                    ports = inst_info.get("ports", {})
                                    port_8880 = ports.get("8880/tcp", [])
                                    if public_ip and port_8880:
                                        host_port = port_8880[0].get("HostPort", "")
                                        if host_port:
                                            effect.worker_url = f"http://{public_ip}:{host_port}/"
                                except Exception:
                                    pass
                        event_store.append(effect, otio_hash_before="")
                    else:
                        event_store.append(
                            VMProvisionFailed(
                                agent_id=next_agent,
                                offer_id=effect.offer_id,
                                error_message=bash_result["stderr"][:500],
                            ),
                            otio_hash_before="",
                        )

                elif effect.effect_type == "VMDeallocated":
                    run_bash(f"vastai destroy instance {effect.instance_id}")
                    event_store.append(effect, otio_hash_before="")

                elif effect.effect_type != "NoOp":
                    event_store.append(effect, otio_hash_before="")

        except Exception as exc:
            print(f"  Error: {exc}")

        # Re-project state after storing effects
        timeline, queue_jobs, events = project_state(event_log_path, timeline_path)

        # Save OTIO
        timeline.to_json_file(timeline_path)

        # Build script metadata for VM context
        script_meta = timeline.metadata.get("documentary", {})
        script_meta["has_script"] = bool(script_meta.get("narration_v1"))

        # Answer any questions workers asked before dispatching more work
        await answer_pending_questions(queue_jobs, event_store, brief)

        # Dispatch pending jobs to workers
        await dispatch_pending_jobs(queue_jobs, event_store, output_dir, script_meta)

        # Run QA gates on completed artifacts
        run_qa_gates(queue_jobs, event_store)

        # Try assembly
        if audio_complete and video_complete and not has_output:
            run_assembly(output_dir, queue_jobs)
            # Re-check
            if os.path.exists(output_path):
                print("[PIPELINE] Assembly complete!")
                return f"Pipeline complete in {cycle + 1} cycles."

    return f"Pipeline reached max cycles ({max_cycles})."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Documentary Pipeline v4")
    parser.add_argument("--brief", default="A 30-second documentary about rainbows")
    parser.add_argument("--output-dir", default="./pipeline_output")
    args = parser.parse_args()

    result = asyncio.run(run_pipeline(args.brief, args.output_dir))
    print(f"\nResult: {result}")
