from __future__ import annotations

import os
os.environ["MEM0_TELEMETRY"] = "False"
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
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_deep import create_deep_agent, DeepAgentDeps, PeriodicReminderConfig, create_sliding_window_processor

from effects import (
    Effect, BudgetExceeded, KIND_TO_MODEL, active_agent_var,
    log_trace_effect, run_subprocess_logged, ProcessSpawned,
    CommandExecuted, FileWritten, NetworkRequest
)
from event_store import EventStore
from effect_parser import parse_agent_text_multi
from config_schema import PipelineConfig

# Patch httpx to log NetworkRequest effects
import httpx
_original_async_send = httpx.AsyncClient.send
_original_sync_send = httpx.Client.send

async def _patched_async_send(self, request, *args, **kwargs):
    try:
        url_str = str(request.url)
        if any(x in url_str for x in ["localhost", "127.0.0.1"]):
            request.extensions["timeout"] = {
                "connect": None,
                "read": None,
                "write": None,
                "pool": None
            }
    except Exception:
        pass
    resp = await _original_async_send(self, request, *args, **kwargs)
    try:
        agent = active_agent_var.get()
        url_str = str(request.url)
        if not any(x in url_str for x in ["localhost", "127.0.0.1", "gsa:"]):
            log_trace_effect(NetworkRequest(
                agent=agent,
                url=url_str,
                method=request.method,
                status_code=resp.status_code
            ))
    except Exception:
        pass
    return resp

def _patched_sync_send(self, request, *args, **kwargs):
    try:
        url_str = str(request.url)
        if any(x in url_str for x in ["localhost", "127.0.0.1"]):
            request.extensions["timeout"] = {
                "connect": None,
                "read": None,
                "write": None,
                "pool": None
            }
    except Exception:
        pass
    resp = _original_sync_send(self, request, *args, **kwargs)
    try:
        agent = active_agent_var.get()
        url_str = str(request.url)
        if not any(x in url_str for x in ["localhost", "127.0.0.1", "gsa:"]):
            log_trace_effect(NetworkRequest(
                agent=agent,
                url=url_str,
                method=request.method,
                status_code=resp.status_code
            ))
    except Exception:
        pass
    return resp

httpx.AsyncClient.send = _patched_async_send
httpx.Client.send = _patched_sync_send


# Setup python path to allow importing config.py from root
import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger(__name__)

def get_active_log_dir() -> str:
    print(f"DEBUG_SYS_ARGV: {sys.argv}", file=sys.stderr, flush=True)
    # First search CLI arguments for a directory containing run_config.json
    for arg in sys.argv:
        if arg and os.path.isdir(arg):
            if os.path.exists(os.path.join(arg, "run_config.json")):
                return arg
    
    path = "/tmp/active_pipeline_log_dir.txt"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
    return "/tmp/documentary-pipeline"

def get_active_ports() -> dict[str, int]:
    log_dir = get_active_log_dir()
    config_path = os.path.join(log_dir, "run_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "ports" in data:
                    return data["ports"]
        except Exception:
            pass

    path = "/tmp/active_pipeline_ports.json"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

# Base path of DB files
LOG_DIR = get_active_log_dir()
event_store = EventStore(log_dir=LOG_DIR)
latest_monologues = {}
active_tasks: dict[str, asyncio.Task[Any]] = {}

class AgentRegistry:
    DEFAULT_PORTS = {
        "gsa": 8000,
        "scenario": 8001,
        "audio": 8002,
        "provisioner": 8003,
        "video": 8004,
        "assembly": 8005,
    }

    @classmethod
    def get_port(cls, role: str) -> int:
        role = role.lower()
        ports = get_active_ports()
        if role in ports:
            try:
                return int(ports[role])
            except ValueError:
                pass
        return cls.DEFAULT_PORTS.get(role, 8000)

    @classmethod
    def get_url(cls, role: str) -> str:
        port = cls.get_port(role)
        return f"http://127.0.0.1:{port}/"

def get_gsa_url() -> str:
    return AgentRegistry.get_url("gsa")

async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)

class LoopBoundLock:
    def __init__(self):
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def get_lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Lock()
        if loop not in self._locks:
            self._locks[loop] = asyncio.Lock()
        return self._locks[loop]

run_lock_manager = LoopBoundLock()


