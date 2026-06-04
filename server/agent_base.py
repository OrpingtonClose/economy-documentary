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

# Base path of DB files
LOG_DIR = "/tmp/documentary-pipeline"
event_store = EventStore(log_dir=LOG_DIR)
latest_monologues = {}
active_tasks: dict[str, asyncio.Task[Any]] = {}

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
    gsa_url: str = "http://127.0.0.1:8000"
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
    import signal
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
    try:
        stdout, stderr = await proc.communicate()
        return stdout.decode(errors="replace") + stderr.decode(errors="replace")
    except asyncio.CancelledError:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass  # process already dead
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
        skill_directories=[os.path.join(Path(__file__).resolve().parent.parent, "obsidian-vault", "prompts")],
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


def get_local_mem0():
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
                    "collection_name": "agent_memories",
                    "path": "/tmp/documentary-pipeline/mem0_qdrant",
                    "embedding_model_dims": 768
                }
            }
        }
        return Memory.from_config(config)
    except Exception as exc:
        logger.error(f"Failed to initialize local Mem0 instance: {exc}")
        return None


async def execute_agent_turn(
    role: str,
    gsa_url: str,
    notification_type: str = "instruction",
    context: dict[Any, Any] | None = None,
) -> list[Effect]:
    """Execute a single reasoning turn for the agent and append parsed effects."""
    lock = run_lock_manager.get_lock()
    async with lock:
        # 1. Fetch history and memory from GSA to bypass direct DB read
        gsa_state = {}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(gsa_url, timeout=1.0)  # health probe
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
        mem0_instance = get_local_mem0()
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
        model_instance = get_agent_model()
        agent = create_pipeline_agent(role, model_instance)

        # 4. Register tool
        @agent.tool
        async def run_bash(ctx, command: str) -> str:
            """Run an arbitrary bash command on the local machine."""
            return await bash_command(ctx, command)

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
                import sys
                import os
                import json
                import subprocess
                import opentimelineio as otio
                from effects import PipelineComplete

                def generate_audio_placeholder(dur: float, out_path: str) -> None:
                    print(f"CRITICAL: Generating silent audio placeholder of duration {dur}s at {out_path}", file=sys.stderr)
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono", "-t", str(dur), out_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                    )

                def generate_video_placeholder(dur: float, out_path: str) -> None:
                    print(f"CRITICAL: Generating black video placeholder of duration {dur}s at {out_path}", file=sys.stderr)
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={dur}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                    )

                def probe_duration(filepath: str) -> float:
                    try:
                        res = subprocess.run(
                            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                            capture_output=True, text=True, check=True
                        )
                        return float(res.stdout.strip())
                    except Exception:
                        return 0.0

                try:
                    # Load timeline
                    if not os.path.exists(timeline_path):
                        # Try to resolve relative to LOG_DIR/timelines
                        alt_path = os.path.join(LOG_DIR, "timelines", os.path.basename(timeline_path))
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
                        subprocess.run(
                            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", final_video_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
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
                        subprocess.run(
                            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", final_audio_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                        )
                    else:
                        generate_audio_placeholder(target_duration, final_audio_path)

                    # 3. Mux them together
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", final_video_path, "-i", final_audio_path, "-c:v", "copy", "-c:a", "aac", "-shortest", output_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
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

                    event_store.append(
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
        from effect_parser import parse_agent_text_multi
        effects = await parse_agent_text_multi(role, agent_text)

        # Compute hash from GSA otio slots
        import hashlib
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(gsa_url, timeout=1.0)  # health probe
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


class StrictEndpointMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/":
            return PlainTextResponse("Not Found: Only root '/' is permitted", status_code=404)
        if request.method not in ("GET", "POST", "PUT"):
            return PlainTextResponse("Method Not Allowed: Only GET, POST and PUT permitted", status_code=405)
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

    @app.get("/")
    async def health(request: Request):
        lock = run_lock_manager.get_lock()
        if lock.locked():
            _agent_health["status"] = "busy"
        else:
            _agent_health["status"] = "healthy"
        try:
            gsa_url = "http://127.0.0.1:8000/"
            async with httpx.AsyncClient() as client:
                resp = await client.get(gsa_url, timeout=1.0)  # health probe
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
                    resp = await client.get("http://127.0.0.1:8000/", timeout=1.0)  # health probe
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
            await execute_agent_turn(
                role=role,
                gsa_url="http://127.0.0.1:8000/",
                notification_type="human" if is_human else "instruction",
                context={"instruction": inst_text} if is_human else None,
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
                            resp = await client.get("http://127.0.0.1:8000/", timeout=1.0)  # health probe
                            if resp.status_code == 200:
                                slots = resp.json().get("otio", {}).get("slots", {})
                                sorted_slots = sorted(slots.items())
                                otio_hash = hashlib.sha256(json.dumps(sorted_slots, sort_keys=True).encode()).hexdigest()[:16]
                    except Exception:
                        pass  # Fall back to default initial_hash on failure
                    event_store.append(inst_effect, otio_hash)

                await execute_agent_turn(
                    role=role,
                    gsa_url="http://127.0.0.1:8000/",
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

        task = asyncio.create_task(run_turn_in_background())
        active_tasks[role] = task

        return PlainTextResponse("", status_code=204)

    @app.on_event("startup")
    async def start_autonomous_loop():
        async def run_loop():
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
                        subprocess.run(f"vastai login {key}", shell=True, capture_output=True)
                    except Exception:
                        pass  # Ignore login command execution failures

            # Wait a few seconds to allow GSA start up in integration tests
            await _sleep(2.0)

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
                            await _sleep(poll_interval)
                            continue

                        gsa_url = "http://127.0.0.1:8000/"
                        try:
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(gsa_url, timeout=1.0)  # health probe
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
                    pass  # Ignore errors in autonomous loop turn execution
                await _sleep(poll_interval)

        asyncio.create_task(run_loop())

    return app
