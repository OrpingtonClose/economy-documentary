"""
Strands pipeline entry point — replaces server/run_pipeline.py for Strands mode.

This is what ``PIPELINE_BACKEND=strands`` activates. It:
1. Builds the 5-node Graph with real stage agents
2. Attaches the OTIO state manager
3. Attaches the pipeline hooks
4. Runs the Graph via RecoveryShell
5. Streams events to the AG-UI bus via SSEBridge

Usage::

    python -m strands_agents.run_strands "Make a 3 minute documentary about deep brain stimulation"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from strands.models.anthropic import AnthropicModel

from strands_agents.graph_pipeline import build_documentary_graph, RecoveryShell
from strands_agents.otio_manager import OTIOStateManager
from strands_agents.stages.preflight import run_preflight, PreflightError
from strands_agents.hooks.pipeline_hooks import (
    BudgetHook,
    CheckpointHook,
    QANodeHook,
    ApprovalGateHook,
    ImmutabilityHook,
    ShellGuardHook,
    StageContractHook,
    ScopeHook,
)
from strands_agents.sse_bridge import SSEBridge

logger = logging.getLogger(__name__)

# Default model
_MODEL_ID = os.environ.get("STRANDS_MODEL", "claude-sonnet-4-20250514")


async def run_documentary(
    brief: str,
    *,
    output_dir: str = "/tmp/documentary-pipeline",
    model_id: str | None = None,
    budget_usd: float = 100.0,
    max_node_executions: int = 50,
    max_retries: int = 3,
    approval_mode: str = "auto_approve",
    gpu_protocol: Any | None = None,
) -> dict[str, Any]:
    """Run the full documentary pipeline using Strands.

    Args:
        brief: The user's brief (e.g. "3 minute documentary about DBS").
        output_dir: Pipeline output directory.
        model_id: Strands model ID (default: claude-sonnet-4-20250514).
        budget_usd: Cost budget for the pipeline.
        max_node_executions: Safety limit on node re-executions.
        max_retries: Recovery retries for the RecoveryShell.
        approval_mode: "auto_approve" (CI) or "manual" (dashboard).
        gpu_protocol: Must be provided by the provisioner — no fallback exists.

    Returns:
        Dict with pipeline result, OTIO state, and execution summary.
    """
    # Set up model
    model = AnthropicModel(model_id=model_id or _MODEL_ID, max_tokens=8192)

    # Preflight — check all resources before doing any work
    report = run_preflight(output_dir=output_dir)
    if not report.passed:
        logger.error("Preflight failed — pipeline cannot start:")
        for f in report.failures:
            logger.error("  %s: %s", f.name, f.message)
        raise PreflightError(report)
    logger.info("Preflight: all checks passed")

    # Set up OTIO state manager
    otio_manager = OTIOStateManager(output_dir=output_dir)
    otio_manager.create_timeline("documentary_draft")

    # Set the timeline path in the environment so tools can find it
    if hasattr(otio_manager, "_timeline_path") and otio_manager._timeline_path:
        os.environ["_timeline_path"] = otio_manager._timeline_path

    # Provisioning is NOT done at startup. The provisioner receives the
    # task list from the pipeline stages (audio tells the provisioner
    # what TTS jobs it needs, production tells it what video jobs it
    # needs). The provisioner optimizes once it sees the full picture.
    # Worker URLs are set as env vars by the provisioner when workers
    # are ready.

    # Build hooks
    approval_stages = set() if approval_mode == "auto_approve" else {
        "scenario", "audio", "visual", "production", "assembly"
    }
    hooks = [
        StageContractHook(),
        ImmutabilityHook(),
        BudgetHook(budget_usd=budget_usd),
        ApprovalGateHook(gated_stages=approval_stages),
        ScopeHook(),
        QANodeHook(),
        CheckpointHook(),
        ShellGuardHook(),
    ]

    # Build the Graph
    graph = build_documentary_graph(
        otio_manager=otio_manager,
        hooks=hooks,
        max_node_executions=max_node_executions,
        model=model,
    )

    # Build the recovery shell
    shell = RecoveryShell(graph, max_retries=max_retries)

    logger.info("Starting Strands documentary pipeline: %s", brief[:100])
    logger.info("  Model: %s", model_id or _MODEL_ID)
    logger.info("  Budget: $%.2f", budget_usd)
    logger.info("  Approval: %s", approval_mode)

    try:
        result = await shell.run(brief)
        summary = {
            "status": "completed",
            "backend": "strands",
            "otio_state": otio_manager.state,
            "checkpoints": len(otio_manager.checkpoints),
            "history": otio_manager.history,
            "cost": otio_manager.cost,
        }
        logger.info("Pipeline completed: %s", summary)
        return summary
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc)
        return {
            "status": "failed",
            "backend": "strands",
            "error": str(exc),
            "otio_state": otio_manager.state,
            "checkpoints": len(otio_manager.checkpoints),
        }


def main():
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python -m strands_agents.run_strands <brief>")
        print("Example: python -m strands_agents.run_strands '3 min documentary about DBS'")
        sys.exit(1)

    brief = " ".join(sys.argv[1:])
    result = asyncio.run(run_documentary(brief))

    print("\n=== Pipeline Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
