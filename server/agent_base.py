from __future__ import annotations

import os
import time
import json
import asyncio
import glob
import logging
import httpx
from typing import Any, Optional, Literal
from dataclasses import dataclass, field
from pathlib import Path
from fastapi import FastAPI, Query
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
run_locks: dict[str, asyncio.Lock] = {}


@dataclass
class PipelineDeps(DeepAgentDeps):
    """Dependencies for pipeline agents."""
    gsa_url: str = "http://localhost:8000"
    agent_role: str = ""
    max_tokens: int = 128_000
    compaction_model: OpenAIChatModel = field(default_factory=lambda: get_agent_model())


class AgentPayload(BaseModel):
    """POST / request body sent to every agent."""
    run_id: str = Field(..., description="UUIDv7 string identifying the pipeline run")
    notification_type: Literal["instruction", "human"] = Field(
        ...,
        description="instruction = directed task; human = operator-sent HumanInstruction",
    )
    context: dict = Field(
        default_factory=dict,
        description="Agent-specific context",
    )


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
                if hasattr(part, "content"):
                    parts.append(str(part.content))
                elif hasattr(part, "text"):
                    parts.append(str(part.text))
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
                if hasattr(part, "content"):
                    content += str(part.content)
                elif hasattr(part, "text"):
                    content += str(part.text)
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


def read_last_n_effects(run_id: str, agent: str, n: int) -> list[Effect]:
    """Read last n events generated by the specified agent from the store."""
    try:
        all_events = event_store.read_all(run_id)
        filtered = [e.effect for e in all_events if e.effect.agent == agent]
        return filtered[-n:]
    except Exception:
        return []


def format_memory(effects: list[Effect]) -> str:
    """Format recent history effects for LLM consumption."""
    if not effects:
        return "No recent history."
    lines = []
    for e in effects:
        lines.append(f"- {e.kind}: {e.model_dump_json(exclude={'run_id', 'effect_id', 'agent', 'timestamp'})}")
    return "\n".join(lines)


def get_active_runs() -> list[str]:
    """Search for all run database files in the log directory."""
    active = []
    if not os.path.exists(LOG_DIR):
        return []
    for path in glob.glob(os.path.join(LOG_DIR, "events_*.db")):
        filename = os.path.basename(path)
        run_id = filename[7:-3]  # remove 'events_' and '.db'
        active.append(run_id)
    return active


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
    return stdout.decode() + stderr.decode()


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
"""

ROLE_INSTRUCTIONS = {
    "scenario": f"""
=== YOUR ROLE ===
You are the Scenario Agent. You write and revise narration scripts for documentary
films. You are a creative writer who understands pacing, tone, narrative structure,
and the constraints of audio-visual production.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Each block needs: narration text (v1/v2/v3), visual_notes, dopamine_hook,
  pronunciation_hints, duration_sec, scene_num, and voice (V1/V2/V3).
- Query state: bash_command("curl -s http://gsa:8000/")
- Parse JSON with jq: bash_command("curl -s http://gsa:8000/ | jq '.timeline.slots'")
- One action per turn. Write narration that fits the scene and duration target.
- You have ONE tool: bash_command. Use it to query the GSA and see which slots
  need filling, which scenes are incomplete, and what revisions are requested.

CRITICAL: Under no circumstances should you ever output markdown tables, summary lists, or structured previews of the script blocks in your explanation text. The parser extracts the script from your output, so if you include tables or bulleted lists summarizing the blocks, the parser will extract those summaries instead of the actual full narration text, destroying the documentary. Always write your rationale as simple paragraphs, and only write the script blocks inside your single final UpdateScript action representation.

=== SKILL CATALOG ===
- server/skills/documentary-writing/SKILL.md — Compelling scripts, ADHD rules, structure, voices, shot planning

Read this skill: bash_command("cat server/skills/documentary-writing/SKILL.md")

{COMMUNICATION_STYLE}

=== PERMITTED EFFECTS ===
UpdateScript, DeleteScene, ReorderScenes, NoOp, ClarificationRequest

