import json
import httpx
from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability

class WorkerHealthSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "web_fetch":
            url = args.get("url", "")
            if "127.0.0.1" in url or "localhost" in url or "vm_instance" in url:
                model_loaded = "Qwen3-TTS"
                worker_type = "tts"
                try:
                    from agent_base import get_gsa_url
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(get_gsa_url())
                        if resp.status_code == 200:
                            state_data = resp.json()
                            jobs = state_data.get("jobs", {}).get("jobs", {}).values()
                            # If there is a pending ltx job, this is the video worker
                            pending_ltx = any(j.get("job_type") == "ltx" and j.get("status") in ("pending", "running") for j in jobs)
                            if pending_ltx:
                                model_loaded = "LTX-2.3"
                                worker_type = "ltx"
                except Exception:
                    pass

                return json.dumps({
                    "ready": True,
                    "worker_type": worker_type,
                    "gpu_name": "RTX 3090",
                    "vram_used_gb": 12.5,
                    "vram_total_gb": 24.0,
                    "model_loaded": model_loaded,
                    "jobs_in_queue": 0,
                    "uptime_seconds": 120.0
                })
        return await handler(args)
