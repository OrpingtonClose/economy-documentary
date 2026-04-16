"""Node recovery plugin -- graph-level precondition and postcondition enforcement.

Defense-in-depth layer that validates StageContract preconditions before a node
starts and logs postcondition status after it completes. The primary self-healing
happens inside each agent (via validate_deliverables tool), but this plugin
catches cases where the agent fails to self-heal.

If preconditions fail, the node is cancelled with a clear error message rather
than running with missing inputs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks.events import AfterNodeCallEvent, BeforeNodeCallEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)

# Maps graph node_id → contracts.py StageContract name
_NODE_CONTRACT_MAP: dict[str, str] = {
    "scenario": "SCENARIO_CONTRACT",
    "audio": "AUDIO_CONTRACT",
    "video": "PRODUCTION_CONTRACT",
    "assembly": "ASSEMBLY_CONTRACT",
    "refine": "SCENARIO_CONTRACT",
    # timing_eval has no contract — it's an evaluation node
}


def _get_contract(node_id: str) -> Any:
    """Look up the StageContract for a graph node."""
    contract_name = _NODE_CONTRACT_MAP.get(node_id)
    if not contract_name:
        return None

    try:
        from contracts import (
            ASSEMBLY_CONTRACT,
            AUDIO_CONTRACT,
            PRODUCTION_CONTRACT,
            SCENARIO_CONTRACT,
        )

        contracts = {
            "SCENARIO_CONTRACT": SCENARIO_CONTRACT,
            "AUDIO_CONTRACT": AUDIO_CONTRACT,
            "PRODUCTION_CONTRACT": PRODUCTION_CONTRACT,
            "ASSEMBLY_CONTRACT": ASSEMBLY_CONTRACT,
        }
        return contracts.get(contract_name)
    except ImportError:
        logger.debug("contracts module not available")
        return None


class NodeRecoveryPlugin(Plugin):
    """Graph-level precondition/postcondition enforcement.

    BeforeNodeCallEvent: validates preconditions, cancels node if inputs missing.
    AfterNodeCallEvent: validates postconditions, logs warnings for defense-in-depth.
    """

    name = "node_recovery"

    @hook
    def before_node_call(self, event: BeforeNodeCallEvent) -> None:
        """Validate preconditions before node starts.

        If preconditions fail, cancel the node with a clear error message
        rather than letting it run with missing inputs.
        """
        node_id = event.node_id
        contract = _get_contract(node_id)
        if contract is None:
            return

        state = event.invocation_state or {}

        # Check required_state keys
        from tools.validation_tools import _PLACEHOLDER_VALUES

        missing_state: list[str] = []
        for key in contract.required_state:
            val = state.get(key, "")
            val_str = str(val).strip() if val is not None else ""
            if val_str in _PLACEHOLDER_VALUES:
                missing_state.append(key)

        if missing_state:
            error_msg = (
                f"Node '{node_id}' precondition FAILED: required state keys "
                f"missing or placeholder: {missing_state}. The upstream node "
                f"did not produce these deliverables. Cannot proceed."
            )
            logger.error(
                "node_id=<%s>, missing_state=<%s> | precondition validation FAILED",
                node_id,
                missing_state,
            )
            event.cancel_node = error_msg

        # Check required services (non-blocking — log warnings only)
        try:
            from contracts import _check_service_health

            for svc in contract.required_services:
                err = _check_service_health(svc)
                if err:
                    if svc.required:
                        logger.error(
                            "node_id=<%s>, service=<%s> | required service unhealthy: %s",
                            node_id,
                            svc.name,
                            err,
                        )
                        event.cancel_node = (
                            f"Node '{node_id}' precondition FAILED: "
                            f"required service '{svc.name}' is unhealthy: {err}"
                        )
                        return
                    else:
                        logger.warning(
                            "node_id=<%s>, service=<%s> | optional service issue: %s",
                            node_id,
                            svc.name,
                            err,
                        )
        except ImportError:
            logger.debug("contracts module not available for service health checks")

        if not missing_state:
            logger.info(
                "node_id=<%s> | preconditions PASSED (%d state keys, %d services)",
                node_id,
                len(contract.required_state),
                len(contract.required_services),
            )

    @hook
    def after_node_call(self, event: AfterNodeCallEvent) -> None:
        """Log postcondition status after node completes.

        The primary postcondition enforcement happens inside the agent
        via validate_deliverables tool. This hook provides defense-in-depth
        logging so failures are visible even if the agent didn't self-heal.
        """
        node_id = event.node_id
        contract = _get_contract(node_id)
        if contract is None:
            return

        state = event.invocation_state or {}

        # Check produced_state
        from tools.validation_tools import _PLACEHOLDER_VALUES

        missing_produced: list[str] = []
        for key in contract.produced_state:
            val = state.get(key, "")
            val_str = str(val).strip() if val is not None else ""
            if val_str in _PLACEHOLDER_VALUES:
                missing_produced.append(key)

        if missing_produced:
            logger.error(
                "node_id=<%s>, missing_produced=<%s> | postcondition FAILED "
                "— agent did not produce required deliverables. "
                "This will cause downstream nodes to fail.",
                node_id,
                missing_produced,
            )
            # Store violation in state for downstream visibility
            violations = state.get("_postcondition_violations", [])
            violations.append({
                "node": node_id,
                "missing": missing_produced,
                "stage_contract": contract.name,
            })
            state["_postcondition_violations"] = violations
        else:
            logger.info(
                "node_id=<%s> | postconditions PASSED (%d produced_state keys)",
                node_id,
                len(contract.produced_state),
            )
