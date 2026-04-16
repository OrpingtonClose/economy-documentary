"""Timeline guardian plugin -- OTIO timeline validation after each invocation.

Replaces server/callbacks/timeline_guardian.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks.events import AfterInvocationEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)


class TimelineGuardianPlugin(Plugin):
    """Validates OTIO timeline structure after each agent invocation."""

    name = "timeline_guardian"

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Validate timeline for gaps, overlaps, and drift after invocation."""
        state = event.invocation_state
        timeline_path = state.get("_timeline_path", "")
        if not timeline_path:
            return

        try:
            from callbacks.timeline_guardian import _VALIDATORS, _load_timeline

            pipeline_phase = state.get("_current_phase", "")
            timeline = _load_timeline(state)
            if not timeline:
                logger.warning("timeline_path=<%s> | timeline not found for validation", timeline_path)
                return

            if pipeline_phase and pipeline_phase in _VALIDATORS:
                validator = _VALIDATORS[pipeline_phase]
                errors = validator(timeline, state)
                if errors:
                    error_msg = f"OTIO VIOLATION [{pipeline_phase}]: {errors}"
                    logger.error(
                        "phase=<%s>, errors=<%d> | timeline validation FAILED",
                        pipeline_phase,
                        len(errors) if isinstance(errors, list) else 1,
                    )
                    state["otio_violation"] = error_msg
                    raise RuntimeError(error_msg)
                else:
                    logger.debug(
                        "phase=<%s> | timeline validation passed", pipeline_phase
                    )
        except ImportError:
            logger.debug("timeline guardian validators not available, skipping")
        except Exception:
            logger.exception("timeline guardian validation error")
