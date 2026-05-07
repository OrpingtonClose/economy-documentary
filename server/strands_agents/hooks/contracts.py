"""ContractEnforcer — validate :class:`StageContract` invariants around runs.

The ADK pipeline uses ``before_agent_callback`` / ``after_agent_callback``
to run ``validate_preconditions`` / ``validate_postconditions`` against a
stage's :class:`StageContract` (see
``server/agents/pipeline.py``). The Strands equivalent is a
:class:`HookProvider` that subscribes to the same lifecycle events.

The hook reads agent state via ``event.agent.state`` and raises
:class:`ContractViolation` when a required key is missing or a produced
key was not written. Downstream orchestrators catch this and decide
whether to escalate, retry, or abort.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent, HookProvider, HookRegistry

from contracts import (
    StageContract,
    validate_postconditions,
    validate_preconditions,
)

logger = logging.getLogger(__name__)


class ContractEnforcer(HookProvider):
    """Validate :class:`StageContract` pre/post conditions on agent runs.

    Attributes:
        contract: The stage contract whose invariants to enforce.
        check_preconditions: Validate ``required_state`` + services before
            the agent starts. Defaults to True.
        check_postconditions: Validate ``produced_state`` + artifacts
            after the agent finishes. Defaults to True.
        state_key: Optional key under which the Strands ``agent.state``
            stores the pipeline blackboard. When ``None`` the entire
            ``agent.state`` dump is passed as the state argument.
    """

    def __init__(
        self,
        contract: StageContract,
        *,
        check_preconditions: bool = True,
        check_postconditions: bool = True,
        state_key: str | None = None,
    ) -> None:
        self.contract = contract
        self.check_preconditions = check_preconditions
        self.check_postconditions = check_postconditions
        self.state_key = state_key

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        """Wire the before/after invocation callbacks."""
        if self.check_preconditions:
            registry.add_callback(BeforeInvocationEvent, self._on_before)
        if self.check_postconditions:
            registry.add_callback(AfterInvocationEvent, self._on_after)

    def _extract_state(self, event: BeforeInvocationEvent | AfterInvocationEvent) -> dict[str, Any]:
        """Extract state for contract validation.

        Checks OTIO timeline metadata first (the ground truth), then
        falls back to agent.state for ephemeral runtime data. Pipeline
        data (scenes, alignment, visual_concepts) lives in OTIO.
        """
        # Check OTIO first — the ground truth
        otio_data = {}
        try:
            from strands_agents.otio_manager import OTIOStateManager
            # Try to get the OTIO manager from the agent's state
            mgr = getattr(event.agent, '_otio_manager', None)
            if mgr is None:
                # Try to find it on the agent's tools or state
                raw = event.agent.state
                try:
                    dumped = raw.get() if hasattr(raw, "get") else raw
                    if isinstance(dumped, dict):
                        mgr = dumped.get("_otio_manager")
                except (TypeError, AttributeError):
                    pass
            if isinstance(mgr, OTIOStateManager):
                for key in ("scenes", "whisperx_alignment", "visual_concepts",
                           "visual_style", "style_lock", "content_analysis"):
                    val = mgr.get_pipeline_metadata(key)
                    if val is not None:
                        otio_data[key] = val
        except Exception:
            pass

        # Then agent.state for ephemeral runtime data
        raw = event.agent.state
        try:
            dumped = raw.get() if hasattr(raw, "get") else raw
        except TypeError:
            dumped = {}
        if not isinstance(dumped, dict):
            dumped = {}
        if self.state_key is None:
            agent_data = dumped
        else:
            nested = dumped.get(self.state_key) or {}
            agent_data = nested if isinstance(nested, dict) else {}

        # OTIO data takes precedence over agent state
        merged = {**agent_data, **otio_data}
        return merged

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        state = self._extract_state(event)
        logger.debug(
            "contract=<%s>, stage=<before> | validating preconditions",
            self.contract.name,
        )
        validate_preconditions(self.contract, state)

    def _on_after(self, event: AfterInvocationEvent) -> None:
        state = self._extract_state(event)
        logger.debug(
            "contract=<%s>, stage=<after> | validating postconditions",
            self.contract.name,
        )
        validate_postconditions(self.contract, state)
