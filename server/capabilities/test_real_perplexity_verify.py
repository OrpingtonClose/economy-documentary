from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability

class PerplexityVerifySimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "perplexity_verify":
            claim = args.get("claim", "")
            return f"VERIFIED: The claim '{claim}' was checked and matches current data.\n\nSources:\n- https://example.com/mock_source"
        return await handler(args)
