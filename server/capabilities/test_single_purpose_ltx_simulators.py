import os
import subprocess
from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability
from event_store import EventStore
from effects import JobStarted, JobCompleted, VMAllocated
from agent_base import get_active_log_dir
from projections import VMs

def _simulate_ltx_job(job_id: str, duration: float) -> str:
    log_dir = get_active_log_dir()
    store = EventStore(log_dir=log_dir)
    
    video_dir = os.path.join(log_dir, "video_outputs")
    os.makedirs(video_dir, exist_ok=True)
    out_path = f"{video_dir}/{job_id}.mp4"
    
    # Generate a real valid MP4 file with the resolved duration
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    
    # Check if VM is allocated
    vms_proj = VMs()
    vms_proj.tick(store)
    if "1234567" not in vms_proj.vms or vms_proj.vms["1234567"].status != "active":
        store.append(VMAllocated(
            agent="provisioner",
            instance_id="1234567",
            role="ltx",
            offer_id="1001",
            worker_url="http://127.0.0.1:8888",
            gpu_type="RTX A6000",
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

class LtxScaleSimulator(AbstractCapability):
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
            if "curl" in cmd and "job_id=job_ltx_scale_1" in cmd:
                return _simulate_ltx_job("job_ltx_scale_1", 3.0)
        return await handler(args)

class LtxSingleSimulator(AbstractCapability):
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
            if "curl" in cmd and "job_id=job_ltx_single_1" in cmd:
                return _simulate_ltx_job("job_ltx_single_1", 3.0)
        return await handler(args)
