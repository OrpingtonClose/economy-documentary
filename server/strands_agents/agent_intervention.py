"""
Agent Intervention — GET/POST into running pipeline agents.

Restores the HTTP-addressable agent capability from the pre-migration
architecture (commit 3dac816). Each graph node exposes:

  GET  /agents/{node_id}     → read node state, last result, pending instructions
  POST /agents/{node_id}     → queue instruction for the node
  GET  /agents/current       → which node is currently executing
  POST /agents/resume/{interrupt_id} → respond to an active interrupt

Instructions are delivered via Strands' built-in interrupt mechanism.
When a node has pending instructions, the InterventionHook raises an
InterruptException before the node executes. The graph pauses, the HTTP
layer can observe the interrupt, and a POST to /agents/resume/{id}
provides the response that resumes execution.

State is file-backed so the HTTP server (FastAPI) and the pipeline
runner (CLI) can communicate even when they are separate processes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from strands.hooks import HookProvider, BeforeNodeCallEvent, AfterMultiAgentInvocationEvent
from strands.interrupt import Interrupt, InterruptException

logger = logging.getLogger(__name__)

_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")
_STATE_FILE = os.path.join(_OUTPUT_DIR, ".agent_intervention_state.json")

# How often the RecoveryShell polls for interrupt responses (seconds)
_POLL_INTERVAL = 1.0
# Maximum time to wait for an interrupt response before failing
_MAX_WAIT_SECONDS = 7200.0

# ---------------------------------------------------------------------------
# Pydantic models for HTTP API
# ---------------------------------------------------------------------------

class QueueInstructionRequest(BaseModel):
    instruction: str
    author: str = "human"

class ResumeInterruptRequest(BaseModel):
    response: str

# ---------------------------------------------------------------------------
# File-backed state store
# ---------------------------------------------------------------------------

class AgentInterventionStore:
    """File-backed store for cross-process agent intervention state.

    Both the FastAPI server and the pipeline runner read/write this file
    so they can coordinate without sharing memory.
    """

    _file: str = _STATE_FILE

    def _read(self) -> dict[str, Any]:
        if not os.path.exists(self._file):
            return {}
        try:
            with open(self._file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, state: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with open(self._file, "w") as f:
            json.dump(state, f, indent=2)

    # -- node execution tracking --

    def set_current_node(self, node_id: str | None) -> None:
        state = self._read()
        state["current_node"] = node_id
        state["updated_at"] = time.time()
        if node_id:
            state.setdefault("node_history", []).append({
                "node_id": node_id,
                "started_at": time.time(),
            })
        self._write(state)

    def get_current_node(self) -> str | None:
        return self._read().get("current_node")

    def get_node_history(self) -> list[dict[str, Any]]:
        return self._read().get("node_history", [])

    # -- instructions --

    def queue_instruction(self, node_id: str, instruction: str, author: str = "human") -> str:
        """Queue an instruction for a specific node. Returns the instruction id."""
        instruction_id = uuid.uuid4().hex[:12]
        state = self._read()
        state.setdefault("instructions", {}).setdefault(node_id, []).append({
            "id": instruction_id,
            "instruction": instruction,
            "author": author,
            "queued_at": time.time(),
            "status": "pending",
        })
        state["updated_at"] = time.time()
        self._write(state)
        logger.info("Instruction %s queued for node '%s' by %s", instruction_id, node_id, author)
        return instruction_id

    def get_pending_instructions(self, node_id: str) -> list[dict[str, Any]]:
        state = self._read()
        all_for_node = state.get("instructions", {}).get(node_id, [])
        return [i for i in all_for_node if i.get("status") == "pending"]

    def mark_instruction_used(self, node_id: str, instruction_id: str) -> None:
        state = self._read()
        for instr in state.get("instructions", {}).get(node_id, []):
            if instr.get("id") == instruction_id:
                instr["status"] = "used"
                instr["used_at"] = time.time()
                break
        self._write(state)

    # -- interrupts --

    def record_interrupt(self, node_id: str, interrupt_id: str, reason: str) -> None:
        state = self._read()
        state.setdefault("active_interrupts", []).append({
            "interrupt_id": interrupt_id,
            "node_id": node_id,
            "reason": reason,
            "created_at": time.time(),
            "status": "waiting",
        })
        state["updated_at"] = time.time()
        self._write(state)
        logger.info("Interrupt %s recorded for node '%s'", interrupt_id, node_id)

    def resolve_interrupt(self, interrupt_id: str, response: str) -> bool:
        state = self._read()
        for intr in state.get("active_interrupts", []):
            if intr.get("interrupt_id") == interrupt_id and intr.get("status") == "waiting":
                intr["status"] = "resolved"
                intr["response"] = response
                intr["resolved_at"] = time.time()
                self._write(state)
                logger.info("Interrupt %s resolved with response", interrupt_id)
                return True
        return False

    def get_interrupt_response(self, interrupt_id: str) -> str | None:
        state = self._read()
        for intr in state.get("active_interrupts", []):
            if intr.get("interrupt_id") == interrupt_id and intr.get("status") == "resolved":
                return intr.get("response")
        return None

    def get_active_interrupts(self) -> list[dict[str, Any]]:
        state = self._read()
        return [i for i in state.get("active_interrupts", []) if i.get("status") == "waiting"]

    def get_all_interrupts(self) -> list[dict[str, Any]]:
        return self._read().get("active_interrupts", [])

    # -- node results (populated by RecoveryShell) --

    def record_node_result(self, node_id: str, result_summary: dict[str, Any]) -> None:
        state = self._read()
        state.setdefault("node_results", {})[node_id] = {
            **result_summary,
            "recorded_at": time.time(),
        }
        state["updated_at"] = time.time()
        self._write(state)

    def get_node_result(self, node_id: str) -> dict[str, Any] | None:
        return self._read().get("node_results", {}).get(node_id)

    # -- full state dump --

    def get_state(self) -> dict[str, Any]:
        return self._read()

    def reset(self) -> None:
        self._write({})


# Global singleton store
_intervention_store = AgentInterventionStore()


def get_intervention_store() -> AgentInterventionStore:
    """Return the global intervention store."""
    return _intervention_store


# ---------------------------------------------------------------------------
# Hook provider — raises InterruptException when instructions are pending
# ---------------------------------------------------------------------------

class InterventionHook(HookProvider):
    """Hook that pauses nodes when they have pending instructions.

    Register this hook with the graph to enable external intervention:

        graph = Graph(..., hooks=[InterventionHook(), ...])

    When a node is about to execute, the hook checks the intervention
    store for pending instructions targeting that node. If found, it
    raises an InterruptException with the instruction, causing the
    graph to pause. The RecoveryShell then waits for a response via
    the HTTP layer and resumes execution.
    """

    def __init__(self, store: AgentInterventionStore | None = None) -> None:
        self.store = store or _intervention_store

    def register_hooks(self, registry: Any, **_: Any) -> None:
        from strands.hooks import HookRegistry
        registry = registry if isinstance(registry, HookRegistry) else registry
        registry.add_callback(BeforeNodeCallEvent, self._on_before_node)
        registry.add_callback(AfterMultiAgentInvocationEvent, self._on_after_invocation)

    def _on_before_node(self, event: BeforeNodeCallEvent) -> None:
        node_id = event.node_id
        self.store.set_current_node(node_id)

        pending = self.store.get_pending_instructions(node_id)
        if pending:
            # Take the oldest pending instruction
            instr = pending[0]
            self.store.mark_instruction_used(node_id, instr["id"])

            interrupt_id = f"intervene-{instr['id']}"
            logger.info(
                "Node '%s' interrupted for instruction %s: %s",
                node_id, instr["id"], instr["instruction"][:80],
            )
            self.store.record_interrupt(node_id, interrupt_id, instr["instruction"])

            raise InterruptException(
                Interrupt(
                    id=interrupt_id,
                    name="agent_instruction",
                    reason=instr["instruction"],
                )
            )

    def _on_after_invocation(self, event: AfterMultiAgentInvocationEvent) -> None:
        self.store.set_current_node(None)


# ---------------------------------------------------------------------------
# RecoveryShell helper — wait for interrupt responses
# ---------------------------------------------------------------------------

async def wait_for_interrupt_response(
    interrupt_id: str,
    store: AgentInterventionStore | None = None,
    poll_interval: float = _POLL_INTERVAL,
    max_wait: float = _MAX_WAIT_SECONDS,
) -> str | None:
    """Poll the store until an interrupt response appears or timeout.

    Returns the response string, or None on timeout.
    """
    store = store or _intervention_store
    start = time.time()
    while time.time() - start < max_wait:
        response = store.get_interrupt_response(interrupt_id)
        if response is not None:
            return response
        await asyncio.sleep(poll_interval)
    logger.warning("Timeout waiting for interrupt response %s", interrupt_id)
    return None


# ---------------------------------------------------------------------------
# FastAPI router — GET / POST endpoints
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def list_agents() -> dict[str, Any]:
    """List all nodes and their known state."""
    store = _intervention_store
    state = store.get_state()
    return {
        "current_node": store.get_current_node(),
        "node_history": store.get_node_history(),
        "node_results": state.get("node_results", {}),
        "active_interrupts": store.get_active_interrupts(),
        "pending_instructions": state.get("instructions", {}),
    }


@router.get("/current")
async def get_current_agent() -> dict[str, Any]:
    """Return the currently executing node, if any."""
    store = _intervention_store
    node_id = store.get_current_node()
    if not node_id:
        return {"current_node": None, "status": "idle"}
    result = store.get_node_result(node_id)
    pending = store.get_pending_instructions(node_id)
    return {
        "current_node": node_id,
        "node_result": result,
        "pending_instructions_count": len(pending),
        "status": "executing" if result is None else "completed",
    }


@router.get("/{node_id}")
async def get_agent(node_id: str) -> dict[str, Any]:
    """Return state for a specific node."""
    store = _intervention_store
    result = store.get_node_result(node_id)
    pending = store.get_pending_instructions(node_id)
    history = [h for h in store.get_node_history() if h.get("node_id") == node_id]
    active = [i for i in store.get_active_interrupts() if i.get("node_id") == node_id]
    return {
        "node_id": node_id,
        "node_result": result,
        "pending_instructions": pending,
        "execution_history": history,
        "active_interrupts": active,
    }


@router.post("/{node_id}")
async def post_agent(node_id: str, req: QueueInstructionRequest) -> dict[str, Any]:
    """Queue an instruction for a specific node.

    If the node is currently executing, the instruction will cause an
    interrupt on the next node boundary. If the node is idle, the
    instruction will be waiting when it next starts.
    """
    store = _intervention_store
    instruction_id = store.queue_instruction(node_id, req.instruction, req.author)
    return {
        "instruction_id": instruction_id,
        "node_id": node_id,
        "instruction": req.instruction,
        "status": "queued",
    }


@router.post("/resume/{interrupt_id}")
async def resume_interrupt(interrupt_id: str, req: ResumeInterruptRequest) -> dict[str, Any]:
    """Provide a response to an active interrupt.

    The RecoveryShell polls for this response and resumes the graph
    with it, allowing the interrupted node to continue with the new
    information.
    """
    store = _intervention_store
    ok = store.resolve_interrupt(interrupt_id, req.response)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Interrupt '{interrupt_id}' not found or already resolved")
    return {
        "interrupt_id": interrupt_id,
        "status": "resolved",
        "response": req.response,
    }
