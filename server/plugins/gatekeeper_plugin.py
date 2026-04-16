"""Gatekeeper plugin -- scenario validation after each invocation.

Wraps server/gatekeeper.py validation as a Strands Plugin.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks.events import AfterInvocationEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)


class GatekeeperPlugin(Plugin):
    """Runs gatekeeper checks after each invocation and emits warnings/rejections."""

    name = "gatekeeper"

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Run gatekeeper validation on pipeline state."""
        state = event.invocation_state
        scenes = state.get("scenes", [])
        if not scenes:
            return

        try:
            from gatekeeper import check_scenario

            results = check_scenario(scenes)
            warnings = []
            rejections = []
            for r in results:
                if r.verdict.value == "reject":
                    rejections.append({"name": r.name, "message": r.message})
                elif r.verdict.value == "warn":
                    warnings.append({"name": r.name, "message": r.message})

            if rejections:
                logger.warning(
                    "rejections=<%d>, warnings=<%d> | gatekeeper found issues",
                    len(rejections),
                    len(warnings),
                )
                state["_gatekeeper_rejections"] = rejections
            if warnings:
                state["_gatekeeper_warnings"] = warnings

            if not rejections and not warnings:
                logger.debug("gatekeeper validation passed")
        except ImportError:
            logger.debug("gatekeeper module not available, skipping validation")
        except Exception:
            logger.exception("gatekeeper validation error")