@dataclass
class PipelineDeps(DeepAgentDeps):
    """Dependencies for pipeline agents."""
    gsa_url: str = get_gsa_url()
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
        
    base_url = "https://api.deepseek.com/v1"
    
    from pydantic_ai.providers.deepseek import DeepSeekProvider
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=base_url, api_key=api_key or "mock_key", timeout=60.0)
    provider_instance = DeepSeekProvider(openai_client=client)
    return OpenAIChatModel(
        "deepseek-chat",
        provider=provider_instance,
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


async def bash_command(ctx, command: str, agent: str = "unknown") -> str:
    """Run a bash command locally with a fallback host resolution for gsa."""
    import socket
    import signal
    import hashlib
    if agent == "unknown":
        agent = active_agent_var.get()

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
        preexec_fn=os.setsid,
    )
    
    # Log ProcessSpawned
    target = command.strip().split()[0] if command.strip() else "bash"
    log_trace_effect(ProcessSpawned(
        agent=agent,
        target=target,
        pid=proc.pid
    ))

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        out_str = stdout.decode(errors="replace")
        err_str = stderr.decode(errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else 0
        
        # Log CommandExecuted
        h = hashlib.sha256(stdout + stderr).hexdigest()
        log_trace_effect(CommandExecuted(
            agent=agent,
            command=command,
            exit_code=exit_code,
            stdout_hash=h
        ))
        
        # Scan arguments for written files
        for token in command.split():
            if token.endswith(".mp4") or token.endswith(".wav") or token.endswith(".pcm") or token.endswith(".txt"):
                cleaned_token = token.strip("'\"")
                if os.path.exists(cleaned_token):
                    size = os.path.getsize(cleaned_token)
                    log_trace_effect(FileWritten(
                        agent=agent,
                        filepath=os.path.abspath(cleaned_token),
                        size_bytes=size
                    ))

        return out_str + err_str
    except asyncio.TimeoutError:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass
        log_trace_effect(CommandExecuted(
            agent=agent,
            command=command,
            exit_code=-1,
            stdout_hash=""
        ))
        return "[TIMEOUT] Command timed out after 15.0 seconds."
    except asyncio.CancelledError:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass  # process already dead
        log_trace_effect(CommandExecuted(
            agent=agent,
            command=command,
            exit_code=-2,
            stdout_hash=""
        ))
        raise



# ===========================================================================
# System Prompts & Instructions for all Roles (Loaded dynamically from Vault)
# ===========================================================================

def _load_prompts() -> dict[str, str]:
    vault_prompts_dir = os.path.join(Path(__file__).resolve().parent.parent, "obsidian-vault", "prompts")
    
    # 1. Load global communication style
    style_path = os.path.join(vault_prompts_dir, "global_communication_style.md")
    try:
        with open(style_path, encoding="utf-8") as f:
            comm_style = f.read().strip()
    except Exception as e:
        logger.error(f"Failed to load communication style: {e}")
        comm_style = ""

    # 2. Load agent prompts
    roles = ["scenario", "audio", "video", "assembly", "provisioner", "test_audio_pipeline", "test_provisioner"]
    instructions = {}
    for role in roles:
        filename = f"{role}_agent_system_prompt.md" if role in ("scenario", "audio", "video", "assembly", "provisioner") else f"{role}_system_prompt.md"
        full_path = os.path.join(vault_prompts_dir, filename)
        try:
            with open(full_path, encoding="utf-8") as f:
                content = f.read().strip()
            # Replace placeholder
            content = content.replace("{COMMUNICATION_STYLE}", comm_style)
            instructions[role] = content
        except Exception as e:
            logger.error(f"Failed to load system prompt for {role}: {e}")
            instructions[role] = ""
    return instructions

ROLE_INSTRUCTIONS = _load_prompts()


def create_pipeline_agent(
    role: str,
    model_instance: OpenAIChatModel,
    extra_capabilities: list[AbstractCapability] | None = None,
) -> Any:
    """Create a pipeline deep agent with all required capabilities."""
    provenance = ProvenanceCapability(
        agent_name=role,
        source_tools=["bash_command"],
    )
    caps = [
        provenance,
        ContextManagerCapability(
            max_tokens=128000,
        ),
        CostTracking(budget_usd=10.0),
    ]
    if extra_capabilities:
        caps.extend(extra_capabilities)

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
        web_fetch=False,
        include_skills=True,
        include_subagents=True,
        include_builtin_subagents=True,
        skill_directories=[os.path.join(Path(__file__).resolve().parent.parent, "obsidian-vault", "prompts")],
        thinking=False,
        cost_tracking=True,
        cost_budget_usd=10.0,
        stuck_loop_detection=True,
        periodic_reminder=PeriodicReminderConfig(every_n_turns=10, first_after=5),
        capabilities=caps,
        deps_type=PipelineDeps,
    )
    return agent


def get_local_mem0(role: str | None = None):
    """Initialize local Mem0 Memory instance using Gemini embeddings and DeepSeek LLM."""
    try:
        from mem0 import Memory
        import os
        gemini_key_path = "/Users/orpington/api_keys/LLMS/gemini_api_key.txt"
        deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
        if not os.path.exists(gemini_key_path) or not os.path.exists(deepseek_key_path):
            logger.error("Required API keys for local Mem0 configuration are missing.")
            return None
            
        with open(gemini_key_path) as f:
            gemini_api_key = f.read().strip()
        with open(deepseek_key_path) as f:
            deepseek_api_key = f.read().strip()
            
        role_suffix = f"_{role}" if role else ""
        config = {
            "embedder": {
                "provider": "gemini",
                "config": {
                    "model": "models/gemini-embedding-001",
                    "api_key": gemini_api_key
                }
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "deepseek-chat",
                    "api_key": deepseek_api_key,
                    "openai_base_url": "https://api.deepseek.com/v1"
                }
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": f"agent_memories{role_suffix}",
                    "path": os.path.join(LOG_DIR, f"mem0_qdrant{role_suffix}"),
                    "embedding_model_dims": 768
                }
            }
        }
        return Memory.from_config(config)
    except Exception as exc:
        logger.error(f"Failed to initialize local Mem0 instance: {exc}")
        return None


from contextlib import asynccontextmanager
from typing import AsyncIterator
from pydantic_ai.models import Model, StreamedResponse
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, TextPart, ModelRequest, ToolReturnPart

