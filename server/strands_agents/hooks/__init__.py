"""HookProvider classes for strands_agents.

Each component adds one or more hooks here. Scaffolded under
``docs/strands-migration/AGENTS.md`` §hooks and expanded as components
land.
"""

from __future__ import annotations

from .recovery_logger import RecoveryLogger

__all__ = ["RecoveryLogger"]
