"""Approval gates for the documentary pipeline — component 15.

Three gates: ``launch_visual_production`` (edit allowed),
``launch_assembly`` (respond-only), and ``request_human_approval``
(issued by the ``escalation`` SubAgent). DeepAgent's
``HumanInTheLoopMiddleware`` intercepts calls to these tools and raises
a LangGraph interrupt. The caller (see :mod:`server.strands_agents.run`)
resumes the graph with a structured ``Command(resume=...)`` carrying
the operator's decision.

The public surface here is:

* :func:`request_human_approval` — the ``@tool`` the escalation
  SubAgent calls. Intercepted by the middleware; its body is only
  reached in unit tests that bypass the middleware.
* :class:`ApprovalDecision` — typed payload for the 4 resume shapes.
* :class:`ApprovalRecord` — audit-trail line written to disk on every
  resume.
* :func:`write_approval_record` — persists the record under
  ``run_dir/approvals/resume_{interrupt_id}.json``.
* :func:`write_pending_envelope` — persists the interrupt payload
  under ``run_dir/approvals/pending_{interrupt_id}.json`` so the
  operator console (:mod:`server.api.approval`) can surface it.
* :func:`resume_command_from_decision` — turns a decision into the
  ``Command(resume=...)`` shape deepagents expects.
* :data:`INTERRUPT_GATE_CONFIG` — the ``interrupt_on`` mapping
  :func:`build_orchestrator` reads. Mirrors the spec table
  (``accept`` / ``edit`` / ``reject`` / ``respond``).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.tools import tool
from langgraph.types import Command

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate config
# ---------------------------------------------------------------------------


DecisionType = Literal["accept", "edit", "reject", "respond"]


INTERRUPT_GATE_CONFIG: dict[str, dict[str, Any]] = {
    "launch_visual_production": {
        "allowed_decisions": ["accept", "edit", "reject", "respond"],
    },
    "launch_assembly": {
        # No surgery on assembly args.
        "allowed_decisions": ["accept", "reject", "respond"],
    },
    "request_human_approval": {
        "allowed_decisions": ["accept", "respond"],
    },
}


# ---------------------------------------------------------------------------
# Decision payloads
# ---------------------------------------------------------------------------


class ApprovalDecision(TypedDict, total=False):
    """Operator decision envelope passed back to :class:`Command`.

    One of four shapes, all with ``type`` as the discriminator:

    * ``{"type": "accept"}`` — run tool with original args.
    * ``{"type": "edit", "args": {...}}`` — run tool with edited args.
    * ``{"type": "reject", "reason": str}`` — tool call raises and the
      agent sees the error in the transcript.
    * ``{"type": "respond", "content": str | dict}`` — free-form
      content is returned as the tool result.
    """

    type: DecisionType
    args: dict[str, Any]
    reason: str
    content: Any


@dataclass(frozen=True)
class ApprovalRecord:
    """Audit line written for every resume.

    The component 14 pipeline contract requires a resume record exists
    for every interrupt fired during a run before the run can be
    marked complete.
    """

    interrupt_id: str
    tool_name: str
    operator: str
    decision: ApprovalDecision
    at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for on-disk persistence."""

        return asdict(self)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _allowed_decisions(tool_name: str) -> set[DecisionType]:
    entry = INTERRUPT_GATE_CONFIG.get(tool_name, {})
    # Unknown tools get the full superset so validate_decision agrees
    # with _build_interrupt_on (pipeline.py); the operator console
    # never advertises an action the queue will 400 on.
    return set(entry.get("allowed_decisions", ["accept", "edit", "reject", "respond"]))