class DryRunModel(Model):
    def __init__(self, role: str):
        self.role = role

    @property
    def model_name(self) -> str:
        return "dry_run_model"

    @property
    def system(self) -> str:
        return "dry_run"

    async def _request(
        self,
        messages: list[ModelMessage],
        model_settings: Any,
        model_request_parameters: Any,
    ) -> ModelResponse:
        import logging
        logger = logging.getLogger("DryRunModel")
        logger.error(f"DEBUG: DryRunModel messages: {messages}")
        for i, msg in enumerate(messages):
            logger.error(f"  msg {i}: {type(msg)}")
            if hasattr(msg, "parts"):
                for j, part in enumerate(msg.parts):
                    logger.error(f"    part {j}: {type(part)} -> {part}")

        tool_returns = []
        for msg in messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        tool_returns.append(part)

        step = len(tool_returns)

        if self.role == "scenario":
            text = (
                "effect: update_script(blocks=[\n"
                "  {\n"
                "    \"scene_num\": 1,\n"
                "    \"block_id\": \"s1_b1\",\n"
                "    \"speaker\": \"narrator\",\n"
                "    \"text\": \"Dopamine drives motivation.\",\n"
                "    \"duration_sec\": 3.0\n"
                "  }\n"
                "])"
            )
            return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")

        elif self.role == "audio":
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(get_gsa_url())
                    if resp.status_code == 200:
                        state_data = resp.json()
                        slots = state_data.get("otio", {}).get("slots", {})
                        jobs = state_data.get("jobs", {}).get("jobs", {}).values()
                        
                        # Find all audio slots (A1:...)
                        audio_slots = {k: v for k, v in slots.items() if k.startswith("A1:")}
                        sorted_keys = sorted(audio_slots.keys())
                        
                        # Check if any slot needs a job queued
                        for slot_key in sorted_keys:
                            slot = audio_slots[slot_key]
                            scene_num = slot.get("scene_num", 1)
                            block_id = slot.get("block_id", "s1_b1")
                            
                            # Find if job exists for this slot
                            matching_job = None
                            for j in jobs:
                                if j.get("job_type") == "tts" and (
                                    j.get("slot_id") == slot_key 
                                    or j.get("slot_id") == block_id 
                                    or j.get("job_id") == f"job_audio_{block_id}"
                                ):
                                    matching_job = j
                                    break
                                    
                            if not matching_job:
                                # Queue TTS job
                                text = (
                                    "effect: queue_audio_job(\n"
                                    f"  job_id=\"job_audio_{block_id}\",\n"
                                    f"  scene_num={scene_num},\n"
                                    f"  block_id=\"{block_id}\",\n"
                                    f"  slot_id=\"{slot_key}\",\n"
                                    f"  params={{\"text\": {json.dumps(slot.get('text', ''))}, \"voice\": \"{slot.get('speaker', 'narrator')}\", \"gpu_type\": \"RTX 4090\"}}\n"
                                    ")"
                                )
                                return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                        
                        # All TTS jobs have been queued. Let's see if any needs duration adjustment.
                        for slot_key in sorted_keys:
                            slot = audio_slots[slot_key]
                            scene_num = slot.get("scene_num", 1)
                            block_id = slot.get("block_id", "s1_b1")
                            
                            if slot.get("status") == "scripted":
                                # Find the completed job
                                completed_job = None
                                for j in jobs:
                                    if j.get("job_type") == "tts" and j.get("status") == "completed" and (
                                        j.get("slot_id") == slot_key 
                                        or j.get("slot_id") == block_id 
                                        or j.get("job_id") == f"job_audio_{block_id}"
                                    ):
                                        completed_job = j
                                        break
                                if completed_job:
                                    measured_sec = completed_job.get("duration_sec", slot.get("scripted_sec", 3.0))
                                    text = (
                                        f"effect: duration_adjusted(\n"
                                        f"  block_id=\"{slot_key}\",\n"
                                        f"  slot_id=\"{slot_key}\",\n"
                                        f"  scene_num={scene_num},\n"
                                        f"  voice_role=\"{slot.get('speaker', 'narrator')}\",\n"
                                        f"  scripted_sec={slot.get('scripted_sec', 3.0)},\n"
                                        f"  measured_sec={measured_sec}\n"
                                        f")"
                                    )
                                    return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                        
                        # All are measured. Let's check if reconciliation is complete.
                        all_measured = all(slot.get("status") == "measured" for slot in audio_slots.values())
                        if all_measured:
                            reconciled = state_data.get("jobs", {}).get("reconciliation_complete", False)
                            if not reconciled:
                                blocks_total = len(audio_slots)
                                total_measured = sum(slot.get("measured_sec") or slot.get("scripted_sec", 3.0) for slot in audio_slots.values())
                                text = (
                                    "effect: reconciliation_complete(\n"
                                    f"  blocks_total={blocks_total},\n"
                                    f"  blocks_passed={blocks_total},\n"
                                    f"  blocks_failed=0,\n"
                                    f"  worst_delta_sec=0.0,\n"
                                    f"  total_measured_sec={total_measured}\n"
                                    ")"
                                )
                                return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                            else:
                                text = "effect: noop(reason=\"audio_complete\")\nAudio reconciliation is complete."
                                return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                        else:
                            text = "effect: noop(reason=\"waiting_for_tts_jobs\")\nWaiting for all TTS jobs to complete and adjust durations."
                            return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
            except Exception as e:
                text = f"effect: noop(reason=\"audio_error_{str(e)[:30]}\")"
                return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")

        elif self.role == "video":
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(get_gsa_url())
                    if resp.status_code == 200:
                        state_data = resp.json()
                        slots = state_data.get("otio", {}).get("slots", {})
                        jobs = state_data.get("jobs", {}).get("jobs", {}).values()
                        
                        # Find all audio and video slots
                        audio_slots = {k: v for k, v in slots.items() if k.startswith("A1:")}
                        video_slots = {k: v for k, v in slots.items() if k.startswith("V1:")}
                        sorted_keys = sorted(video_slots.keys())
                        
                        for slot_key in sorted_keys:
                            slot = video_slots[slot_key]
                            scene_num = slot.get("scene_num", 1)
                            block_id = slot.get("block_id", "s1_b1")
                            
                            # Find matching job
                            matching_job = None
                            for j in jobs:
                                if j.get("job_type") == "ltx" and (
                                    j.get("slot_id") == slot_key 
                                    or j.get("slot_id") == block_id 
                                    or j.get("job_id") == f"job_video_{block_id}"
                                ):
                                    matching_job = j
                                    break
                                    
                            # Determine expected duration matching corresponding audio
                            audio_slot_key = f"A1:{scene_num}:{block_id}"
                            audio_slot = audio_slots.get(audio_slot_key, {})
                            audio_duration = audio_slot.get("measured_sec") or audio_slot.get("scripted_sec") or 4.0
                            
                            if not matching_job:
                                # Queue video job
                                job_id = f"job_video_{block_id}"
                                text = (
                                    "effect: queue_video_job(\n"
                                    f"  job_id=\"{job_id}\",\n"
                                    f"  scene_num={scene_num},\n"
                                    f"  block_id=\"{block_id}\",\n"
                                    f"  slot_id=\"{slot_key}\",\n"
                                    f"  params={{\"text\": {json.dumps(slot.get('text', ''))}, \"duration_sec\": {audio_duration}, \"gpu_type\": \"RTX A6000\"}}\n"
                                    ")"
                                )
                                return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                        
                        # Check for merging
                        for slot_key in sorted_keys:
                            slot = video_slots[slot_key]
                            scene_num = slot.get("scene_num", 1)
                            block_id = slot.get("block_id", "s1_b1")
                            
                            if slot.get("status") != "delivered":
                                # Find completed job
                                completed_job = None
                                for j in jobs:
                                    if j.get("job_type") == "ltx" and j.get("status") == "completed" and (
                                        j.get("slot_id") == slot_key 
                                        or j.get("slot_id") == block_id 
                                        or j.get("job_id") == f"job_video_{block_id}"
                                    ):
                                        completed_job = j
                                        break
                                if completed_job:
                                    audio_slot_key = f"A1:{scene_num}:{block_id}"
                                    audio_slot = audio_slots.get(audio_slot_key, {})
                                    audio_duration = audio_slot.get("measured_sec") or audio_slot.get("scripted_sec") or 4.0
                                    duration_sec = completed_job.get("duration_sec") or audio_duration
                                    
                                    db_dir = state_data.get("state", {}).get("config", {}).get("log_dir")
                                    if not db_dir:
                                        db_dir = get_active_log_dir()
                                    job_id = f"job_video_{block_id}"
                                    artifact_uri = f"{db_dir}/video_outputs/{job_id}.mp4"
                                    
                                    text = (
                                        "effect: merge_into_otio(\n"
                                        f"  job_id=\"{job_id}\",\n"
                                        f"  block_id=\"{slot_key}\",\n"
                                        f"  scene_num={scene_num},\n"
                                        f"  slot_id=\"{slot_key}\",\n"
                                        f"  artifact_uri=\"{artifact_uri}\",\n"
                                        f"  track_name=\"V1_Video\",\n"
                                        f"  duration_sec={duration_sec}\n"
                                        ")"
                                    )
                                    return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                        
                        # Check if all video slots are delivered
                        all_delivered = all(slot.get("status") == "delivered" for slot in video_slots.values())
                        if all_delivered:
                            text = "effect: noop(reason=\"video_already_merged\")\nVideo is already merged."
                            return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                        else:
                            text = "effect: noop(reason=\"waiting_for_video_jobs\")\nWaiting for video jobs to complete and merge."
                            return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
            except Exception as e:
                text = f"effect: noop(reason=\"video_error_{str(e)[:30]}\")"
                return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")

        elif self.role == "provisioner":
            active_vms = False
            active_vm_id = None
            active_vm_role = None
            active_roles = set()
            all_completed = False
            pending_jobs = []
            preempted_vm_id = None
            state_data = {}
            slots = {}
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(get_gsa_url())
                    if resp.status_code == 200:
                        state_data = resp.json()
                        active_vms = state_data.get("vms", {}).get("active_count", 0) > 0
                        for v in state_data.get("vms", {}).get("vms", {}).values():
                            if v.get("status") == "active":
                                active_vm_id = v.get("instance_id")
                                active_vm_role = v.get("role")
                                if v.get("role"):
                                    active_roles.add(v.get("role"))
                            if v.get("status") == "observed_gone" or v.get("observed_status") == "not_found":
                                if v.get("status") != "destroyed":
                                    preempted_vm_id = v.get("instance_id")
                        jobs = list(state_data.get("jobs", {}).get("jobs", {}).values())
                        pending_jobs = [j for j in jobs if j.get("status") in ("pending", "running", "failed")]
                        slots = state_data.get("otio", {}).get("slots", {})
                        if active_vm_role == "tts":
                            audio_slots = [s for k, s in slots.items() if k.startswith("A1:")]
                            all_completed = len(audio_slots) > 0 and all(s.get("status") == "measured" for s in audio_slots)
                        elif active_vm_role == "ltx":
                            video_slots = [s for k, s in slots.items() if k.startswith("V1:")]
                            all_completed = len(video_slots) > 0 and all(s.get("status") == "delivered" for s in video_slots)
                        else:
                            all_completed = False
            except Exception:
                pass

            if preempted_vm_id:
                if step == 0:
                    return ModelResponse(parts=[
                        ToolCallPart("run_bash", {"command": f"vastai destroy instance {preempted_vm_id}"}, tool_call_id="call_destroy")
                    ], model_name="dry_run_model")
                else:
                    text = f"effect: vm_deallocated(instance_id=\"{preempted_vm_id}\", reason=\"stale\")\nVM deallocated."
                    return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")

            # Check if there is any pending job type that does not have an active VM
            unserved_roles = []
            for j in pending_jobs:
                jt = j.get("job_type")
                if jt and jt not in active_roles and jt not in unserved_roles:
                    unserved_roles.append(jt)

            if unserved_roles:
                vm_role = unserved_roles[0]
                gpu_type = "RTX 4090"
                for j in pending_jobs:
                    if j.get("job_type") == vm_role:
                        gpu_type = j.get("params", {}).get("gpu_type") or ("RTX A6000" if vm_role == "ltx" else "RTX 4090")
                        break

                new_instance_id = "7654321" if "1234567" in state_data.get("vms", {}).get("vms", {}) else "1234567"

                if step == 0:
                    return ModelResponse(parts=[
                        ToolCallPart("run_bash", {"command": "vastai search offers"}, tool_call_id="call_search")
                    ], model_name="dry_run_model")
                elif step == 1:
                    return ModelResponse(parts=[
                        ToolCallPart("run_bash", {"command": f"vastai create instance 1001 --image worker-{vm_role}"}, tool_call_id="call_create")
                    ], model_name="dry_run_model")
                elif step == 2:
                    if vm_role == "tts":
                        cmd = f"vastai copy b2.36862:/qwen3-tts-voicedesign/ C.{new_instance_id}:/workspace/models/qwen3-tts-voicedesign/"
                    else:
                        cmd = f"vastai copy b2.36862:/ltx-2.3/ C.{new_instance_id}:/workspace/models/ltx23/"
                    return ModelResponse(parts=[
                        ToolCallPart("run_bash", {"command": cmd}, tool_call_id="call_copy")
                    ], model_name="dry_run_model")
                elif step == 3:
                    return ModelResponse(parts=[
                        ToolCallPart("run_bash", {"command": "vastai show instances"}, tool_call_id="call_show")
                    ], model_name="dry_run_model")
                else:
                    text = f"effect: vm_allocated(instance_id=\"{new_instance_id}\", worker_url=\"http://127.0.0.1:8888\", role=\"{vm_role}\", offer_id=\"1001\", gpu_type=\"{gpu_type}\", cost_per_hour=0.40)\nVM allocated and ready."
                    return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
            else:
                idle_vm_to_destroy = None
                for v in state_data.get("vms", {}).get("vms", {}).values():
                    if v.get("status") == "active":
                        role = v.get("role")
                        has_pending_jobs = any(j.get("job_type") == role for j in pending_jobs)
                        if not has_pending_jobs:
                            if role == "tts":
                                audio_slots = [s for k, s in slots.items() if k.startswith("A1:")]
                                role_completed = len(audio_slots) > 0 and all(s.get("status") == "measured" for s in audio_slots)
                            elif role == "ltx":
                                video_slots = [s for k, s in slots.items() if k.startswith("V1:")]
                                role_completed = len(video_slots) > 0 and all(s.get("status") == "delivered" for s in video_slots)
                            else:
                                role_completed = True
                            
                            if role_completed:
                                idle_vm_to_destroy = v.get("instance_id")
                                break

                if idle_vm_to_destroy:
                    if step == 0:
                        return ModelResponse(parts=[
                            ToolCallPart("run_bash", {"command": f"vastai destroy instance {idle_vm_to_destroy}"}, tool_call_id="call_destroy")
                        ], model_name="dry_run_model")
                    else:
                        text = f"effect: vm_deallocated(instance_id=\"{idle_vm_to_destroy}\", reason=\"job_done\")\nVM deallocated."
                        return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                else:
                    matching_pending_jobs = [j for j in pending_jobs if j.get("job_type") in active_roles]
                    if matching_pending_jobs:
                        job_to_dispatch = matching_pending_jobs[0]
                        job_id = job_to_dispatch.get("job_id")
                        role = job_to_dispatch.get("job_type")
                        if step == 0:
                            return ModelResponse(parts=[
                                ToolCallPart("run_bash", {"command": f"curl -X POST http://127.0.0.1:8888/{role}?job_id={job_id}"}, tool_call_id="call_dispatch_more")
                            ], model_name="dry_run_model")
                        else:
                            text = f"effect: noop(reason=\"dispatched_job_{job_id}\")\nDispatched job {job_id}."
                            return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")
                    else:
                        text = "effect: noop(reason=\"vm_active_executing_jobs\")\nVM is active and executing jobs."
                        return ModelResponse(parts=[TextPart(text)], model_name="dry_run_model")

        elif self.role == "assembly":
            output_path = os.path.join(get_active_log_dir(), "final_documentary.mp4")
            if step == 0:
                return ModelResponse(parts=[
                    ToolCallPart("assemble_final_cut", {
                        "output_path": output_path,
                        "timeline_path": "timeline.otio",
                        "include_placeholders": True,
                        "target_duration": 7.0,
                        "run_id": "dry_run"
                    }, tool_call_id="call_assemble")
                ], model_name="dry_run_model")
            else:
                return ModelResponse(parts=[TextPart("effect: noop(reason=\"assembly_complete\")\n")], model_name="dry_run_model")

        return ModelResponse(parts=[TextPart("Dry run turn.")], model_name="dry_run_model")

    async def request(self, messages, model_settings, model_request_parameters):
        return await self._request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        response = await self._request(messages, model_settings, model_request_parameters)
        class DryRunStreamedResponse(StreamedResponse):
            def __init__(self, resp):
                self._resp = resp
            async def __aiter__(self):
                for part in self._resp.parts:
                    yield part
            @property
            def model_name(self) -> str:
                return "dry_run_model"
            @property
            def system(self) -> str:
                return "dry_run"
        yield DryRunStreamedResponse(response)


