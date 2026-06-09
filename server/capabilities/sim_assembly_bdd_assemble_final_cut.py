import os
import subprocess
from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.capabilities.abstract import AbstractCapability
from event_store import EventStore
from effects import PipelineComplete

class AssembleFinalCutSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "assemble_final_cut":
            output_path = args.get("output_path", "/tmp/final_documentary.mp4")
            timeline_path = args.get("timeline_path", "timeline.otio")
            include_placeholders = args.get("include_placeholders", True)
            target_duration = args.get("target_duration", 7.0)

            from agent_base import get_active_log_dir, run_movie_assembly
            from projections import Timeline
            import opentimelineio as otio

            log_dir = get_active_log_dir()
            store = EventStore(log_dir=log_dir)

            # Rebuild the OTIO timeline from event store projections
            timeline_proj = Timeline()
            timeline_proj.tick(store)

            # Ensure the parent directory for timeline path exists
            if not os.path.isabs(timeline_path):
                timeline_dir = os.path.join(log_dir, "timelines")
                os.makedirs(timeline_dir, exist_ok=True)
                timeline_path = os.path.join(timeline_dir, os.path.basename(timeline_path))
            else:
                os.makedirs(os.path.dirname(timeline_path), exist_ok=True)

            otio.adapters.write_to_file(timeline_proj.timeline, timeline_path)

            # Run real assembly so we output a structurally valid MP4 with valid duration and audio track
            result_str = run_movie_assembly(
                output_path=output_path,
                timeline_path=timeline_path,
                include_placeholders=include_placeholders,
                target_duration=target_duration,
                event_store_instance=store,
                log_dir=log_dir
            )
            return result_str
        return await handler(args)
