from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability

class VastCreateSimulator(AbstractCapability):
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
            if "vastai create instance" in cmd:
                from effects import CommandExecuted, ProcessSpawned, log_trace_effect
                import hashlib
                agent = ctx.deps.agent_role
                log_trace_effect(ProcessSpawned(agent=agent, target="vastai", pid=12346))
                log_trace_effect(CommandExecuted(
                    agent=agent,
                    command=cmd,
                    exit_code=0,
                    stdout_hash=hashlib.sha256(b"simulated vastai create instance").hexdigest()
                ))
                # Returns mock instance started message
                return "Started. Instance ID: 1234567"
        return await handler(args)
