"""
Strands pipeline entry point — direct API calls, no litellm.

Environment variables:
    STRANDS_MODEL         Model ID (default: claude-sonnet-4-20250514)
    STRANDS_API_KEY       API key for the model provider
    STRANDS_BASE_URL      Base URL (for OpenAI-compatible providers like Kimi)
    PIPELINE_OUTPUT_DIR   Output directory (default: /tmp/documentary-pipeline)
    PIPELINE_BUDGET_USD   Budget in USD (default: 100.0)
    PIPELINE_MAX_NODES    Max node executions (default: 50)
    PIPELINE_MAX_RETRIES  Max recovery retries (default: 3)
    PIPELINE_APPROVAL     Approval mode: auto_approve or manual (default: auto_approve)
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
from typing import Any, AsyncIterator

import httpx
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

# Defaults
DEFAULT_MODEL = os.environ.get("STRANDS_MODEL", "claude-sonnet-4-20250514")
DEFAULT_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")
DEFAULT_BUDGET = float(os.environ.get("PIPELINE_BUDGET_USD", "100.0"))
DEFAULT_MAX_NODES = int(os.environ.get("PIPELINE_MAX_NODES", "50"))
DEFAULT_MAX_RETRIES = int(os.environ.get("PIPELINE_MAX_RETRIES", "3"))
DEFAULT_APPROVAL = os.environ.get("PIPELINE_APPROVAL", "auto_approve")


class DirectModel:
    """Direct API caller — no litellm indirection.

    Supports:
        - Anthropic Claude (via Anthropic API)
        - OpenAI-compatible providers (Kimi, Moonshot, etc.)
    """

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8192,
    ):
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.api_key = api_key or os.environ.get("STRANDS_API_KEY", "")
        self.base_url = base_url or os.environ.get("STRANDS_BASE_URL", "")

        # Determine provider from model_id
        model_lower = model_id.lower()
        if model_lower.startswith("claude-") or model_lower.startswith("anthropic/"):
            self.provider = "anthropic"
            self.api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            self.base_url = self.base_url or "https://api.anthropic.com"
        else:
            # OpenAI-compatible (Kimi, Moonshot, local, etc.)
            self.provider = "openai"
            self.api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
            if not self.base_url:
                if "kimi" in model_lower or "moonshot" in model_lower:
                    self.base_url = "https://api.moonshot.cn/v1"
                else:
                    self.base_url = "https://api.openai.com/v1"

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        """Stream completion from the model provider.

        Yields events compatible with Strands event loop.
        """
        if self.provider == "anthropic":
            async for event in self._stream_anthropic(messages, tools):
                yield event
        else:
            async for event in self._stream_openai(messages, tools):
                yield event

    async def _stream_anthropic(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> AsyncIterator[dict]:
        """Stream from Anthropic API."""
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model_id.split("/")[-1],
            "max_tokens": self.max_tokens,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        async with self._client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    yield event
                except json.JSONDecodeError:
                    continue

    async def _stream_openai(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> AsyncIterator[dict]:
        """Stream from OpenAI-compatible API (Kimi, Moonshot, etc.)."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }
        # OpenAI-compatible uses same format for tools
        tool_schemas = tools if tools else None

        payload = {
            "model": self.model_id.split("/")[-1],
            "max_tokens": self.max_tokens,
            "messages": messages,
            "stream": True,
        }
        if tool_schemas:
            payload["tools"] = tool_schemas

        async with self._client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    yield chunk
                except json.JSONDecodeError:
                    continue

    async def close(self) -> None:
        await self._client.aclose()


