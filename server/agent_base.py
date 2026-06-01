from __future__ import annotations

import os
import time
import json
import asyncio
import glob
import logging
import httpx
from typing import Any, Optional, Literal, cast
from dataclasses import dataclass, field
from pathlib import Path
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, UserPromptPart
from pydantic_ai_provenance.capability import ProvenanceCapability
from pydantic_ai_summarization import ContextManagerCapability
from pydantic_ai_shields import CostTracking
from pydantic_deep import create_deep_agent, DeepAgentDeps, PeriodicReminderConfig, create_sliding_window_processor

from effects import Effect, BudgetExceeded, KIND_TO_MODEL
from event_store import EventStore

# Setup python path to allow importing config.py from root
import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger(__name__)

# Base path for DB files
LOG_DIR = "/tmp/documentary-pipeline"
event_store = EventStore(log_dir=LOG_DIR)

class LoopBoundLock:
    def __init__(self):
        self._lock = None

    def get_lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Lock()
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

run_lock_manager = LoopBoundLock()


@dataclass
class PipelineDeps(DeepAgentDeps):
    """Dependencies for pipeline agents."""
    gsa_url: str = "http://localhost:8000"
    agent_role: str = ""
    max_tokens: int = 128_000
    compaction_model: OpenAIChatModel = field(default_factory=lambda: get_agent_model())





class AgentResponse(BaseModel):
    """POST / response returned by every agent handler."""
    status: Literal["ok", "error", "halted"]
    effects_extracted: list[str] = Field(default_factory=list)
    error_message: str = ""
    agent: str
    timestamp: float = Field(default_factory=time.time)


class AgentHealthResponse(BaseModel):
    """GET / response from any agent."""
    status: Literal["healthy", "busy", "error"] = "healthy"
    agent: str
    last_run: Optional[float] = None
    current_task: Optional[str] = None
    last_error: Optional[str] = None
    idle_since: Optional[float] = None


def get_agent_model() -> OpenAIChatModel:
    """Initialize OpenAIChatModel utilizing local deepseek API key."""
    api_key = ""
    _deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if os.path.exists(_deepseek_key_path):
        with open(_deepseek_key_path) as f:
            api_key = f.read().strip()
    if api_key:
        os.environ["DEEPSEEK_API_KEY"] = api_key
        
    base_url = os.environ.get("DEEPSEEK_BASE_URL")
    if base_url:
        from pydantic_ai.providers.deepseek import DeepSeekProvider
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=base_url, api_key=api_key or "mock_key")
        provider_instance = DeepSeekProvider(openai_client=client)
        return OpenAIChatModel(
            "deepseek-chat",
            provider=provider_instance,
        )
        
    return OpenAIChatModel(
        "deepseek-chat",
        provider="deepseek",
    )


async def llm_complete(system: str, user: str, model: OpenAIChatModel) -> str:
    """Helper to run a completion using the provided OpenAIChatModel."""
    agent = Agent(model, system_prompt=system)
    result = await agent.run(user)
    return result.output


def _render_messages(messages: list) -> str:
    """Render a list of messages into a single text representation."""
    parts = []
    for m in messages:
        if isinstance(m, (ModelRequest, ModelResponse)):
            for part in m.parts:
                part_content = getattr(part, "content", None)
                part_text = getattr(part, "text", None)
                if part_content is not None:
                    parts.append(str(part_content))
                elif part_text is not None:
                    parts.append(str(part_text))
                else:
                    parts.append(str(part))
        else:
            parts.append(str(m))
    return "\n\n".join(parts)


def _determine_focus(role: str, state: dict) -> str:
    """Read GSA state to determine what the agent is working on."""
    timeline = state.get("otio", {})
    jobs = state.get("jobs", {})

    if role == "audio":
        slots = timeline.get("slots", {})
        dirty = [addr for addr, slot in slots.items() if slot.get("status") == "scripted"]
        if dirty:
            addr = dirty[0]
            slot = slots[addr]
            attempts = jobs.get("block_attempts", {}).get(addr, 0)
            return (
                f"audio reconciliation of block {addr}, "
                f"attempt {attempts}/5, "
                f"measured {slot.get('measured_sec')}s vs target {slot.get('scripted_sec')}s"
            )
        return "audio pipeline — all blocks clean, awaiting instructions"

    if role == "video":
        pending = [j for j in jobs.get("jobs", {}).values()
                   if j.get("status") in ("pending", "running") and j.get("job_type") == "ltx"]
        if pending:
            return f"video generation for {len(pending)} pending LTX jobs"
        return "video pipeline — awaiting approved audio"

    if role == "scenario":
        slots = timeline.get("slots", {})
        unfilled = [addr for addr, slot in slots.items() if slot.get("status") == "scripted"]
        if unfilled:
            return f"script writing: {len(unfilled)} unfilled slots"
        return "script refinement — all slots filled"

    if role == "assembly":
        return "final assembly — merging approved clips"

    if role == "provisioner":
        pending = [j for j in jobs.get("jobs", {}).values()
                   if j.get("status") == "pending"]
        if pending:
            return f"provisioning for {len(pending)} pending jobs"
        return "provisioner — no pending jobs"

    return f"{role} agent — no active task"