def run_movie_assembly(
    output_path: str,
    timeline_path: str,
    include_placeholders: bool,
    target_duration: float,
    event_store_instance,
    log_dir: str,
) -> str:
    """Core function to assemble a final cut movie from an OTIO timeline.
    Extracted from the agent tool for independent testability.
    """
    import sys
    import os
    import json
    import subprocess
    import opentimelineio as otio
    from effects import PipelineComplete

    def generate_audio_placeholder(dur: float, out_path: str) -> None:
        print(f"CRITICAL: Generating audio placeholder of duration {dur}s at {out_path}", file=sys.stderr)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        run_subprocess_logged(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=44100", "-t", str(dur), out_path],
            agent="assembly", check=True
        )

    def generate_video_placeholder(dur: float, out_path: str) -> None:
        print(f"CRITICAL: Generating black video placeholder of duration {dur}s at {out_path}", file=sys.stderr)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        run_subprocess_logged(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={dur}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
            agent="assembly", check=True
        )

    def probe_duration(filepath: str) -> float:
        try:
            res = run_subprocess_logged(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                agent="assembly", text=True, check=True
            )
            out = res.stdout
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            return float(out.strip())
        except Exception:
            return 0.0

    try:
        # Load timeline
        if not os.path.exists(timeline_path):
            # Try to resolve relative to log_dir/timelines
            alt_path = os.path.join(log_dir, "timelines", os.path.basename(timeline_path))
            if os.path.exists(alt_path):
                timeline_path = alt_path

        timeline = otio.adapters.read_from_file(timeline_path)
        video_track = None
        audio_track = None
        for track in timeline.tracks:
            if track.name == "V1_Video":
                video_track = track
            elif track.name == "A1_Narration":
                audio_track = track

        video_clips = []
        if video_track:
            for item in video_track:
                if isinstance(item, otio.schema.Clip):
                    path = ""
                    if isinstance(item.media_reference, otio.schema.ExternalReference):
                        path = item.media_reference.target_url
                    elif isinstance(item.media_reference, otio.schema.MissingReference):
                        path = ""

                    if not path or not os.path.exists(path):
                        if include_placeholders:
                            dur = item.source_range.duration.to_seconds()
                            placeholder_path = f"/tmp/placeholders/{item.name}.mp4"
                            generate_video_placeholder(dur, placeholder_path)
                            path = placeholder_path
                        else:
                            raise RuntimeError(f"Missing video media file: {path} for clip {item.name}")
                    video_clips.append(path)

        audio_clips = []
        if audio_track:
            for item in audio_track:
                if isinstance(item, otio.schema.Clip):
                    path = ""
                    if isinstance(item.media_reference, otio.schema.ExternalReference):
                        path = item.media_reference.target_url
                    elif isinstance(item.media_reference, otio.schema.MissingReference):
                        path = ""

                    if not path or not os.path.exists(path):
                        if include_placeholders:
                            dur = item.source_range.duration.to_seconds()
                            placeholder_path = f"/tmp/placeholders/{item.name}.wav"
                            generate_audio_placeholder(dur, placeholder_path)
                            path = placeholder_path
                        else:
                            raise RuntimeError(f"Missing audio media file: {path} for clip {item.name}")
                    audio_clips.append(path)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # 1. Render final video track
        final_video_path = "/tmp/final_video.mp4"
        if len(video_clips) == 1:
            final_video_path = video_clips[0]
        elif len(video_clips) > 1:
            concat_file = "/tmp/concat_video.txt"
            with open(concat_file, "w") as f:
                for c in video_clips:
                    f.write(f"file '{c}'\n")
            run_subprocess_logged(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", final_video_path],
                agent="assembly", check=True
            )
        else:
            generate_video_placeholder(target_duration, final_video_path)

        # 2. Render final audio track
        final_audio_path = "/tmp/final_audio.wav"
        if len(audio_clips) == 1:
            final_audio_path = audio_clips[0]
        elif len(audio_clips) > 1:
            concat_file = "/tmp/concat_audio.txt"
            with open(concat_file, "w") as f:
                for c in audio_clips:
                    f.write(f"file '{c}'\n")
            run_subprocess_logged(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", final_audio_path],
                agent="assembly", check=True
            )
        else:
            generate_audio_placeholder(target_duration, final_audio_path)

        import sys
        sys.__stdout__.write(f"DEBUG ASSEMBLY: final_audio_path={final_audio_path} size={os.path.getsize(final_audio_path) if os.path.exists(final_audio_path) else 'not exist'} final_video_path={final_video_path} size={os.path.getsize(final_video_path) if os.path.exists(final_video_path) else 'not exist'}\n")
        sys.__stdout__.flush()

        # Apply loudness normalization (-16.0 LUFS target, -1.0 dBTP max true peak)
        normalized_audio_path = "/tmp/normalized_audio.wav"
        run_subprocess_logged(
            ["ffmpeg", "-y", "-i", final_audio_path, "-af", "loudnorm=I=-16:TP=-1.0:LRA=11", normalized_audio_path],
            agent="assembly", check=True
        )
        final_audio_path = normalized_audio_path

        sys.__stdout__.write(f"DEBUG ASSEMBLY: normalized_audio_path={normalized_audio_path} size={os.path.getsize(normalized_audio_path) if os.path.exists(normalized_audio_path) else 'not exist'}\n")
        sys.__stdout__.flush()

        # 3. Mux them together
        run_subprocess_logged(
            ["ffmpeg", "-y", "-i", final_video_path, "-i", final_audio_path, "-c:v", "copy", "-c:a", "aac", "-shortest", output_path],
            agent="assembly", check=True
        )

        actual_dur = probe_duration(output_path) or target_duration

        # Emit PipelineComplete effect
        otio_hash = "initial_hash"
        try:
            slots_dict = {}
            for addr, s in timeline.slots.items():
                slots_dict[addr] = s
            import hashlib
            sorted_slots = sorted(slots_dict.items())
            otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
        except Exception:
            pass  # Fall back to default initial_hash on failure

        event_store_instance.append(
            PipelineComplete(
                agent="assembly",
                output_path=output_path,
                duration_sec=actual_dur,
            ),
            otio_hash
        )

        return f"SUCCESS: Final documentary assembled at {output_path}. Duration: {actual_dur}s."

    except Exception as e:
        return f"ERROR: Assembly failed: {e}"


