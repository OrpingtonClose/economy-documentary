"""Approval gate hook -- interrupts the graph before key pipeline stages.

Replaces the file-based polling in server/callbacks/approval_gate.py.
Uses Strands BeforeNodeCallEvent to interrupt the graph, which transitions
to Status.INTERRUPTED and persists state via SessionManager. The frontend
sends an interrupt response to resume.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks.events import BeforeNodeCallEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)

_GATED_NODES = {"audio", "video", "assembly"}


class ApprovalGatePlugin(Plugin):
    """Interrupts the graph before audio, video, and assembly nodes for approval."""

    name = "approval_gate"

    def __init__(self, gated_nodes: set[str] | None = None) -> None:
        self._gated_nodes = gated_nodes or _GATED_NODES
        self._approved: set[str] = set()
        super().__init__()

    def approve(self, node_id: str) -> None:
        """Mark a node as approved so it can proceed.

        Args:
            node_id: The node identifier to approve.
        """
        self._approved.add(node_id)
        logger.info("node_id=<%s> | approval granted", node_id)

    def is_approved(self, node_id: str) -> bool:
        """Check if a node has been approved.

        Args:
            node_id: The node identifier to check.

        Returns:
            True if the node is approved.
        """
        return node_id in self._approved

    @hook
    def before_node_call(self, event: BeforeNodeCallEvent) -> None:
        """Interrupt before gated nodes if not yet approved."""
        node_id = event.node_id
        if node_id not in self._gated_nodes:
            return

        if self.is_approved(node_id):
            logger.debug("node_id=<%s> | already approved, proceeding", node_id)
            return

        logger.info(
            "node_id=<%s> | awaiting approval, interrupting graph", node_id
        )
        event.interrupt(
            name=f"approval_{node_id}",
            data={
                "stage": node_id,
                "message": f"Awaiting approval for {node_id} stage",
                "action_required": "approve_or_reject",
            },
        )