async def otio_aware_compress(ctx, messages, **kwargs):
    """Compaction hook used by ContextManagerCapability before compression."""
    # 1. Curl GSA for current state
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(ctx.deps.gsa_url)
            state = resp.json()
    except Exception:
        state = {}

    # 2. Determine focus from state
    focus = _determine_focus(ctx.deps.agent_role, state)

    # 3. Split protected sections from compressible content
    protected = []
    compressible = []
    for m in messages:
        content = ""
        if isinstance(m, (ModelRequest, ModelResponse)):
            for part in m.parts:
                part_content = getattr(part, "content", None)
                part_text = getattr(part, "text", None)
                if part_content is not None:
                    content += str(part_content)
                elif part_text is not None:
                    content += str(part_text)
                else:
                    content += str(part)
        else:
            content = str(m)

        if "=== BASE KNOWLEDGE" in content or "=== SKILL CATALOG" in content:
            protected.append(m)
        else:
            compressible.append(m)

    if not compressible:
        return messages

    # 4. Compact compressible content only
    flat = _render_messages(compressible)
    system = (
        f"Compress this agent context. Preserve everything related to: {focus}. "
        f"Keep all IDs, numbers, durations, verdicts, failure reasons. "
        f"Remove redundant pleasantries, old success details, clean blocks. "
        f"Output ONLY compressed context."
    )
    compressed = await llm_complete(
        system=system, user=flat, model=ctx.deps.compaction_model
    )

    return protected + [
        ModelRequest(parts=[SystemPromptPart(content="[Compacted System Context]")]),
        ModelRequest(parts=[UserPromptPart(content=compressed)])
    ]


def list_skills() -> str:
    """Return newline-separated relative paths of skill markdown files."""
    skills = []
    project_root = Path(__file__).resolve().parent.parent
    skills_dir = project_root / "server" / "skills"
    if not skills_dir.exists():
        return ""
    for root, _, files in os.walk(skills_dir):
        for f in files:
            if f.endswith(".md"):
                rel_path = Path(root).relative_to(project_root) / f
                skills.append(str(rel_path))
    return "\n".join(sorted(skills))


def read_last_n_effects(agent: str, n: int) -> list[Effect]:
    """Read last n events generated by the specified agent from the store."""
    try:
        all_events = event_store.read_all()
        filtered = [e.effect for e in all_events if e.effect.agent == agent]
        return cast(list[Effect], filtered[-n:])
    except Exception:
        return []


def format_memory(effects: list[Effect]) -> str:
    """Format recent history effects for LLM consumption."""
    if not effects:
        return "No recent history."
    lines = []
    for e in effects:
        lines.append(f"- {e.kind}: {e.model_dump_json(exclude={'effect_id', 'agent', 'timestamp'})}")
    return "\n".join(lines)


async def bash_command(ctx, command: str) -> str:
    """Run a bash command locally with a fallback host resolution for gsa."""
    import socket
    if "gsa:8000" in command:
        try:
            socket.gethostbyname("gsa")
        except socket.gaierror:
            command = command.replace("gsa:8000", "localhost:8000")
            command = command.replace("gsa", "localhost")

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(errors="replace") + stderr.decode(errors="replace")


# ===========================================================================
# System Prompts & Instructions for all Roles
# ===========================================================================

