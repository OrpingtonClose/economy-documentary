import os
import subprocess
from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability
from event_store import EventStore
from effects import JobStarted, JobCompleted, VMAllocated, run_subprocess_logged
from agent_base import get_active_log_dir
from projections import VMs

def _simulate_tts_job(job_id: str, duration: float) -> str:
    log_dir = get_active_log_dir()
    store = EventStore(log_dir=log_dir)
    
    audio_dir = os.path.join(log_dir, "audio_outputs")
    os.makedirs(audio_dir, exist_ok=True)
    out_path = f"{audio_dir}/{job_id}.wav"
    
    # Calculate a unique frequency based on the job_id hash to ensure LUFS/pitch variation
    import hashlib
    h_val = int(hashlib.sha256(job_id.encode()).hexdigest(), 16)
    frequency = 200 + (h_val % 300)
    
    # Generate a real valid WAV file to test actual FFmpeg concatenation and normalization
    run_subprocess_logged(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100", "-t", str(duration), out_path],
        agent="provisioner", check=True
    )
    
    # Check if VM is allocated
    vms_proj = VMs()
    vms_proj.tick(store)
    if "1234567" not in vms_proj.vms or vms_proj.vms["1234567"].status != "active":
        store.append(VMAllocated(
            agent="provisioner",
            instance_id="1234567",
            role="tts",
            offer_id="1001",
            worker_url="http://127.0.0.1:8888",
            gpu_type="RTX 4090",
            cost_per_hour=0.40
        ), "initial_hash")
        
    store.append(JobStarted(agent="provisioner", job_id=job_id, vm_instance_id="1234567"), "initial_hash")
    store.append(JobCompleted(
        agent="provisioner",
        job_id=job_id,
        artifact_uri=out_path,
        duration_sec=duration,
        vm_instance_id="1234567"
    ), "initial_hash")
    
    return f'{{"status": "success", "job_id": "{job_id}", "artifact_uri": "{out_path}"}}'

class TtsJob1Simulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd and "job_id=job_tts_1" in cmd:
                return _simulate_tts_job("job_tts_1", 3.0)
        return await handler(args)

class TtsColdStartSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd and "job_id=job_tts_cold_1" in cmd:
                return _simulate_tts_job("job_tts_cold_1", 4.0)
        return await handler(args)

class TtsSingleBlockSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd and "job_id=job_tts_single_1" in cmd:
                return _simulate_tts_job("job_tts_single_1", 3.5)
        return await handler(args)

class TtsMultiBlockSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd:
                if "job_id=job_tts_multi_1" in cmd:
                    return _simulate_tts_job("job_tts_multi_1", 3.0)
                elif "job_id=job_tts_multi_2" in cmd:
                    return _simulate_tts_job("job_tts_multi_2", 4.5)
                elif "job_id=job_tts_multi_3" in cmd:
                    return _simulate_tts_job("job_tts_multi_3", 4.0)
        return await handler(args)

class TtsFailSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd:
                if "job_id=job_tts_fail_1" in cmd:
                    return _simulate_tts_job("job_tts_fail_1", 3.0)
                elif "job_id=job_tts_fail" in cmd:
                    return _simulate_tts_job("job_tts_fail", 3.0)
        return await handler(args)

class TtsPreemptSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd and "job_id=job_tts_preempt_1" in cmd:
                return _simulate_tts_job("job_tts_preempt_1", 3.0)
        return await handler(args)
