from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability

class VastSearchSimulator(AbstractCapability):
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
            if "vastai search offers" in cmd:
                return (
                    "ID      CUDA   GPU_name       Num_GPUs  VRAM   Inet_up  Inet_down  Reliability  Price\n"
                    "1001    12.0   RTX 3090       1         24.0   100.0    100.0      0.99         0.45\n"
                    "1002    12.0   RTX 4090       1         24.0   150.0    150.0      0.99         0.85\n"
                )
        return await handler(args)