COMMUNICATION_STYLE = """
=== COMMUNICATION STYLE ===

You communicate in rich, detailed natural language. Be verbose. Explain your
observations, reasoning, decisions, and results thoroughly. Every output you
produce is read by a parser that extracts structured information from your prose.

RULES FOR WRITING:
1. STATE EVERYTHING EXPLICITLY. Do not assume the reader remembers prior context.
   Bad: "I did it."
   Good: "I queried the GSA and observed that block A1:3:1 has status 'scripted'
          with no measured duration. I decided to queue a TTS job for this block."

2. INCLUDE ALL IDENTIFIERS. Every block address, job ID, VM instance ID,
   offer ID, and URL must appear in your text.
   Bad: "The block passed."
   Good: "Block A1:3:1 measured 4.23 seconds against a scripted target of 4.00
          seconds. The delta is 0.23 seconds, which is within tolerance
          (max(4.00 * 0.15, 0.25) = 0.60 seconds). I judge this block as passing."

3. EXPLAIN REASONING. Show your work. The parser cannot see your tool outputs;
   it only sees your final text. If you compared two values, state both values
   and the comparison result.
   Bad: "Provisioned a VM."
   Good: "I searched Vast.ai and found 12 offers. I evaluated each for GPU type,
          VRAM, CUDA version, and price. Offer 7843219 ranked highest: RTX 4090,
          24GB VRAM, CUDA 12.6, $0.42/hr. I provisioned it with image
          vastai/worker:tts --disk 64. Instance ID is 9912834."

4. DESCRIBE FAILURES COMPLETELY. Error messages, exit codes, and raw output
   must be quoted in your text.
   Bad: "It failed."
   Good: "The curl to worker http://1.2.3.4:8880/ returned exit code 7
          (Failed to connect). The stderr was 'Connection refused'. I conclude
          the worker is down and will destroy and reprovision."

5. ONE ACTION PER TURN. Focus on a single decision and describe it fully.
   Do not list multiple unrelated actions. The parser extracts one effect
   from your text. Make that one effect obvious and well-described.

6. NEVER USE STRUCTURED FORMATS. No JSON, no XML, no markdown tables,
   no EFFECT: markers, no labeled sections. Write as if composing an email
   to a colleague who needs to understand exactly what you did and why.

7. NO CLOCK TIMEOUTS / SLEEPS. Never run 'sleep' commands or introduce artificial blocking delays in your bash commands. If a resource or VM is still loading/provisioning, output a summary and return a NoOp or end your turn. The agent loop will automatically check progress on your next turn a few seconds later.

8. DO NOT POLL OR WAIT WITHIN A TURN. In this event-driven architecture, any effects you decide to emit (such as queueing a job, allocating a VM, or updating the script) are ONLY committed to the database after your current turn completely finishes. Therefore, you can NEVER observe the results of your current turn's decisions by querying the GSA or running bash commands within the same turn.
   - Do not attempt to query GSA repeatedly to check if a job you just decided to queue has appeared or completed.
   - Once you decide on an action (e.g., QueueJob, VMAllocated, JobApproved, NoOp), state your decision clearly and END YOUR TURN immediately.
   - Trust the asynchronous pipeline: the coordinator will trigger your next turn after other agents (like the Provisioner or VM workers) have acted on your decisions.

9. MAXIMALLY INQUISITIVE ON OBSTACLES.
"""

