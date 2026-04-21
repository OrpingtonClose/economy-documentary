"""`@tool`-decorated callables exposed to the DeepAgent orchestrator and
its SubAgents. One module per component (02, 04, 11, 12) plus the
shared ``task_pool`` primitive.
"""

from .assembly_tool import (
    ASSEMBLY_TOOL_NAME,
    DURATION_TOLERANCE_SEC,
    assemble_final_cut,
    reset_assembly_helpers,
    set_assembly_helpers,
)

__all__ = [
    "ASSEMBLY_TOOL_NAME",
    "DURATION_TOLERANCE_SEC",
    "assemble_final_cut",
    "reset_assembly_helpers",
    "set_assembly_helpers",
]
