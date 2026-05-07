"""
Strands pipeline entry point — replaces server/run_pipeline.py for Strands mode.

This is what ``PIPELINE_BACKEND=strands`` activates. It:
1. Creates the OTIO timeline file on disk (stateless — no in-memory manager)
2. Writes the pipeline manifest for cross-process discovery
3. Builds the 4-node Graph with gate validation
4. Runs the Graph via RecoveryShell
5. Streams events to the AG-UI bus via SSEBridge

Usage::

    python -m strands_agents.run_strands "Make a 3 minute documentary about deep brain stimulation"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

import opentimelineio as otio
from strands.models.anthropic import AnthropicModel

from strands_agents.graph_pipeline import build_documentary_graph, RecoveryShell
from strands_agents.stages.preflight import run_preflight, PreflightError
from strands_agents.hooks.pipeline_hooks import (
    BudgetHook,
    ApprovalGateHook,
    ImmutabilityHook,
    ShellGuardHook,
)
from strands_agents.sse_bridge import SSEBridge

logger = logging.getLogger(__name__)

# Default model
_MODEL_ID = os.environ.get("STRANDS_MODEL", "claude-sonnet-4-20250514")


def _create_timeline_file(timeline_path: str) -> None:
    """Create the OTIO timeline file on disk with initial structure.

    This is the ONLY shared state in the pipeline. Every agent reads
    from and writes to this file. No in-memory manager needed.
    """
    from tools.otio_file_ops import TRACK_V1, TRACK_A1, TRACK_A2, otio_write

    timeline = otio.schema.Timeline(name="documentary_draft")

    # Create tracks
    timeline.tracks.append(otio.schema.Track(name=TRACK_V1, kind="video"))
    timeline.tracks.append(otio.schema.Track(name=TRACK_A1, kind="audio"))
    timeline.tracks.append(otio.schema.Track(name=TRACK_A2, kind="audio"))

    # Initialize pipeline metadata
    timeline.metadata["documentary"] = {
        "state": "draft",
        "state_reason": "timeline_created",
        "state_history": [
            {"from": None, "to": "draft", "reason": "timeline_created", "timestamp": time.time()}
        ],
    }

    otio_write(timeline_path, timeline)
    logger.info("Timeline written to %s", timeline_path)


def _write_pipeline_manifest(pipeline_dir: str, timeline_path: str) -> None:
    """Write the pipeline manifest for cross-process timeline discovery.

    The manifest is a small JSON file at a well-known location.
    Every tool function reads it to discover the timeline path.
    No env-var passing needed (except PIPELINE_DIR set once before forking).
    """
    manifest = {
        "timeline_path": timeline_path,
        "pipeline_dir": pipeline_dir,
        "run_id": os.environ.get("DOCUMENTARY_RUN_ID", uuid.uuid4().hex[:8]),
        "created_at": time.time(),
    }
    manifest_path = os.path.join(pipeline_dir, "pipeline_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    logger.info("Pipeline manifest written to %s", manifest_path)


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

    # Create pipeline directory structure
    pipeline_dir = output_dir
    timeline_dir = os.path.join(pipeline_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")

    # Create the OTIO timeline file on disk (stateless)
    _create_timeline_file(timeline_path)

    # Write the pipeline manifest for cross-process discovery
    _write_pipeline_manifest(pipeline_dir, timeline_path)

    # Set PIPELINE_DIR so resolve_timeline_path() can find the manifest
    os.environ["PIPELINE_DIR"] = pipeline_dir

    # Provisioning is NOT done at startup. The merged agents
    # (Audio+Provisioner, Video+Provisioner) provision workers when
    # they need them. They search Vast.ai, read results, reason about
    # which GPU to pick, and remember past decisions in Letta memory.

    # Build hooks — only safety invariants, no contract hooks
    # (OTIO gate node enforces contracts at stage boundaries)
    approval_stages = set() if approval_mode == "auto_approve" else {
        "scenario", "audio", "video"
    }
    hooks = [
        ImmutabilityHook(),
        BudgetHook(budget_usd=budget_usd),
        ApprovalGateHook(gated_stages=approval_stages),
        ShellGuardHook(),
    ]

    # Build the Graph (no more otio_manager parameter)
    graph = build_documentary_graph(
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
        result = await shell.run(
            brief,
            initial_state={"_timeline_path": timeline_path},
        )
        # Read final state from the OTIO file
        from tools.otio_lifecycle import get_otio_lifecycle_state
        from tools.otio_file_ops import otio_read
        timeline = otio_read(timeline_path)
        doc_meta = timeline.metadata.get("documentary", {})
        summary = {
            "status": "completed",
            "backend": "strands",
            "otio_state": get_otio_lifecycle_state(timeline_path),
            "timeline_path": timeline_path,
        }
        logger.info("Pipeline completed: %s", summary)
        return summary
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc)
        return {
            "status": "failed",
            "backend": "strands",
            "error": str(exc),
            "timeline_path": timeline_path,
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
