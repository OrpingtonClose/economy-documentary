from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability

class VastShowSimulator(AbstractCapability):
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
            if "vastai show instances" in cmd or "vastai show instance" in cmd:
                # Returns status table indicating running instance
                return (
                    "ID       Status   IP          Port  GPU       VRAM  Hourly\n"
                    "1234567  running  127.0.0.1   8888  RTX 3090  24.0  0.45\n"
                )
        return await handler(args)