def validate_decision(
    tool_name: str,
    decision: ApprovalDecision,
) -> None:
    """Raise :class:`ValueError` if the decision is malformed.

    Args:
        tool_name: The intercepted tool name.
        decision: Operator decision dict.

    Raises:
        ValueError: If ``type`` is missing/unknown, if the decision
            type is not allowed for the gate, or if a required
            companion key is missing.
    """

    decision_type = decision.get("type")
    if decision_type not in ("accept", "edit", "reject", "respond"):
        raise ValueError(f"unknown decision type: {decision_type!r}")

    allowed = _allowed_decisions(tool_name)
    if decision_type not in allowed:
        raise ValueError(
            f"decision '{decision_type}' not allowed for gate "
            f"'{tool_name}'; allowed: {sorted(allowed)}",
        )

    if decision_type == "edit":
        args = decision.get("args")
        if not isinstance(args, dict):
            raise ValueError("edit decision requires dict 'args'")
    elif decision_type == "reject":
        reason = decision.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reject decision requires non-empty 'reason'")
    elif decision_type == "respond":
        if "content" not in decision:
            raise ValueError("respond decision requires 'content'")


def resume_command_from_decision(
    tool_name: str,
    decision: ApprovalDecision,
) -> Command:
    """Turn a validated decision into a LangGraph :class:`Command`.

    Emits the legacy single-decision shape
    (``Command(resume={"type": ...})``). Use
    :func:`langchain_resume_command_from_decision` instead when the
    interrupt was raised by ``langchain.agents.middleware.HumanInTheLoopMiddleware``,
    which expects ``Command(resume={"decisions": [...]})`` with one
    entry per parallel ``action_request``.

    Args:
        tool_name: The intercepted tool name.
        decision: Operator decision dict.

    Returns:
        A ``Command(resume={...})`` ready for ``agent.ainvoke(...)``.

    Raises:
        ValueError: If the decision fails :func:`validate_decision`.
    """

    validate_decision(tool_name, decision)
    return Command(resume=dict(decision))


def _to_langchain_decision(
    tool_name: str,
    decision: ApprovalDecision,
) -> dict[str, Any]:
    """Translate a project ``ApprovalDecision`` to the langchain HITL vocab.

    Mapping:

    * ``{"type": "accept"}`` → ``{"type": "approve"}``
    * ``{"type": "edit", "args": {...}}`` →
      ``{"type": "edit", "edited_action": {"name": tool_name, "args": {...}}}``
    * ``{"type": "reject", "reason": str}`` →
      ``{"type": "reject", "message": str}``
    * ``{"type": "respond", "content": ...}`` →
      ``{"type": "reject", "message": str(content)}`` (langchain HITL
      has no ``respond`` action; folding it into ``reject`` keeps the
      operator's free-form content surfaced as the tool error message
      so the agent transcript still records it)
    """

    decision_type = decision.get("type")
    if decision_type == "accept":
        return {"type": "approve"}
    if decision_type == "edit":
        args = decision.get("args") or {}
        return {
            "type": "edit",
            "edited_action": {"name": tool_name, "args": dict(args)},
        }
    if decision_type == "reject":
        reason = decision.get("reason") or ""
        return {"type": "reject", "message": str(reason)}
    if decision_type == "respond":
        content = decision.get("content")
        return {"type": "reject", "message": str(content)}
    raise ValueError(f"unknown decision type: {decision_type!r}")


def langchain_resume_command_from_decision(
    tool_name: str,
    decision: ApprovalDecision,
    *,
    action_count: int = 1,
) -> Command:
    """Build a langchain HITL middleware ``Command(resume=...)``.

    ``langchain.agents.middleware.HumanInTheLoopMiddleware`` groups
    every parallel tool call that needs review into a single
    ``HITLRequest`` carrying N ``action_requests``. The middleware
    then unwraps the resume payload as
    ``response["decisions"]`` and validates ``len(decisions) == N``.
    Single-decision shapes (``{"type": "approve"}``) raise
    ``KeyError: 'decisions'`` the moment the run resumes.

    This helper builds the correct shape: one langchain-vocab decision
    per ``action_request``, all replicated from the operator's single
    decision. The operator approves a *gate* (one user click), and the
    middleware then receives one approval per parallel call covered
    by that gate.

    Args:
        tool_name: The intercepted tool name. Used for ``edit`` to
            rebuild ``edited_action.name``.
        decision: Validated project ``ApprovalDecision``.
        action_count: Number of ``action_requests`` in the langchain
            ``HITLRequest``. Pass 1 for a non-batched gate
            (``request_human_approval``, ``launch_assembly``).

    Returns:
        A ``Command(resume={"decisions": [...]})`` with
        ``action_count`` entries.

    Raises:
        ValueError: If ``action_count < 1`` or the decision fails
            :func:`validate_decision`.
    """

    if action_count < 1:
        raise ValueError(
            f"action_count must be >= 1, got {action_count}",
        )

    validate_decision(tool_name, decision)
    langchain_decision = _to_langchain_decision(tool_name, decision)
    return Command(
        resume={
            "decisions": [
                dict(langchain_decision) for _ in range(action_count)
            ],
        },
    )