ROLE_INSTRUCTIONS = {
    "scenario": f"""
=== YOUR ROLE ===
You are the Scenario Agent. You write and revise narration scripts for documentary films.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- GSA is read-only. DO NOT attempt to write to it using HTTP POST or PUT requests (e.g. via curl). All state updates and effects MUST be declared exclusively in your prose response so they can be parsed and written to the event store automatically.
- Each scene/segment needs: narration text, visual notes, duration estimate, scene number, and speaker.
- CRITICAL: Do not output markdown tables or bulleted lists of script sections in your explanation, as they interfere with parsing.

=== SKILL CATALOG ===
- server/skills/documentary-writing/SKILL.md — Compelling scripts, ADHD rules, structure, voices, shot planning

Read this skill: bash_command("cat server/skills/documentary-writing/SKILL.md")

{COMMUNICATION_STYLE}

=== WORKFLOW ===
1. Query GSA state to see the timeline.
2. If the script is missing or a scene has been deleted/reordered, write or revise the narration script.
3. If a downstream agent reports a duration mismatch, revise the narration text for the failed segment to adjust its length.

=== ACTION INFORMATION REQUIREMENTS ===
State your reasoning and present these details with precision in your prose:
- When writing or revising narration: State the scene number, segment identifier, speaker/voice, narration text, visual notes, and target duration.
- When removing a scene: Specify the scene number and the reason for deleting it.
- When reorganizing the order of scenes: Specify the new sequence of scene numbers.
- When waiting for other components: Describe what you are waiting for and why.
""",

    "audio": f"""
=== YOUR ROLE ===
You are the Audio Agent. You manage the audio production pipeline, trigger TTS, and judge duration alignment.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- GSA is read-only. DO NOT attempt to write to it using HTTP POST or PUT requests (e.g. via curl). All state updates and effects MUST be declared exclusively in your prose response so they can be parsed and written to the event store automatically.
- TTS budget: $2.00 limit. Max 5 attempts per segment before escalation.
- Pacing Tolerance: delta <= max(scripted_sec * 0.15, 0.25).
- Do NOT attempt to allocate, provision, or deallocate VMs. You are ONLY responsible for queueing jobs ('queue_job'), approving/reconciling audio, and adjusting block target durations. You do NOT manage infrastructure.

=== SKILL CATALOG ===
- server/skills/audio-production/SKILL.md — Qwen3-TTS capabilities, text chunking, voice selection, preprocessing, pronunciation hints

Read this skill: bash_command("cat server/skills/audio-production/SKILL.md")

{COMMUNICATION_STYLE}

=== WORKFLOW ===
1. Query GSA state to check script segments and jobs.
2. For any scripted segments that lack audio: request audio generation (TTS) using voice models.
3. For any measured audio segments: compare the measured duration against the scripted target. Approve if within tolerance; request a retry with adjusted parameters (or escalate if max attempts reached) if outside tolerance.
4. Once all script blocks have been successfully reconciled and their durations adjusted (such that no dirty blocks remain, and all are clean/measured), emit a reconciliation complete effect.

=== ACTION INFORMATION REQUIREMENTS ===
State your reasoning and present these details with precision in your prose:
- When requesting audio generation: Specify the segment identifier, scene number, speaker voice, and the exact text to synthesize.
- When approving measured audio: Specify the segment identifier, target duration, measured duration, the calculated delta and tolerance, and your approval verdict.
- When requesting a retry/re-synthesis: Specify the segment identifier, attempt count, and adjustments (e.g. speed or text changes).
- When escalating a failed segment: Describe the segment identifier, the history of all 5 attempts, and the issue.
- When all script blocks are reconciled: You MUST declare that reconciliation is complete. You MUST specify the total blocks, number of blocks passed, number of blocks failed, the worst duration delta in seconds, and the total measured duration in seconds.
- When waiting: State if you are waiting for active jobs to finish or if all segments are clean.
""",


    "video": f"""
=== YOUR ROLE ===
You are the Video Agent. You generate visual clips using LTX-2.3.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- GSA is read-only. DO NOT attempt to write to it using HTTP POST or PUT requests (e.g. via curl). All state updates and effects MUST be declared exclusively in your prose response so they can be parsed and written to the event store automatically.
- Measured audio duration is LAW — every video must match its audio exactly.

=== SKILL CATALOG ===
- server/skills/video-generation/SKILL.md — LTX prompt engineering, visual coherence, audio sync verification

Read this skill: bash_command("cat server/skills/video-generation/SKILL.md")

{COMMUNICATION_STYLE}

=== WORKFLOW ===
1. Query GSA state.
2. For any approved narration audio segments that lack video: request video clip generation (LTX) matching the exact audio duration.
3. For any completed video clips: review their quality/coherence, and either approve and merge them into the timeline, or reject and request a retry.

=== ACTION INFORMATION REQUIREMENTS ===
State your reasoning and present these details with precision in your prose:
- When requesting video clip generation: Specify the segment identifier, scene number, and the detailed visual description prompt.
- When reviewing a rendered clip: Specify the job ID, quality notes, and your approval or rejection verdict.
- When merging a clip into the timeline: Specify the segment identifier, track name, and duration.
- When waiting: Describe if you are waiting for approved audio or running video jobs.
""",

    "assembly": f"""
=== YOUR ROLE ===
You are the Assembly Agent. You compose the final documentary from approved audio and video clips.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- GSA is read-only. DO NOT attempt to write to it using HTTP POST or PUT requests (e.g. via curl). All state updates and effects MUST be declared exclusively in your prose response so they can be parsed and written to the event store automatically.
- Rule: Validate that all slots are filled, durations match, and tracks align before rendering using `ffmpeg`.

=== SKILL CATALOG ===
- server/skills/video-editing/SKILL.md — ffmpeg commands, OTIO timeline validation, output MP4 verification

Read this skill: bash_command("cat server/skills/video-editing/SKILL.md")

{COMMUNICATION_STYLE}

=== WORKFLOW ===
1. Query GSA state.
2. If all timeline segments have approved audio and video clips, validate the final timeline and render the output using ffmpeg.
3. Verify that the rendered documentary file exists, is uncorrupted, and matches the target duration.

=== ACTION INFORMATION REQUIREMENTS ===
State your reasoning and present these details with precision in your prose:
- When rendering the final documentary: Specify the final output path, total duration, and verification checklist results.
- When reporting an assembly failure: Describe the validation checks or FFmpeg render steps that failed.
- When waiting: Explain what clips are missing or what you are waiting for.
""",

    "provisioner": f"""
=== YOUR ROLE ===
You are the Provisioner Agent. You provision GPU VMs and dispatch jobs.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- GSA is read-only. DO NOT attempt to write to it or update VM/job states using HTTP POST or PUT requests (e.g. via curl). All state updates and effects (like VM allocation/adoption, job starts/completions, and deallocations) MUST be declared exclusively in your prose response so they can be parsed and written to the event store automatically.
- Rule: Adopt existing active VMs if possible. Never double-rent. Only one VM can be active at a time. If you need/want to provision a different VM, you must first destroy the existing active VM before renting the new one. Always use 'yes | vastai destroy instance <instance_id>' or pipe 'y' to prevent the command from hanging on confirmation prompts.
- Rule: STRICT SINGLE-EFFECT ORDER OF OPERATIONS. The parser can only extract one effect per turn. Therefore, you MUST transition through the provisioning lifecycle step-by-step across multiple turns. Never combine VM allocation/adoption, job dispatch, and job completion in a single turn response.
  - Turn 1: If a VM needs to be created or adopted, output the VM details to trigger a 'vm_allocated' effect (specifying the actual worker URL if adopted and ready, or 'unknown' if booting) and end your turn.
  - Turn 2: Once the 'vm_allocated' event exists in GSA, dispatch the job by posting to the worker's root URL (e.g. HTTP POST to the worker URL 'http://localhost:8888/'), emit 'job_started', and end your turn immediately without checking for completion.
  - Turn 3: Only after 'job_started' exists in GSA, poll the worker for completion and emit 'job_completed' (or 'job_failed') when done.
  - Turn 4: Only after 'job_completed' exists in GSA, deallocate the VM and emit 'vm_deallocated'.
- VM Setup & Real Media Production: When provisioning a GPU VM on Vast.ai, you must configure the instance to run the real VM agent. Your onstart bootstrap command must:
  1. Clone the public repository using the `strands-migration` branch:
     `git clone --depth 1 --branch strands-migration https://github.com/OrpingtonClose/economy-documentary.git /workspace/repo`
  2. Run the VM agent onstart script matching the VM's role (where <role> is 'tts' or 'ltx'):
     `bash /workspace/repo/scripts/vm_onstart_<role>.sh "<deepseek_api_key>" "<vast_api_key>"`
     (Inline the DeepSeek API key and Vast.ai API key from your environment/files). This script clons the repository, sets up the virtual environment, downloads models, and starts the VM agent (`scripts/vm_agent.py`) listening on port 8880.
- Worker URL & Tunneling: Since port 8880 is internal, establish a local SSH tunnel mapping port 8888 locally to port 8880 on the VM (`ssh -o StrictHostKeyChecking=no -f -N -L 8888:localhost:8880 -p <ssh_port> root@<ssh_host>`). Register `http://localhost:8888` as the worker URL in GSA.
- Job Dispatch: The worker is a fully autonomous reasoning agent running `vm_agent.py`. POST the raw text instruction/prompt directly to the worker at `http://localhost:8888/`. The worker returns a natural language response describing the result (containing the generated file path). As the Provisioner Agent, you will parse the path from the worker's response using your natural reasoning (there are no strict prefix or structured formatting constraints). Once the path on the VM is retrieved, you MUST download the file from the VM to the host machine via SSH/SCP (using the VM's SSH port and host, e.g. `scp -o StrictHostKeyChecking=no -P <ssh_port> root@<ssh_host>:<vm_path> /tmp/output.wav`). Record the local host path `/tmp/output.wav` (or `/tmp/video.mp4` for video) as the `artifact_uri` in the `job_completed` effect.
- Diagnostics: Treat slow boots or failures as diagnostic mysteries; run SSH checks (like `nvidia-smi`, `docker logs`) to inspect worker logs.

=== SKILL CATALOG ===
- server/skills/gpu-provisioning/SKILL.md — Vast.ai operations, GPU matching decision tree for LTX-2.3, instance creation, health verification, cost optimization

Read this skill: bash_command("cat server/skills/gpu-provisioning/SKILL.md")

{COMMUNICATION_STYLE}

=== WORKFLOW ===
1. Query GSA state to see pending jobs and VM status.
2. If there are pending jobs and no suitable active VM is available: search Vast.ai, rent a compatible GPU VM, and track its status.
3. Dispatch queued jobs to the active VM worker's endpoint.
4. If a worker fails, check logs/diagnostics via SSH (e.g. docker logs, nvidia-smi) to resolve the issue before deciding to release it.
5. If all jobs are complete and VMs are idle, release the VMs.

=== ACTION INFORMATION REQUIREMENTS ===
State your reasoning and present these details with precision in your prose:
- When renting a GPU VM: Specify the instance ID, machine ID, GPU model, hourly cost, and role.
- When releasing a GPU VM: Specify the instance ID and the reason (e.g. idle, failed).
- When updating VM status: Specify the instance ID, the current status (initializing, ready, offline), and drift.
- When dispatching a job: State the job ID and worker URL.
- When waiting: State what jobs are running/booting and why you are waiting.
""",

    "test_audio_pipeline": f"""
=== YOUR ROLE ===
You are a Test Agent for the Audio Pipeline.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- Inject test effects and drive the pipeline through the endpoints.
- Privileged direct event store access is allowed for verification.
""",

    "test_provisioner": f"""
=== YOUR ROLE ===
You are a Test Agent for the Provisioner.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Tool: `bash_command` (query GSA: `curl -s http://localhost:8000/`).
- Inject pending jobs and verify VM allocation and job completion cycles.
"""
}


