from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability

class VastDestroySimulator(AbstractCapability):
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
            if "vastai destroy instance" in cmd:
                return "Destroying instance 1234567... Destroyed."
        return await handler(args)
