"""
Strands pipeline entry point — all parameters explicit.

Defaults are defined in DEFAULTS dict below. All can be overridden via CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

import opentimelineio as otio

from strands_agents.graph_pipeline import build_documentary_graph, RecoveryShell
from strands_agents.stages.preflight import run_preflight, PreflightError
from strands_agents.hooks.pipeline_hooks import (
    BudgetHook,
    ApprovalGateHook,
    ImmutabilityHook,
    ShellGuardHook,
)

logger = logging.getLogger(__name__)

# =============================================================================
# DEFAULTS — edit these to change baseline behavior
# =============================================================================
DEFAULTS = {
    "model": "kimi-k2.6",
    "base_url": "https://api.moonshot.ai/v1",
    "output_dir": "/tmp/documentary-pipeline",
    "budget": 100.0,
    "max_nodes": 50,
    "max_retries": 3,
    "approval": "auto_approve",
}
# =============================================================================


def _get_model(model_id: str, api_key: str, base_url: str | None = None) -> Any:
    """Return a Strands-compatible model instance.

    Uses Strands' built-in model classes — no litellm.
    - Claude models → AnthropicModel
    - Everything else → OpenAIModel with custom base_url (Kimi, DeepSeek, Qwen, local, etc.)
    """
    model_lower = model_id.lower()

    if model_lower.startswith("claude-") or model_lower.startswith("anthropic/"):
        from strands.models.anthropic import AnthropicModel
        return AnthropicModel(model_id=model_id, params={"max_tokens": 8192})

    # OpenAI-compatible: Kimi, Moonshot, DeepSeek, Qwen, local, etc.
    # OpenRouter requires the full prefix (e.g. moonshotai/kimi-k2.6)
    # Direct providers (DeepSeek, Moonshot direct) use bare model IDs
    from openai import AsyncOpenAI
    from strands.models.openai import OpenAIModel

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url or DEFAULTS["base_url"],
    )
    # Keep full model ID for OpenRouter; strip prefix only for direct providers
    if "openrouter" in (base_url or DEFAULTS["base_url"]):
        model_name = model_id
    else:
        model_name = model_id.split("/")[-1]
    return OpenAIModel(client=client, model_id=model_name, params={"max_tokens": 8192})


def _create_timeline_file(timeline_path: str) -> None:
    from tools.otio_file_ops import TRACK_V1, TRACK_A1, TRACK_A2, otio_write
    timeline = otio.schema.Timeline(name="documentary_draft")
    timeline.tracks.append(otio.schema.Track(name=TRACK_V1, kind="video"))
    timeline.tracks.append(otio.schema.Track(name=TRACK_A1, kind="audio"))
    timeline.tracks.append(otio.schema.Track(name=TRACK_A2, kind="audio"))
    timeline.metadata["documentary"] = {
        "state": "draft",
        "state_reason": "timeline_created",
        "state_history": [{"from": None, "to": "draft", "reason": "timeline_created", "timestamp": time.time()}],
    }
    otio_write(timeline_path, timeline)


def _write_pipeline_manifest(pipeline_dir: str, timeline_path: str) -> None:
    manifest = {
        "timeline_path": timeline_path,
        "pipeline_dir": pipeline_dir,
        "run_id": uuid.uuid4().hex[:8],
        "created_at": time.time(),
    }
    with open(os.path.join(pipeline_dir, "pipeline_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)


async def run_documentary(
    brief: str,
    *,
    model_id: str,
    api_key: str,
    base_url: str | None = None,
    output_dir: str = DEFAULTS["output_dir"],
    budget_usd: float = DEFAULTS["budget"],
    max_node_executions: int = DEFAULTS["max_nodes"],
    max_retries: int = DEFAULTS["max_retries"],
    approval_mode: str = DEFAULTS["approval"],
) -> dict[str, Any]:
    model = _get_model(model_id, api_key, base_url)

    report = run_preflight(output_dir=output_dir)
    if not report.passed:
        for f in report.failures:
            logger.error("Preflight: %s: %s", f.name, f.message)
        raise PreflightError(report)

    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")

    _create_timeline_file(timeline_path)
    _write_pipeline_manifest(output_dir, timeline_path)
    os.environ["PIPELINE_DIR"] = output_dir

    approval_stages = set() if approval_mode == "auto_approve" else {"scenario", "audio", "video", "assembly"}
    hooks = [ImmutabilityHook(), BudgetHook(budget_usd=budget_usd), ApprovalGateHook(gated_stages=approval_stages), ShellGuardHook()]

    graph = build_documentary_graph(hooks=hooks, max_node_executions=max_node_executions, model=model)
    shell = RecoveryShell(graph, max_retries=max_retries)

    logger.info("Brief: %s", brief[:80])
    logger.info("Model: %s", model_id)

    try:
        await shell.run(brief, initial_state={"_timeline_path": timeline_path})
        from tools.otio_lifecycle import get_otio_lifecycle_state
        return {"status": "completed", "otio_state": get_otio_lifecycle_state(timeline_path), "timeline_path": timeline_path}
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "timeline_path": timeline_path}


def main():
    parser = argparse.ArgumentParser(description="Run documentary pipeline")
    parser.add_argument("brief", nargs="+", help="Documentary brief")
    parser.add_argument("--model", "-m", default=DEFAULTS["model"], help=f"Model ID (default: {DEFAULTS['model']})")
    parser.add_argument("--api-key", "-k", required=True, help="API key")
    parser.add_argument("--base-url", default=DEFAULTS.get("base_url"), help=f"Base URL (default: {DEFAULTS.get('base_url')})")
    parser.add_argument("--output-dir", "-o", default=DEFAULTS["output_dir"], help=f"Output dir (default: {DEFAULTS['output_dir']})")
    parser.add_argument("--budget", "-b", type=float, default=DEFAULTS["budget"], help=f"Budget USD (default: {DEFAULTS['budget']})")
    parser.add_argument("--max-nodes", type=int, default=DEFAULTS["max_nodes"], help=f"Max node executions (default: {DEFAULTS['max_nodes']})")
    parser.add_argument("--max-retries", type=int, default=DEFAULTS["max_retries"], help=f"Max retries (default: {DEFAULTS['max_retries']})")
    parser.add_argument("--approval", "-a", choices=["auto_approve", "manual"], default=DEFAULTS["approval"], help=f"Approval mode (default: {DEFAULTS['approval']})")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    result = asyncio.run(run_documentary(
        brief=" ".join(args.brief),
        model_id=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        output_dir=args.output_dir,
        budget_usd=args.budget,
        max_node_executions=args.max_nodes,
        max_retries=args.max_retries,
        approval_mode=args.approval,
    ))

    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