def create_pipeline_agent(role: str, model_instance: OpenAIChatModel) -> Any:
    """Create a pipeline deep agent with all required capabilities."""
    provenance = ProvenanceCapability(
        agent_name=role,
        source_tools=["bash_command"],
    )

    agent = create_deep_agent(
        model=model_instance,
        instructions=ROLE_INSTRUCTIONS[role],
        on_before_compress=otio_aware_compress,
        history_processors=[
            create_sliding_window_processor(
                trigger=("messages", 100),
                keep=("messages", 50),
                max_input_tokens=128000,
            ),
        ],
        eviction_token_limit=None,
        context_manager=True,
        context_manager_max_tokens=128000,
        include_todo=False,
        include_filesystem=False,
        include_plan=False,
        include_memory=False,
        include_checkpoints=False,
        web_search=False,
        web_fetch=True,
        include_skills=True,
        include_subagents=True,
        include_builtin_subagents=True,
        skill_directories=["skills"],
        thinking=False,
        cost_tracking=True,
        cost_budget_usd=10.0,
        stuck_loop_detection=True,
        periodic_reminder=PeriodicReminderConfig(every_n_turns=10, first_after=5),
        capabilities=[
            provenance,
            ContextManagerCapability(
                max_tokens=128000,
            ),
            CostTracking(budget_usd=10.0),
        ],
        deps_type=PipelineDeps,
    )
    return agent


