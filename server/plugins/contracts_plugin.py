"""Contracts plugin -- stage-specific pipeline state contract validation.

Uses proper StageContract definitions from contracts.py to validate
postconditions after each agent invocation. Determines the current
pipeline stage from invocation_state["pipeline_phase"] and runs the
appropriate contract's validate_postconditions.

This is a defense-in-depth layer — the primary postcondition enforcement
happens inside each agent via validate_deliverables tool.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks.events import AfterInvocationEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)

# Maps pipeline_phase values → contracts.py contract names
_PHASE_CONTRACT_MAP: dict[str, str] = {
    "scenario": "SCENARIO_CONTRACT",
    "audio": "AUDIO_CONTRACT",
    "visual_direction": "VISUAL_DIRECTION_CONTRACT",
    "production": "PRODUCTION_CONTRACT",
    "assembly": "ASSEMBLY_CONTRACT",
}


class ContractsPlugin(Plugin):
    """Runs stage-specific contract validation after each agent invocation."""

    name = "contracts"

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Run postcondition validation using the proper StageContract."""
        state = event.invocation_state
        if not state:
            return

        pipeline_phase = state.get("pipeline_phase", "")
        contract_name = _PHASE_CONTRACT_MAP.get(pipeline_phase, "")

        if not contract_name:
            logger.debug(
                "pipeline_phase=<%s> | no contract mapped, skipping validation",
                pipeline_phase,
            )
            return

        try:
            from contracts import (
                ASSEMBLY_CONTRACT,
                AUDIO_CONTRACT,
                PRODUCTION_CONTRACT,
                SCENARIO_CONTRACT,
                VISUAL_DIRECTION_CONTRACT,
                validate_postconditions,
            )

            contracts: dict[str, Any] = {
                "SCENARIO_CONTRACT": SCENARIO_CONTRACT,
                "AUDIO_CONTRACT": AUDIO_CONTRACT,
                "VISUAL_DIRECTION_CONTRACT": VISUAL_DIRECTION_CONTRACT,
                "PRODUCTION_CONTRACT": PRODUCTION_CONTRACT,
                "ASSEMBLY_CONTRACT": ASSEMBLY_CONTRACT,
            }

            contract = contracts.get(contract_name)
            if contract is None:
                return

            violations = validate_postconditions(contract, state)
            if violations:
                logger.warning(
                    "pipeline_phase=<%s>, contract=<%s>, violations=<%d> | "
                    "postcondition validation failed",
                    pipeline_phase,
                    contract_name,
                    len(violations) if isinstance(violations, list) else 1,
                )
                existing = state.get("_contract_violations", [])
                if isinstance(violations, list):
                    state["_contract_violations"] = existing + violations
                else:
                    state["_contract_violations"] = existing + [str(violations)]
            else:
                logger.info(
                    "pipeline_phase=<%s>, contract=<%s> | postcondition validation passed",
                    pipeline_phase,
                    contract_name,
                )

        except ImportError:
            logger.debug("contracts module not available, skipping validation")
        except Exception:
            logger.exception(
                "pipeline_phase=<%s> | contract validation error",
                pipeline_phase,
            )