=== WORKFLOW ===
1. Query the GSA to see the current timeline state.
2. Identify unfilled slots, script gaps, or voice mismatches.
3. Read relevant skills if unsure how to proceed.
4. Write or revise narration text for ONE block.
5. Describe what you wrote, why it fits, and which block it targets.
""",
    "audio": f"""
=== YOUR ROLE ===
You are the Audio Agent. You own the entire audio pipeline from script to
measured audio. You are methodical, resourceful, and strategic. You plan across
multiple turns, batch similar work, and escalate only after exhausting reasonable
options.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- TTS: Qwen3-TTS runs on GPU VMs (RTX 4090 or A100 via Vast.ai).
- Measurement: WhisperX transcribes generated audio and reports duration.
- Tolerance: A block passes if |measured - scripted| <= max(scripted*0.15, 0.25s).
- Budget: $2.00 total TTS spend across all blocks in this run.
- Attempt budget: max 5 TTS generations per block before escalation.
- Query state: bash_command("curl -s http://gsa:8000/ | jq '.jobs'")
- Query timeline: bash_command("curl -s http://gsa:8000/ | jq '.timeline.slots'")
- You have ONE tool: bash_command.

=== SKILL CATALOG ===
- server/skills/audio-production/SKILL.md — Qwen3-TTS capabilities, text chunking, voice selection, preprocessing, pronunciation hints

Read this skill: bash_command("cat server/skills/audio-production/SKILL.md")

{COMMUNICATION_STYLE}

=== DECISION FRAMEWORK ===
When you see dirty blocks (status=scripted, no audio yet):
  1. Query the GSA. Read the full block list. Count dirty blocks.
  2. Prioritize by: attempt count (lower first), text length (shorter first),
     voice assignment (batch same-voice blocks together).
  3. Decide: queue one job, or batch multiple blocks into one VM job?
  4. For each job: specify voice (V1/V2/V3), text (exact narration), slot_id.
  5. Describe your decision in detail: which blocks, why these, what params.

When you see measured blocks (status=measured, awaiting judgment):
  1. Query the GSA. Read measured durations and scripted targets.
  2. For each: compute delta = |measured_sec - scripted_sec|.
  3. Compute tolerance = max(scripted_sec * 0.15, 0.25).
  4. If delta <= tolerance: the block PASSES. Describe: the block address,
     measured value, scripted target, delta, tolerance, and your judgment.
  5. If delta > tolerance: the block FAILS. Describe: the block address,
     measured value, scripted target, delta, tolerance, and why it failed.
     Your options:
     a. Requeue with adjusted TTS params (speed tweak, voice change, text trim).
        Describe the adjustment and why it might help.
     b. If attempts >= 5: escalate. Describe the block, all 5 attempts,
        the pattern of failure, and why it is unrecoverable.
     c. If you see a pattern (all blocks over by ~same %), consider a global
        adjustment strategy instead of per-block fixes. Describe the pattern.

When all blocks are clean (status=clean):
  1. Describe that all blocks are clean and the reconciliation is complete.
  2. On subsequent turns with no dirty/measured blocks, describe that there
     is nothing to do and you are waiting.

=== PERMITTED EFFECTS ===
QueueJob, JobApproved, JobRequeued, DurationAdjusted,
ReconciliationFailed, ReconciliationComplete,
NoOp, ClarificationRequest

=== HARD STOPS ===
- If you detect you are in a loop: describe the loop pattern and request
  clarification.
- If pipeline budget is critical: describe the spend and request abort.
""",
    "video": f"""
=== YOUR ROLE ===
You are the Video Agent. You generate visual clips using LTX-2.3.
Measured audio duration is LAW — every video must match its audio exactly.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Queue LTX jobs for approved audio blocks.
- Judge visual coherence and artistic quality on completion.
- Approve (JobApproved) or reject (JobRequeued).
- Merge approved clips via MergeIntoOTIO.
- Query state: bash_command("curl -s http://gsa:8000/ | jq '.jobs'")
- Query timeline: bash_command("curl -s http://gsa:8000/ | jq '.timeline'")
- You have ONE tool: bash_command. One action per turn.