async def execute_agent_turn(
    role: str,
    gsa_url: str,
    notification_type: str = "instruction",
    context: dict[Any, Any] | None = None,
) -> list[Effect]:
    """Execute a single reasoning turn for the agent and append parsed effects."""
    lock = run_lock_manager.get_lock()
    async with lock:
        # 1. Read last 5 effects by this agent
        memory = read_last_n_effects(role, 5)
        memory_text = format_memory(memory)

        # 2. Build prompt
        skills = list_skills()
        prompt = f"""\
=== CURRENT CONTEXT ===
GSA URL: {gsa_url}
Available Skills:
{skills}

=== RECENT HISTORY ===
{memory_text}
"""
        if context:
            prompt += f"\n=== ADDITIONAL CONTEXT/INSTRUCTION ===\n{json.dumps(context, indent=2)}\n"

        # 3. Create agent and model
        model_instance = get_agent_model()
        agent = create_pipeline_agent(role, model_instance)

        # 4. Register tool
        @agent.tool
        async def run_bash(ctx, command: str) -> str:
            """Run an arbitrary bash command on the local machine."""
            return await bash_command(ctx, command)

        # 5. Run the agent
        deps = PipelineDeps(gsa_url=gsa_url, agent_role=role, compaction_model=model_instance)
        from pydantic_ai import UsageLimits
        result = await agent.run(prompt, deps=deps, usage_limits=UsageLimits(request_limit=300))
        agent_text = result.output

        try:
            with open(f"/tmp/documentary-pipeline/agent_debug_{role}.log", "a", encoding="utf-8") as f:
                f.write(f"\n\n========================================\n")
                f.write(f"TURN START: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"PROMPT:\n{prompt}\n")
                f.write(f"RESPONSE:\n{agent_text}\n")
                f.write(f"========================================\n")
        except Exception:
            pass

        # 6. Parse effects
        from effect_parser import parse_agent_text_multi
        effects = await parse_agent_text_multi(role, agent_text)

        # Compute hash from GSA otio slots
        import hashlib
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(gsa_url)
                gsa_state = resp.json()
            slots = gsa_state.get("otio", {}).get("slots", {})
            sorted_slots = sorted(slots.items())
            payload = json.dumps(sorted_slots, sort_keys=True)
            otio_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
        except Exception:
            otio_hash = "initial_hash"

        # Append effects
        for effect in effects:
            event_store.append(effect, otio_hash)

        # Post-turn budget checks
        try:
            # We can check budget exceeded by calling the helper or examining shields CostTracking
            spent = getattr(result, "cost", 0.0)
            if spent > 10.0:
                effect = BudgetExceeded(
                    agent=role,
                    spent_usd=spent,
                    limit_usd=10.0,
                )
                event_store.append(effect, otio_hash)
        except Exception:
            pass

        return effects


class StrictEndpointMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/":
            return PlainTextResponse("Not Found: Only root '/' is permitted", status_code=404)
        if request.method not in ("GET", "POST"):
            return PlainTextResponse("Method Not Allowed: Only GET and POST permitted", status_code=405)
        if request.query_params:
            return PlainTextResponse("Bad Request: Query parameters are prohibited", status_code=400)
        return await call_next(request)


def make_agent_app(role: str) -> FastAPI:
    """FastAPI application builder for a pipeline agent."""
    app = FastAPI(title=f"{role.capitalize()} Agent Server")
    app.add_middleware(StrictEndpointMiddleware)

    _agent_health = {
        "status": "healthy",
        "agent": role,
        "last_run": None,
        "current_task": None,
        "last_error": None,
        "idle_since": time.time(),
    }

    @app.get("/", response_model=AgentHealthResponse)
    async def health():
        try:
            gsa_url = "http://localhost:8000/"
            async with httpx.AsyncClient() as client:
                resp = await client.get(gsa_url)
                if resp.status_code == 200:
                    state = resp.json()
                    _agent_health["current_task"] = _determine_focus(role, state)
        except Exception:
            pass
        return _agent_health

    @app.post("/", response_model=AgentResponse)
    async def post_handler(request: Request):
        if _agent_health["status"] == "busy":
            return AgentResponse(
                status="ok",
                effects_extracted=[],
                agent=role,
                timestamp=time.time(),
            )

        body = await request.body()
        instruction_text = body.decode("utf-8").strip()

        if not os.path.exists(os.path.join(LOG_DIR, "events.db")):
            raise HTTPException(status_code=400, detail="No active runs found")

        is_human = False
        inst_text = ""
        if instruction_text and instruction_text != "Wake up and check GSA":
            is_human = True
            inst_text = instruction_text

        async def run_turn_in_background():
            _agent_health["status"] = "busy"
            _agent_health["last_run"] = time.time()
            try:
                # Append HumanInstruction event immediately if applicable
                if is_human:
                    from effects import HumanInstruction
                    inst_effect = HumanInstruction(
                        agent="operator",
                        target_agent=role,
                        instruction=inst_text,
                        from_human="operator",
                    )
                    # Compute hash from GSA otio slots
                    import hashlib
                    otio_hash = "initial_hash"
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get("http://localhost:8000/")
                            if resp.status_code == 200:
                                slots = resp.json().get("otio", {}).get("slots", {})
                                sorted_slots = sorted(slots.items())
                                otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
                    except Exception:
                        pass
                    event_store.append(inst_effect, otio_hash)

                await execute_agent_turn(
                    role=role,
                    gsa_url="http://localhost:8000/",
                    notification_type="human" if is_human else "instruction",
                    context={"instruction": inst_text} if is_human else None,
                )
                _agent_health["status"] = "healthy"
                _agent_health["idle_since"] = time.time()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                _agent_health["status"] = "error"
                _agent_health["last_error"] = str(exc)
                _agent_health["idle_since"] = time.time()

        asyncio.create_task(run_turn_in_background())

        return AgentResponse(
            status="ok",
            effects_extracted=[],
            agent=role,
            timestamp=time.time(),
        )

    @app.on_event("startup")
    async def start_autonomous_loop():
        async def run_loop():
            if role == "provisioner":
                vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
                if os.path.exists(vast_key_path):
                    try:
                        with open(vast_key_path) as f:
                            key = f.read().strip()
                        import subprocess
                        subprocess.run(f"vastai login {key}", shell=True, capture_output=True)
                    except Exception:
                        pass

            # Wait a few seconds for GSA start up in integration tests
            await asyncio.sleep(2.0)

            intervals = {
                "scenario": 5.0,
                "audio": 3.0,
                "video": 3.0,
                "assembly": 5.0,
                "provisioner": 2.0,
            }
            poll_interval = intervals.get(role, 10.0)

            while True:
                try:
                    db_exists = os.path.exists(os.path.join(LOG_DIR, "events.db"))
                    if db_exists:
                        # Skip if run is already executing a turn for this agent
                        lock = run_lock_manager.get_lock()
                        if lock.locked():
                            await asyncio.sleep(poll_interval)
                            continue

                        gsa_url = "http://localhost:8000/"
                        try:
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(gsa_url)
                                if resp.status_code == 200:
                                    state = resp.json()
                                else:
                                    state = None
                        except Exception:
                            state = None

                        if state:
                            current_phase = state.get("state", {}).get("current_phase", "init")
                            if current_phase not in ("done", "aborted"):
                                should_act = False
                                if role == "scenario":
                                    slots = state.get("otio", {}).get("slots", {})
                                    if slots:
                                        all_events = event_store.read_all()
                                        last_failed = False
                                        for e in reversed(all_events):
                                            if e.effect.kind == "update_script":
                                                break
                                            if e.effect.kind == "reconciliation_failed":
                                                last_failed = True
                                                break
                                        if last_failed:
                                            should_act = True
                                elif role == "audio":
                                    slots = state.get("otio", {}).get("slots", {})
                                    jobs = state.get("jobs", {}).get("jobs", {}).values()
                                    active_or_done_job_slots = {j.get("slot_id") for j in jobs if j.get("status") in ("pending", "running", "completed")}
                                    has_unqueued_slots = any(s.get("status") == "scripted" and addr not in active_or_done_job_slots for addr, s in slots.items())
                                    reconciled = state.get("jobs", {}).get("reconciliation_complete", False)
                                    has_measured = any(s.get("status") == "measured" for s in slots.values())
                                    if has_unqueued_slots or (has_measured and not reconciled):
                                        should_act = True
                                elif role == "video":
                                    slots = state.get("otio", {}).get("slots", {})
                                    jobs = state.get("jobs", {}).get("jobs", {}).values()
                                    active_or_done_ltx = {j.get("slot_id") for j in jobs if j.get("job_type") == "ltx" and j.get("status") in ("pending", "running", "completed")}
                                    unqueued_video = any(s.get("status") in ("measured", "delivered") and addr not in active_or_done_ltx for addr, s in slots.items())
                                    completed_ltx = any(j.get("job_type") == "ltx" and j.get("status") == "completed" for j in jobs)
                                    if unqueued_video or completed_ltx:
                                        should_act = True
                                elif role == "assembly":
                                    slots = state.get("otio", {}).get("slots", {})
                                    all_filled = len(slots) > 0 and all(s.get("status") == "delivered" for s in slots.values())
                                    if all_filled and current_phase != "done":
                                        should_act = True
                                elif role == "provisioner":
                                    jobs = state.get("jobs", {})
                                    pending_jobs = any(j.get("status") == "pending" for j in jobs.get("jobs", {}).values())
                                    active_vms = state.get("vms", {}).get("active_count", 0) > 0
                                    if pending_jobs or active_vms:
                                        should_act = True
                                elif "test_" in role:
                                    should_act = True

                                if should_act:
                                    _agent_health["status"] = "busy"
                                    _agent_health["last_run"] = time.time()
                                    try:
                                        await execute_agent_turn(
                                            role=role,
                                            gsa_url=gsa_url,
                                            notification_type="instruction",
                                            context={},
                                        )
                                        _agent_health["status"] = "healthy"
                                        _agent_health["idle_since"] = time.time()
                                    except Exception as exc:
                                        _agent_health["status"] = "error"
                                        _agent_health["last_error"] = str(exc)
                                        _agent_health["idle_since"] = time.time()
                except Exception:
                    pass
                await asyncio.sleep(poll_interval)

        asyncio.create_task(run_loop())

    return app
