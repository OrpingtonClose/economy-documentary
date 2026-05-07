"""
OTIO-based contract enforcer — timeline is ground truth.

A stage is not complete because the LLM says so. A stage is complete
because the OTIO timeline has the right clips in the right places.

The OTIO agent validates the timeline after each stage. If the clips
aren't there, the stage isn't done. The LLM can't fake it — the
timeline doesn't lie.

This replaces the state-key-based postcondition checks with OTIO
validation. Preconditions (upstream dependencies) still check state
keys because the data needs to flow between stages. But postconditions
(the "did you actually do the work?" check) validate the timeline.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookProvider,
    HookRegistry,
)

from contracts import StageContract, ContractViolation, validate_preconditions

logger = logging.getLogger(__name__)

# Map stage names to the OTIO validator phase
_STAGE_TO_OTIO_PHASE = {
    "scenario": "scenario",
    "audio": "audio",
    "visual_direction": "visual_direction",
    "production": "production",
    "assembly": "assembly",
}


class OTIOContractEnforcer(HookProvider):
    """Enforce contracts using OTIO timeline as ground truth.

    Preconditions: check state keys (upstream data must exist).
    Postconditions: validate the OTIO timeline (clips must be real).
    """

    def __init__(
        self,
        contract: StageContract,
        *,
        check_preconditions: bool = True,
        check_postconditions: bool = True,
    ) -> None:
        self.contract = contract
        self.check_preconditions = check_preconditions
        self.check_postconditions = check_postconditions

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        if self.check_preconditions:
            registry.add_callback(BeforeInvocationEvent, self._on_before)
        if self.check_postconditions:
            registry.add_callback(AfterInvocationEvent, self._on_after)

    def _extract_state(self, event):
        raw = event.agent.state
        try:
            dumped = raw.get() if hasattr(raw, "get") else raw
        except TypeError:
            dumped = {}
        if not isinstance(dumped, dict):
            dumped = {}
        return dumped

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        state = self._extract_state(event)
        logger.debug(
            "contract=<%s>, stage=<before> | validating preconditions",
            self.contract.name,
        )
        validate_preconditions(self.contract, state)

    def _on_after(self, event: AfterInvocationEvent) -> None:
        """Validate the OTIO timeline after the stage completes.

        This is the ground truth check. The LLM can claim it's done,
        but if the clips aren't in the timeline, the stage isn't done.
        """
        state = self._extract_state(event)
        stage_name = self.contract.name
        otio_phase = _STAGE_TO_OTIO_PHASE.get(stage_name)

        if not otio_phase:
            return  # No OTIO validation for this stage

        timeline_path = os.environ.get("_timeline_path", "")
        if not timeline_path or not os.path.exists(timeline_path):
            # No timeline — can't validate. Trust the agent.
            logger.warning(
                "contract=<%s> | no timeline to validate, skipping OTIO check",
                stage_name,
            )
            return

        try:
            from tools.otio_tools import validate_timeline

            class _ToolCtx:
                def __init__(self):
                    self.state = {
                        "_timeline_path": timeline_path,
                        "pipeline_phase": otio_phase,
                    }

            result = validate_timeline(otio_phase, tool_context=_ToolCtx())
            result_dict = json.loads(result) if isinstance(result, str) else result

            if not result_dict.get("valid", False):
                error_msg = (
                    f"OTIO VALIDATION FAILED for stage '{stage_name}': "
                    f"{result_dict.get('errors', result_dict.get('error', 'unknown'))}. "
                    f"The timeline does not have the required clips. "
                    f"The stage must actually generate and write content."
                )
                logger.error(error_msg)
                raise ContractViolation(
                    stage=stage_name,
                    message=error_msg,
                    details={"otio_validation": result_dict},
                )

            logger.info(
                "contract=<%s> | OTIO validation PASSED — timeline has real clips",
                stage_name,
            )
        except ContractViolation:
            raise
        except ImportError:
            logger.debug(
                "contract=<%s> | OTIO tools not available, skipping postcondition",
                stage_name,
            )
        except Exception as exc:
            logger.warning(
                "contract=<%s> | OTIO validation failed with exception: %s",
                stage_name,
                exc,
            )