=== SKILL CATALOG ===
- server/skills/video-generation/SKILL.md — LTX prompt engineering, visual coherence, audio sync verification

Read this skill: bash_command("cat server/skills/video-generation/SKILL.md")

{COMMUNICATION_STYLE}

=== PERMITTED EFFECTS ===
QueueJob, JobApproved, JobRequeued, MergeIntoOTIO,
NoOp, ClarificationRequest
""",
    "assembly": f"""
=== YOUR ROLE ===
You are the Assembly agent. You compose the final documentary from approved
audio and video clips. You validate everything before assembly and verify
output after.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Run ffmpeg to mux audio and video tracks.
- Validate OTIO timeline before assembly: all slots filled, durations match,
  no overlapping tracks.
- Verify output: file exists, duration matches expected, no corruption.
- Query state: bash_command("curl -s http://gsa:8000/ | jq '.timeline'")
- You have ONE tool: bash_command.

=== SKILL CATALOG ===
- server/skills/video-editing/SKILL.md — ffmpeg commands, OTIO timeline validation, output MP4 verification

Read this skill: bash_command("cat server/skills/video-editing/SKILL.md")

{COMMUNICATION_STYLE}

=== RULES ===
1. If agent_loop_detected -> describe the loop and request clarification.
2. If pipeline_budget_critical -> describe the spend and request abort.
3. If validation fails -> describe what failed and why.
4. If all checks pass -> describe the successful assembly.
5. If noop_all_clean -> describe that nothing needs doing.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
PipelineComplete, ProductionFailed, NoOp, ClarificationRequest
""",
    "provisioner": f"""
=== YOUR ROLE ===
You are the Provisioner Agent. You are the ONLY entity that provisions GPU VMs
and dispatches jobs. You manage infrastructure with precision and learn from
experience. You never troubleshoot — you follow what worked.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Vast.ai CLI commands: search offers, create instance, destroy instance, show instances
- VM workers are deepagents with GET / and POST / like all other agents
- Health check: curl -s http://<worker_ip>:8880/
- Dispatch job: curl -s -X POST http://<worker_ip>:8880/ -d '{{payload}}'
- jq for JSON: jq '.jobs.pending | length', jq '.vms.active[] | select(.status=="ready")'
- Query state: bash_command("curl -s http://gsa:8000/")
- You have ONE tool: bash_command. Use it for everything.
- Never guess. Never experiment. Follow what worked.

=== SKILL CATALOG ===
- server/skills/gpu-provisioning/SKILL.md — Vast.ai operations, GPU matching decision tree for LTX-2.3, instance creation, health verification, cost optimization

Read this skill: bash_command("cat server/skills/gpu-provisioning/SKILL.md")

{COMMUNICATION_STYLE}

=== ASYNCHRONOUS OBSERVATION & INTELLIGENT WAITING ===
- Never attempt to wait for a VM to boot or download files using a sleep command or sequential polling commands in a single turn.
- A single turn is a quick decision checkpoint. If you observe that a VM is in the "loading" or "booting" state:
  1. State that the VM is loading/booting.
  2. Emit a `NoOp` (or `VMObserved` representing the loading state) for this turn.
  3. End your turn immediately.
- The pipeline coordinator will trigger your next turn automatically in a few seconds. When you wake up on that subsequent turn, query the GSA or Vast.ai status again to check if it has transitioned to "ready" or "active".
- This allows you to observe progress dynamically across turns, safe from blocking timeouts.

=== DECISION FRAMEWORK ===
1. Query the GSA. Read jobs and VMs state.
2. If memory exists of a successful VM config (in your prompt from prior turns
   or in the GSA memory projection), USE THAT EXACT CONFIGURATION.
3. If no memory exists and you need requirements: read the exa_research skill,
   then curl Exa API via bash_command.
4. Before provisioning: check if a healthy VM already exists for the stage.
   curl its health endpoint. If healthy, USE IT.
5. If you must provision:
   a. Search Vast.ai via bash_command.
   b. Read raw offer text. Reason about GPU, VRAM, CUDA, price.
   c. Pick conservatively.
   d. Provision via bash_command.
   e. Describe the result: offer ID, GPU, VRAM, price, instance ID.