def _create_timeline_file(timeline_path: str) -> None:
    """Create the OTIO timeline file on disk."""
    from tools.otio_file_ops import TRACK_V1, TRACK_A1, TRACK_A2, otio_write

    timeline = otio.schema.Timeline(name="documentary_draft")
    timeline.tracks.append(otio.schema.Track(name=TRACK_V1, kind="video"))
    timeline.tracks.append(otio.schema.Track(name=TRACK_A1, kind="audio"))
    timeline.tracks.append(otio.schema.Track(name=TRACK_A2, kind="audio"))

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
    """Write the pipeline manifest."""
    manifest = {
        "timeline_path": timeline_path,
        "pipeline_dir": pipeline_dir,
        "run_id": os.environ.get("DOCUMENTARY_RUN_ID", uuid.uuid4().hex[:8]),
        "created_at": time.time(),
    }
    manifest_path = os.path.join(pipeline_dir, "pipeline_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


async def run_documentary(
    brief: str,
    *,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    model_id: str = DEFAULT_MODEL,
    budget_usd: float = DEFAULT_BUDGET,
    max_node_executions: int = DEFAULT_MAX_NODES,
    max_retries: int = DEFAULT_MAX_RETRIES,
    approval_mode: str = DEFAULT_APPROVAL,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Run the full documentary pipeline.

    Args:
        brief: The documentary brief.
        output_dir: Pipeline output directory.
        model_id: Model ID (e.g. claude-sonnet-4-20250514, moonshot-v1-8k).
        budget_usd: Cost budget.
        max_node_executions: Safety limit.
        max_retries: Recovery retries.
        approval_mode: auto_approve or manual.
        api_key: Override API key.
        base_url: Override base URL.

    Returns:
        Pipeline result dict.
    """
    # Instantiate direct model
    model = DirectModel(model_id, api_key=api_key, base_url=base_url)

    # Preflight
    report = run_preflight(output_dir=output_dir)
    if not report.passed:
        logger.error("Preflight failed:")
        for f in report.failures:
            logger.error("  %s: %s", f.name, f.message)
        raise PreflightError(report)
    logger.info("Preflight passed")

    # Create pipeline directory
    pipeline_dir = output_dir
    timeline_dir = os.path.join(pipeline_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")

    _create_timeline_file(timeline_path)
    _write_pipeline_manifest(pipeline_dir, timeline_path)
    os.environ["PIPELINE_DIR"] = pipeline_dir

    # Build hooks
    approval_stages = set() if approval_mode == "auto_approve" else {
        "scenario", "audio", "video", "assembly"
    }
    hooks = [
        ImmutabilityHook(),
        BudgetHook(budget_usd=budget_usd),
        ApprovalGateHook(gated_stages=approval_stages),
        ShellGuardHook(),
    ]

    # Build graph
    graph = build_documentary_graph(
        hooks=hooks,
        max_node_executions=max_node_executions,
        model=model,
    )

    # Recovery shell
    shell = RecoveryShell(graph, max_retries=max_retries)

    logger.info("Starting pipeline: %s", brief[:100])
    logger.info("  Model: %s", model_id)
    logger.info("  Budget: $%.2f", budget_usd)

    try:
        result = await shell.run(brief, initial_state={"_timeline_path": timeline_path})
        from tools.otio_lifecycle import get_otio_lifecycle_state
        summary = {
            "status": "completed",
            "backend": "strands",
            "otio_state": get_otio_lifecycle_state(timeline_path),
            "timeline_path": timeline_path,
        }
        return summary
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc)
        return {"status": "failed", "error": str(exc), "timeline_path": timeline_path}
    finally:
        await model.close()


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run the documentary pipeline (direct API, no litellm)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("brief", nargs="+", help="The documentary brief")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Model ID")
    parser.add_argument("--output-dir", "-o", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("--budget", "-b", type=float, default=DEFAULT_BUDGET, help="Budget in USD")
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES, help="Max node executions")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Max retries")
    parser.add_argument("--approval", "-a", choices=["auto_approve", "manual"], default=DEFAULT_APPROVAL)
    parser.add_argument("--api-key", help="Override API key")
    parser.add_argument("--base-url", help="Override base URL")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    brief = " ".join(args.brief)
    result = asyncio.run(run_documentary(
        brief,
        output_dir=args.output_dir,
        model_id=args.model,
        budget_usd=args.budget,
        max_node_executions=args.max_nodes,
        max_retries=args.max_retries,
        approval_mode=args.approval,
        api_key=args.api_key,
        base_url=args.base_url,
    ))

    print("\n=== Pipeline Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
