"""Contracts plugin -- pipeline state contract validation.

Wraps server/contracts.py validation as a Strands Plugin.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks.events import AfterInvocationEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)


class ContractsPlugin(Plugin):
    """Runs contract checks on pipeline state after each invocation."""

    name = "contracts"

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Run contract validation after invocation."""
        state = event.invocation_state
        if not state:
            return

        try:
            from contracts import validate_contracts

            violations = validate_contracts(state)
            if violations:
                logger.warning(
                    "violations=<%d> | contract validation failed",
                    len(violations),
                )
                existing = state.get("_contract_violations", [])
                state["_contract_violations"] = existing + violations
            else:
                logger.debug("contract validation passed")
        except ImportError:
            logger.debug("contracts module not available, skipping validation")
        except Exception:
            logger.exception("contract validation error")