# ---------------------------------------------------------------------------
# On-disk audit trail
# ---------------------------------------------------------------------------


def _approvals_dir(run_dir: Path) -> Path:
    path = run_dir / "approvals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_pending_envelope(
    run_dir: Path,
    interrupt_id: str,
    tool_name: str,
    payload: dict[str, Any],
) -> Path:
    """Persist the pending interrupt so the operator console can read it.

    Args:
        run_dir: Run filesystem root.
        interrupt_id: LangGraph interrupt id.
        tool_name: The intercepted tool name.
        payload: Tool-call arguments + context the operator sees.

    Returns:
        The written file path
        (``run_dir/approvals/pending_{interrupt_id}.json``).
    """

    path = _approvals_dir(run_dir) / f"pending_{interrupt_id}.json"
    path.write_text(
        json.dumps(
            {
                "interrupt_id": interrupt_id,
                "tool_name": tool_name,
                "payload": payload,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            indent=2,
            sort_keys=True,
        ),
    )
    logger.info(
        "interrupt_id=<%s>, tool_name=<%s> | wrote pending envelope",
        interrupt_id,
        tool_name,
    )
    return path


def write_approval_record(run_dir: Path, record: ApprovalRecord) -> Path:
    """Persist the audit line for a resumed interrupt.

    Args:
        run_dir: Run filesystem root.
        record: The :class:`ApprovalRecord` to persist.

    Returns:
        The written file path
        (``run_dir/approvals/resume_{interrupt_id}.json``).
    """

    path = _approvals_dir(run_dir) / f"resume_{record.interrupt_id}.json"
    path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True),
    )
    # After a resume we clear the corresponding pending envelope so the
    # operator console does not surface it again on reconnect.
    pending = _approvals_dir(run_dir) / f"pending_{record.interrupt_id}.json"
    if pending.exists():
        pending.unlink()
    logger.info(
        "interrupt_id=<%s>, tool_name=<%s>, decision_type=<%s> | wrote resume record",
        record.interrupt_id,
        record.tool_name,
        record.decision.get("type"),
    )
    return path


def new_interrupt_id() -> str:
    """Generate a LangGraph-compatible interrupt id."""

    return f"int-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# The @tool itself
# ---------------------------------------------------------------------------


@tool
def request_human_approval(
    reason: str,
    summary: str,
    options: list[str] | None = None,
    context_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Pause the run for an operator decision.

    Args:
        reason: Short label the UI surfaces (e.g.
            ``"escalation:skip_scene_s3"``).
        summary: One-paragraph human-readable summary the operator
            sees.
        options: When provided, the UI renders them as clickable
            buttons. The operator response is then one of these
            strings.
        context_paths: Files the operator may want to inspect
            (timeline, failed frame, error log).

    Returns:
        In production the result is the operator's resume payload —
        ``HumanInTheLoopMiddleware`` intercepts this call before the
        body runs. Outside of a wired middleware (e.g. in unit tests)
        this returns a deterministic ``pending`` envelope so the
        call site can still assert on shape.
    """

    logger.info(
        "reason=<%s>, option_count=<%d>, context_path_count=<%d> | "
        "request_human_approval invoked outside middleware",
        reason,
        len(options or []),
        len(context_paths or []),
    )
    return {
        "status": "pending",
        "reason": reason,
        "summary": summary,
        "options": list(options or []),
        "context_paths": list(context_paths or []),
    }


__all__ = [
    "INTERRUPT_GATE_CONFIG",
    "ApprovalDecision",
    "ApprovalRecord",
    "DecisionType",
    "new_interrupt_id",
    "request_human_approval",
    "resume_command_from_decision",
    "validate_decision",
    "write_approval_record",
    "write_pending_envelope",
]