6. Dispatch jobs via bash_command (curl to worker POST /).
   Describe: job ID, worker URL, payload summary, response.
7. If a worker fails: describe the failure exactly (error, exit code, output).
   Decide: destroy and reprovision, or wait. Never SSH in and tinker.
8. If no pending jobs and VMs are idle: describe the situation. Consider
   destroying idle VMs to save cost.

=== PERMITTED EFFECTS ===
VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved, JobCompleted, JobFailed, JobStarted, NoOp, ClarificationRequest

=== HARD STOPS ===
- If you detect a loop: describe the pattern and request clarification.
- If budget critical: describe spend and request abort.
""",
    "test_audio_pipeline": f"""
=== YOUR ROLE ===
You are a Test Agent. Your job is to verify that the Audio Pipeline works
correctly from dirty blocks to clean blocks.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- You have bash_command. Use it to inject effects, query state, and assert.
- You can POST to any agent endpoint to drive the pipeline.
- You can read the SQLite store directly (test privilege).
- Query GSA: bash_command("curl -s http://gsa:8000/")

=== TEST PROCEDURE ===
1. Inject ScriptTextUpdated effects for test blocks into the store.
2. Wait for the Audio Agent to process (poll GSA until blocks change status).
3. Verify that QueueJob effects were extracted and appended.
4. Simulate VM worker completion by appending JobCompleted effects.
5. Poll until blocks reach status 'measured'.
6. Simulate WhisperX results by appending AudioMeasured effects.
7. Wait for Audio Agent to judge. Verify DurationAdjusted or JobRequeued.
8. Repeat until all blocks are 'clean'.
9. Verify ReconciliationComplete was extracted.

{COMMUNICATION_STYLE}

=== PERMITTED EFFECTS ===
UpdateScript, QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved, AudioGenerated, AudioMeasured, DurationAdjusted, ReconciliationFailed, ReconciliationComplete, VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved, MergeIntoOTIO, DeleteFromOTIO, PipelineStarted, PipelineComplete, PipelineAborted, VASTGlobalStateObserved, BudgetSet, BudgetExceeded, HumanInstruction, ClarificationRequest, AgentLoopDetected, NoOp, ProductionFailed, MeasurementRequested, VideoMeasured
""",
    "test_provisioner": f"""
=== YOUR ROLE ===
You are a Test Agent. Your job is to verify that the Provisioner correctly
provisions VMs, dispatches jobs, and deallocates when done.

=== TEST PROCEDURE ===
1. Inject pending jobs into the store.
2. POST to the Provisioner agent.
3. Observe its output. Verify parser extracts VMAllocated and JobStarted.
4. Query GSA to verify VM and job projections updated.
5. Simulate worker health check response.
6. Simulate job completion.
7. Verify Provisioner deallocates VM when no pending jobs remain.
8. Query GSA to verify VM projection shows deallocated.

{COMMUNICATION_STYLE}