# Capability simulator registry and loaders are removed.
# Test-only capabilities are loaded statically via harness and make_agent_app.


async def execute_agent_turn(
    role: str,
    gsa_url: str,
    notification_type: str = "instruction",
    context: dict[Any, Any] | None = None,
    config: PipelineConfig | None = None,
    extra_capabilities: list[Any] | None = None,
) -> list[Effect]:
    """Execute a single reasoning turn for the agent and append parsed effects."""
    lock = run_lock_manager.get_lock()
    async with lock:
        token = active_agent_var.set(role)
        try:
            # 1. Fetch history and memory from GSA to bypass direct DB read
            gsa_state = {}
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(gsa_url)  # health probe
                    if resp.status_code == 200:
                        gsa_state = resp.json()
            except Exception:
                pass  # GSA not available or returned non-200

            recent_by_agent = gsa_state.get("state", {}).get("recent_effects", {}).get(role, [])
            memory = []
            for e_dict in recent_by_agent:
                try:
                    kind = e_dict.get("kind")
                    if kind in KIND_TO_MODEL:
                        memory.append(KIND_TO_MODEL[kind].model_validate(e_dict))
                except Exception:
                    pass  # Ignore invalid/malformed history models
            memory_text = format_memory(memory)

            lt_memories = []
            mem0_instance = None
            lt_memories = []
            mem0_instance = get_local_mem0(role)
            if mem0_instance:
                try:
                    mem_res = mem0_instance.get_all(filters={"user_id": role})
                    lt_memories = [m["memory"] for m in mem_res.get("results", []) if "memory" in m]
                except Exception as exc:
                    logger.error(f"Failed to fetch long-term memories from local Mem0 for {role}: {exc}")

            lt_memories_text = ""
            if lt_memories:
                lt_memories_text = "\n=== LONG-TERM MEMORY ===\n" + "\n".join(f"- {m}" for m in lt_memories) + "\n"

            # 2. Build prompt
            skills = list_skills()
            prompt = f"""\
=== CURRENT CONTEXT ===
GSA URL: {gsa_url}
Available Skills:
{skills}
{lt_memories_text}
=== RECENT HISTORY ===
{memory_text}
"""
            if context:
                prompt += f"\n=== ADDITIONAL CONTEXT/INSTRUCTION ===\n{json.dumps(context, indent=2)}\n"

            # 3. Create agent and model

            extra_caps = []
            use_dry_run_model = False
            caps_list = []
            if config and config.capabilities:
                caps_list = config.capabilities
            elif gsa_state:
                gsa_config = gsa_state.get("state", {}).get("config", {})
                if isinstance(gsa_config, dict):
                    if gsa_config.get("capabilities"):
                        caps_list = gsa_config.get("capabilities")
                    elif gsa_config.get("simulation_mode") is True:
                        caps_list = [
                            "DryRunModel",
                            "VastSearchSimulator",
                            "VastCreateSimulator",
                            "VastShowSimulator",
                            "VastDestroySimulator",
                            "WorkerHealthSimulator",
                            "TtsJob1Simulator",
                            "TtsColdStartSimulator",
                            "TtsSingleBlockSimulator",
                            "TtsMultiBlockSimulator",
                            "TtsFailSimulator",
                            "TtsPreemptSimulator",
                            "LtxScaleSimulator",
                            "LtxSingleSimulator",
                            "VastCopySimulator",
                            "AssembleFinalCutSimulator"
                        ]

            if caps_list and "DryRunModel" in caps_list:
                use_dry_run_model = True

            if extra_capabilities:
                for cap in extra_capabilities:
                    if isinstance(cap, type):
                        extra_caps.append(cap())
                    else:
                        extra_caps.append(cap)

            model_instance = get_agent_model()
            if use_dry_run_model:
                model_instance = DryRunModel(role)
            agent = create_pipeline_agent(role, model_instance, extra_capabilities=extra_caps)

            # 4. Register tool
            @agent.tool
            async def run_bash(ctx, command: str) -> str:
                """Run an arbitrary bash command on the local machine."""
                return await bash_command(ctx, command, agent=role)

            if role == "assembly":
                @agent.tool
                async def assemble_final_cut(
                    ctx,
                    output_path: str,
                    timeline_path: str,
                    include_placeholders: bool,
                    target_duration: float,
                    run_id: str,
                ) -> str:
                    """Assemble the final documentary cut.

                    Args:
                        output_path: Path to the output mp4 file.
                        timeline_path: Path to the input OTIO timeline file.
                        include_placeholders: Whether to generate placeholders for missing clips.
                        target_duration: Target duration in seconds.
                        run_id: Unique execution run identifier.
                    """
                    return run_movie_assembly(
                        output_path=output_path,
                        timeline_path=timeline_path,
                        include_placeholders=include_placeholders,
                        target_duration=target_duration,
                        event_store_instance=event_store,
                        log_dir=LOG_DIR,
                    )

            # 5. Run the agent
            deps = PipelineDeps(gsa_url=gsa_url, agent_role=role, compaction_model=model_instance)
            from pydantic_ai import UsageLimits
            result = await agent.run(prompt, deps=deps, usage_limits=UsageLimits(request_limit=300))
            agent_text = result.output
            latest_monologues[role] = agent_text

            # Update long-term memories using local Mem0 Memory client
            if mem0_instance:
                try:
                    await asyncio.to_thread(mem0_instance.add, agent_text, user_id=role)
                except Exception as exc:
                    logger.error(f"Failed to update agentic memory for {role}: {exc}")

            try:
                with open(f"/tmp/documentary-pipeline/agent_debug_{role}.log", "a", encoding="utf-8") as f:
                    f.write(f"\n\n========================================\n")
                    f.write(f"TURN START: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"PROMPT:\n{prompt}\n")
                    f.write(f"RESPONSE:\n{agent_text}\n")
                    f.write(f"========================================\n")
            except Exception:
                pass  # Ignore debug log write failures

            # 6. Parse effects
            effects = await parse_agent_text_multi(role, agent_text)

            # Compute hash from GSA otio slots
            import hashlib
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(gsa_url)  # health probe
                    gsa_state = resp.json()
                slots = gsa_state.get("otio", {}).get("slots", {})
                sorted_slots = sorted(slots.items())
                payload = json.dumps(sorted_slots, sort_keys=True)
                otio_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
            except Exception:
                otio_hash = "initial_hash"

            # Append effects
            for effect in effects:
                if effect.kind == "noop":
                    continue
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
                pass  # Ignore exceptions during budget tracking spent computation

            return effects
        finally:
            active_agent_var.reset(token)


class StrictEndpointMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/":
            return PlainTextResponse("Not Found: Only root '/' is permitted", status_code=404)
        if request.method not in ("GET", "POST", "PUT"):
            return PlainTextResponse("Method Not Allowed: Only GET, POST and PUT permitted", status_code=405)
        if request.query_params:
            return PlainTextResponse("Bad Request: Query parameters are prohibited", status_code=400)
        return await call_next(request)


def load_run_config() -> PipelineConfig:
    config_path = os.path.join(LOG_DIR, "run_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                return PipelineConfig.model_validate_json(f.read())
        except Exception:
            pass
    return PipelineConfig()


def make_agent_app(role: str, extra_capabilities: list[Any] | None = None) -> FastAPI:
    """FastAPI application builder for a pipeline agent."""
    app = FastAPI(title=f"{role.capitalize()} Agent Server")
    app.extra_capabilities = extra_capabilities or []
    app.add_middleware(StrictEndpointMiddleware)

    _agent_health = {
        "status": "healthy",
        "agent": role,
        "last_run": None,
        "current_task": None,
        "last_error": None,
        "idle_since": time.time(),
    }

    @app.get("/")
    async def health(request: Request):
        lock = run_lock_manager.get_lock()
        if lock.locked():
            _agent_health["status"] = "busy"
        else:
            _agent_health["status"] = "healthy"
        try:
            gsa_url = get_gsa_url()
            async with httpx.AsyncClient() as client:
                resp = await client.get(gsa_url)  # health probe
                if resp.status_code == 200:
                    state = resp.json()
                    _agent_health["current_task"] = _determine_focus(role, state)
        except Exception:
            pass  # Ignore health check GSA connection failures

        accept = request.headers.get("accept", "")
        if "application/json" in accept:
            return _agent_health

        status = _agent_health.get("status", "healthy")
        task = _agent_health.get("current_task") or "no active task"
        message = f"Hello. I am the {role} agent. Currently, my status is {status}. I am working on: {task}."
        return PlainTextResponse(message, media_type="text/plain")

    @app.post("/")
    async def post_handler(request: Request):
        body = await request.body()
        instruction_text = body.decode("utf-8").strip()

        if not os.path.exists(os.path.join(LOG_DIR, "events.db")):
            raise HTTPException(status_code=400, detail="No active runs found")

        is_human = False
        inst_text = ""
        if instruction_text and instruction_text not in ("Wake up and check GSA", "Wakeup"):
            is_human = True
            inst_text = instruction_text

        lock = run_lock_manager.get_lock()
        if lock.locked():
            if not is_human:
                return PlainTextResponse("Already busy", status_code=200)
            return PlainTextResponse("Conflict: Agent is busy", status_code=409)

        if is_human:
            from effects import HumanInstruction
            inst_effect = HumanInstruction(
                agent="operator",
                target_agent=role,
                instruction=inst_text,
                from_human="operator",
            )
            import hashlib
            otio_hash = "initial_hash"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(get_gsa_url())  # health probe
                    if resp.status_code == 200:
                        slots = resp.json().get("otio", {}).get("slots", {})
                        sorted_slots = sorted(slots.items())
                        otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
            except Exception:
                pass  # Fall back to default initial_hash on failure
            event_store.append(inst_effect, otio_hash)

        _agent_health["status"] = "busy"
        _agent_health["last_run"] = time.time()
        try:
            run_config = load_run_config()
            await execute_agent_turn(
                role=role,
                gsa_url=get_gsa_url(),
                notification_type="human" if is_human else "instruction",
                context={"instruction": inst_text} if is_human else None,
                config=run_config,
                extra_capabilities=app.extra_capabilities,
            )
            _agent_health["status"] = "healthy"
            _agent_health["idle_since"] = time.time()
        except Exception as exc:
            _agent_health["status"] = "error"
            _agent_health["last_error"] = str(exc)
            _agent_health["idle_since"] = time.time()
            raise HTTPException(status_code=500, detail=str(exc))

        resp_text = latest_monologues.get(role) or f"I am the {role} agent. Currently my status is healthy."
        accept = request.headers.get("accept", "")
        if "application/json" in accept:
            return AgentResponse(
                status="ok",
                effects_extracted=[],
                agent=role,
                timestamp=time.time(),
            )
        return PlainTextResponse(resp_text, media_type="text/plain")

    @app.put("/")
    async def put_handler(request: Request):
        body = await request.body()
        instruction_text = body.decode("utf-8").strip()

        if not os.path.exists(os.path.join(LOG_DIR, "events.db")):
            raise HTTPException(status_code=400, detail="No active runs found")

        # Cancel existing task if running
        global active_tasks
        existing_task = active_tasks.get(role)
        if existing_task and not existing_task.done():
            existing_task.cancel()
            try:
                await existing_task
            except asyncio.CancelledError:
                pass  # Task cancelled successfully

        is_human = False
        inst_text = ""
        if instruction_text and instruction_text != "Wake up and check GSA" and instruction_text != "Wakeup":
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
                            resp = await client.get(get_gsa_url())  # health probe
                            if resp.status_code == 200:
                                slots = resp.json().get("otio", {}).get("slots", {})
                                sorted_slots = sorted(slots.items())
                                otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
                    except Exception:
                        pass  # Fall back to default initial_hash on failure
                    event_store.append(inst_effect, otio_hash)

                run_config = load_run_config()
                await execute_agent_turn(
                    role=role,
                    gsa_url=get_gsa_url(),
                    notification_type="human" if is_human else "instruction",
                    context={"instruction": inst_text} if is_human else None,
                    config=run_config,
                    extra_capabilities=app.extra_capabilities,
                )
                _agent_health["status"] = "healthy"
                _agent_health["idle_since"] = time.time()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                _agent_health["status"] = "error"
                _agent_health["last_error"] = str(exc)
                _agent_health["idle_since"] = time.time()

        task = asyncio.create_task(run_turn_in_background())
        active_tasks[role] = task

        return PlainTextResponse("", status_code=204)

    @app.on_event("startup")
    async def start_autonomous_loop():
        assembly_triggered = False
        async def run_loop():
            nonlocal assembly_triggered
            if role == "provisioner":
                key = os.environ.get("VAST_AI_KEY") or os.environ.get("VAST_KEY")
                if not key:
                    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
                    if os.path.exists(vast_key_path):
                        try:
                            with open(vast_key_path) as f:
                                key = f.read().strip()
                        except Exception:
                            pass  # Ignore missing or unreadable vast key file
                if key:
                    try:
                        import subprocess
                        subprocess.run(f"vastai set api-key {key}", shell=True, capture_output=True)
                    except Exception:
                        pass  # Ignore login command execution failures

            # Sleep 0.5 seconds initially to allow GSA socket to open
            await _sleep(0.5)
            poll_interval = 0.5

            while True:
                try:
                    db_exists = os.path.exists(os.path.join(LOG_DIR, "events.db"))
                    if db_exists:
                        # Skip if run is already executing a turn for this agent
                        lock = run_lock_manager.get_lock()
                        if lock.locked():
                            await _sleep(poll_interval)
                            continue

                        gsa_url = get_gsa_url()
                        try:
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(gsa_url)  # health probe
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
                                    completed_tts_slots = {j.get("slot_id") for j in jobs if j.get("job_type") == "tts" and j.get("status") == "completed"}
                                    has_unqueued_slots = any(addr.startswith("A1") and s.get("status") == "scripted" and addr not in active_or_done_job_slots for addr, s in slots.items())
                                    has_completed_unmeasured = any(addr.startswith("A1") and addr in completed_tts_slots and s.get("status") == "scripted" for addr, s in slots.items())
                                    reconciled = state.get("jobs", {}).get("reconciliation_complete", False)
                                    has_measured = any(addr.startswith("A1") and s.get("status") == "measured" for addr, s in slots.items())
                                    if has_unqueued_slots or has_completed_unmeasured or (has_measured and not reconciled):
                                        should_act = True
                                elif role == "video":
                                    slots = state.get("otio", {}).get("slots", {})
                                    jobs = state.get("jobs", {}).get("jobs", {}).values()
                                    active_or_done_ltx = {j.get("slot_id") for j in jobs if j.get("job_type") == "ltx" and j.get("status") in ("pending", "running", "completed")}
                                    completed_ltx_slots = {j.get("slot_id") for j in jobs if j.get("job_type") == "ltx" and j.get("status") == "completed"}
                                    unqueued_video = any(
                                        addr.startswith("V1") and
                                        s.get("status") != "delivered" and
                                        slots.get(addr.replace("V1:", "A1:"), {}).get("status") == "measured" and
                                        addr not in active_or_done_ltx
                                        for addr, s in slots.items()
                                    )
                                    has_unmerged_completed_ltx = any(addr.startswith("V1") and addr in completed_ltx_slots and s.get("status") != "delivered" for addr, s in slots.items())
                                    if unqueued_video or has_unmerged_completed_ltx:
                                        should_act = True
                                elif role == "assembly":
                                    slots = state.get("otio", {}).get("slots", {})
                                    all_filled = len(slots) > 0 and all(s.get("status") == "delivered" or (addr.startswith("A1") and s.get("status") == "measured") for addr, s in slots.items())
                                    import sys
                                    print(f"DEBUG: assembly checking slots={ {k: v.get('status') for k, v in slots.items()} } all_filled={all_filled} current_phase={current_phase}", file=sys.stderr)
                                    if all_filled and current_phase != "done" and not assembly_triggered:
                                        should_act = True
                                        assembly_triggered = True
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
                                        run_config = load_run_config()
                                        await execute_agent_turn(
                                            role=role,
                                            gsa_url=gsa_url,
                                            notification_type="instruction",
                                            context={},
                                            config=run_config,
                                            extra_capabilities=app.extra_capabilities,
                                        )
                                        _agent_health["status"] = "healthy"
                                        _agent_health["idle_since"] = time.time()
                                    except Exception as exc:
                                        import traceback
                                        traceback.print_exc()
                                        _agent_health["status"] = "error"
                                        _agent_health["last_error"] = str(exc)
                                        _agent_health["idle_since"] = time.time()
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                await _sleep(poll_interval)

        asyncio.create_task(run_loop())

    return app
