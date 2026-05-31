"""Maintainer notification — enshrined error-handling pattern.

Whenever any unit catches an exception, instead of:

    except Exception as exc:
        logger.warning("...")          # SWALLOWS — agent never sees it
        pass

Do this:

    from maintainer import notify_maintainer

    except Exception as exc:
        decision = notify_maintainer(
            operation="tts_generation",
            error=str(exc),
            context={"scene_id": 3, "worker_url": url},
        )
        if decision["action"] == "retry":
            # ... retry logic
        elif decision["action"] == "destroy_and_reprovision":
            # ... destroy VM, provision new
        elif decision["action"] == "abort":
            raise RuntimeError(exc)

The maintainer agent is the one that decides.  The code does not decide.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def notify_maintainer(
    operation: str,
    error: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Notify the maintainer agent that an error occurred.

    This is the single entry point for all error reporting in the pipeline.
    Every ``except`` block should call this instead of logging and moving on.

    Args:
        operation: What was being attempted (e.g. "video_render", "vm_provisioning").
        error: The exception message or error text.
        context: Any structured data that helps the agent decide (urls, ids, params).

    Returns:
        Decision dict.  Keys:
        - ``action``: ``retry``, ``destroy_and_reprovision``, ``skip``, ``abort``, ``wait``
        - ``reason``: Human-readable why the agent chose this action
        - ``payload``: Optional extra data for the caller
    """
    ctx = context or {}

    # Always log — the maintainer agent may be offline, logs are the SSOT
    logger.error(
        "MAINTAINER: operation=%s error=%s context=%s",
        operation, error, json.dumps(ctx, default=str),
    )

    # If recovery middleware is available, use the full agent ladder
    try:
        from recovery import escalate_pipeline_error
        decision = escalate_pipeline_error(
            operation_name=operation,
            error_msg=error,
            severity="critical",
            diagnosis_hint=f"Maintainer notification for {operation}",
            pipeline_state=ctx,
            diagnostic_data=ctx,
        )
        return {
            "action": decision.get("action", "abort"),
            "reason": decision.get("reason", "escalation returned no reason"),
            "payload": decision,
        }
    except Exception as exc:
        logger.error("MAINTAINER: escalation system failed: %s", exc)

    # Fallback: if no recovery system, the code must raise — never swallow
    return {
        "action": "abort",
        "reason": f"escalation unavailable for {operation}: {error}",
        "payload": {},
    }