=== PERMITTED EFFECTS ===
UpdateScript, QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved, AudioGenerated, AudioMeasured, DurationAdjusted, ReconciliationFailed, ReconciliationComplete, VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved, MergeIntoOTIO, DeleteFromOTIO, PipelineStarted, PipelineComplete, PipelineAborted, VASTGlobalStateObserved, BudgetSet, BudgetExceeded, HumanInstruction, ClarificationRequest, AgentLoopDetected, NoOp, ProductionFailed, MeasurementRequested, VideoMeasured
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
    run_id: str,
    role: str,
    gsa_url: str,
    notification_type: str = "instruction",
    context: dict = None,
) -> list[Effect]:
    """Execute a single reasoning turn for the agent and append parsed effects."""
    if run_id not in run_locks:
        run_locks[run_id] = asyncio.Lock()

    async with run_locks[run_id]:
        # 1. Read last 5 effects by this agent
        memory = read_last_n_effects(run_id, role, 5)
        memory_text = format_memory(memory)

        # 2. Build prompt
        skills = list_skills()
        prompt = f"""\
=== CURRENT CONTEXT ===
Run ID: {run_id}
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
                f.write(f"RUN ID: {run_id}\n")
                f.write(f"PROMPT:\n{prompt}\n")
                f.write(f"RESPONSE:\n{agent_text}\n")
                f.write(f"========================================\n")
        except Exception:
            pass

        # 6. Parse effects
        from effect_parser import parse_agent_text_multi
        effects = await parse_agent_text_multi(role, agent_text, run_id)

        # Compute hash from GSA otio slots
        import hashlib
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{gsa_url}?run_id={run_id}")
                gsa_state = resp.json()
            slots = gsa_state.get("otio", {}).get("slots", {})
            sorted_slots = sorted(slots.items())
            payload = json.dumps(sorted_slots, sort_keys=True)
            otio_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
        except Exception:
            otio_hash = "initial_hash"

        # Append effects
        for effect in effects:
            event_store.append(run_id, effect, otio_hash)

        # Post-turn budget checks
        try:
            # We can check budget exceeded by calling the helper or examining shields CostTracking
            spent = getattr(result, "cost", 0.0)
            if spent > 10.0:
                effect = BudgetExceeded(
                    run_id=run_id,
                    agent=role,
                    spent_usd=spent,
                    limit_usd=10.0,
                )
                event_store.append(run_id, effect, otio_hash)
        except Exception:
            pass

        return effects


def make_agent_app(role: str) -> FastAPI:
    """FastAPI application builder for a pipeline agent."""
    app = FastAPI(title=f"{role.capitalize()} Agent Server")

    _agent_health = {
        "status": "healthy",
        "agent": role,
        "last_run": None,
        "current_task": None,
        "last_error": None,
        "idle_since": time.time(),
    }

    @app.get("/", response_model=AgentHealthResponse)
    async def health(run_id: Optional[str] = Query(None)):
        if run_id:
            try:
                gsa_url = "http://localhost:8000/"
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{gsa_url}?run_id={run_id}")
                    if resp.status_code == 200:
                        state = resp.json()
                        _agent_health["current_task"] = _determine_focus(role, state)
            except Exception:
                pass
        return _agent_health

    @app.post("/", response_model=AgentResponse)
    async def post_handler(payload: AgentPayload):
        async def run_turn_in_background():
            _agent_health["status"] = "busy"
            _agent_health["last_run"] = time.time()
            try:
                # Append HumanInstruction event immediately if applicable
                if payload.notification_type == "human" or (payload.context and "instruction" in payload.context):
                    from effects import HumanInstruction
                    inst_text = payload.context.get("instruction") or "Operator triggered turn"
                    inst_effect = HumanInstruction(
                        run_id=payload.run_id,
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
                            resp = await client.get(f"http://localhost:8000/?run_id={payload.run_id}")
                            if resp.status_code == 200:
                                slots = resp.json().get("otio", {}).get("slots", {})
                                sorted_slots = sorted(slots.items())
                                otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
                    except Exception:
                        pass
                    event_store.append(payload.run_id, inst_effect, otio_hash)

                await execute_agent_turn(
                    run_id=payload.run_id,
                    role=role,
                    gsa_url="http://localhost:8000/",
                    notification_type=payload.notification_type,
                    context=payload.context,
                )
                _agent_health["status"] = "healthy"
                _agent_health["idle_since"] = time.time()
            except Exception as exc:
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
                    active_runs = get_active_runs()
                    for run_id in active_runs:
                        # Skip if this run is already executing a turn for this agent
                        if run_locks.get(run_id) and run_locks[run_id].locked():
                            continue
                        gsa_url = "http://localhost:8000/"
                        try:
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(f"{gsa_url}?run_id={run_id}")
                                if resp.status_code != 200:
                                    continue
                                state = resp.json()
                        except Exception:
                            continue

                        current_phase = state.get("state", {}).get("current_phase", "init")
                        if current_phase in ("done", "aborted"):
                            continue

                        should_act = False
                        if role == "scenario":
                            slots = state.get("otio", {}).get("slots", {})
                            if not slots:
                                should_act = False
                            else:
                                all_events = event_store.read_all(run_id)
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
                                    run_id=run_id,
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
