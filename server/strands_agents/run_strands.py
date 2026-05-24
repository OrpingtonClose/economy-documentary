"""
Strands pipeline entry point — all parameters explicit.

Defaults are defined in DEFAULTS dict below. All can be overridden via CLI.
"""

from __future__ import annotations

import os
import sys

# Ensure server/ is on sys.path so strands_agents and tools imports resolve
# when the script is run directly (python strands_agents/run_strands.py)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVER_DIR = os.path.dirname(_SCRIPT_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any

import opentimelineio as otio

from strands_agents.graph_pipeline import build_documentary_graph, RecoveryShell, STAGE_ORDER
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
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "output_dir": "/tmp/documentary-pipeline",
    "budget": 100.0,
    "max_nodes": 200,
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
        return AnthropicModel(model_id=model_id, max_tokens=8192)

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

    # Kimi K2.6: disable thinking mode (reasoning_content not preserved by
    # Strands in multi-turn tool calls → 400 from Moonshot API).
    # Direct API requires temperature=0.6.
    params: dict[str, Any] = {"max_tokens": 8192}
    if "kimi" in model_lower or "moonshot" in model_lower:
        params["temperature"] = 0.6
        params["extra_body"] = {"thinking": {"type": "disabled"}}


    return OpenAIModel(client=client, model_id=model_name, params=params)


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


def _acquire_pipeline_lock(output_dir: str) -> str:
    """Create a lock file to prevent concurrent pipeline runs.

    Returns the lock file path. Raises RuntimeError if another run is active.
    """
    lock_file = os.path.join(output_dir, ".pipeline.lock")
    os.makedirs(output_dir, exist_ok=True)

    # Check for stale lock (PID no longer exists)
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():
                # Check if the old process is still alive
                try:
                    os.kill(old_pid, 0)
                    raise RuntimeError(
                        f"Another pipeline run is active (PID {old_pid}). "
                        f"Lock file: {lock_file}. Stop the other run first."
                    )
                except OSError:
                    pass  # Process is dead, lock is stale
        except (ValueError, OSError):
            pass  # Corrupt lock file, overwrite it

    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))
    return lock_file


def _release_pipeline_lock(lock_file: str) -> None:
    """Remove the pipeline lock file."""
    try:
        os.remove(lock_file)
    except OSError:
        pass


def _destroy_all_vms() -> None:
    """Destroy all VMs recorded in the VM registry."""
    import subprocess
    from vm_registry import list_vms

    vms = list_vms()
    if not vms:
        return
    destroyed = 0
    for vm in vms:
        if vm.instance_id:
            try:
                subprocess.run(
                    ["vastai", "destroy", "instance", vm.instance_id],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                destroyed += 1
            except Exception:
                pass
    if destroyed:
        print(f"  [CLEANUP] Destroyed {destroyed} VM(s).")


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
    agent_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    model = _get_model(model_id, api_key, base_url)
    os.environ["STRANDS_MODEL"] = model_id

    # Prevent concurrent runs
    lock_file = _acquire_pipeline_lock(output_dir)
    _provisioner = None

    report = run_preflight(output_dir=output_dir)
    if not report.passed:
        for f in report.failures:
            logger.error("Preflight: %s: %s", f.name, f.message)
        _release_pipeline_lock(lock_file)
        raise PreflightError(report)

    # Lazy provisioning: workers are provisioned on-demand when tools call
    # ensure_available(). No eager provisioning at pipeline start.
    # This prevents VM leaks and ensures we only pay for what we use.

    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")

    _create_timeline_file(timeline_path)
    _write_pipeline_manifest(output_dir, timeline_path)
    os.environ["PIPELINE_DIR"] = output_dir

    approval_stages = set() if approval_mode == "auto_approve" else {"scenario", "audio", "video", "assembly"}
    hooks = [ImmutabilityHook(), ApprovalGateHook(gated_stages=approval_stages), ShellGuardHook()]
    # BudgetHook disabled — it can abort mid-run. Log-only tracking instead.
    budget_hook = BudgetHook(budget_usd=budget_usd)
    hooks.append(budget_hook)

    # Agent intervention hook — enables GET/POST into running agents
    # Disabled when DOCUMENTARY_NO_INTERVENTION is set (e.g. for CI)
    if os.environ.get("DOCUMENTARY_NO_INTERVENTION", "").strip().lower() not in ("1", "true", "yes"):
        from strands_agents.agent_intervention import InterventionHook
        hooks.append(InterventionHook())

    graph, shell = build_documentary_graph(
        hooks=hooks, max_node_executions=max_node_executions, model=model,
        agent_urls=agent_urls,
    )
    shell.max_retries = max_retries

    logger.info("Brief: %s", brief[:80])
    logger.info("Model: %s", model_id)

    # Print stage transitions for visibility
    print(f"\n{'='*60}")
    print(f"  DOCUMENTARY PIPELINE")
    print(f"  Brief: {brief[:60]}")
    print(f"  Model: {model_id}")
    print(f"  Budget: ${budget_usd:.2f}")
    print(f"  Stages: {' → '.join(STAGE_ORDER)}")
    print(f"{'='*60}\n")

    # Pull latest agent memory from git
    try:
        from agent_memory import pull_memory
        pull_result = pull_memory()
        logger.info("Agent memory pull: %s", pull_result)
    except Exception as exc:
        logger.warning("Agent memory pull failed (non-blocking): %s", exc)

    result: dict[str, Any] = {}
    try:
        await shell.run(brief, initial_state={"_timeline_path": timeline_path})
        from tools.otio_lifecycle import get_otio_lifecycle_state

        # Verify master.mp4 exists before claiming success
        assembly_output = None
        try:
            from tools.otio_metadata import read_pipeline_metadata
            assembly_output = read_pipeline_metadata(timeline_path, "assembly_output_path")
        except Exception:
            pass

        master_mp4 = assembly_output or os.path.join(output_dir, "master.mp4")
        if os.path.exists(master_mp4):
            size_mb = os.path.getsize(master_mp4) / (1024 * 1024)
            result = {
                "status": "completed",
                "otio_state": get_otio_lifecycle_state(timeline_path),
                "timeline_path": timeline_path,
                "output_path": master_mp4,
                "output_size_mb": round(size_mb, 2),
            }
            print(f"\n{'='*60}")
            print(f"  PIPELINE COMPLETE")
            print(f"  Output: {master_mp4}")
            print(f"  Size: {size_mb:.1f} MB")
            print(f"{'='*60}")
        else:
            result = {
                "status": "failed",
                "error": f"Pipeline reported complete but output file missing: {master_mp4}",
                "timeline_path": timeline_path,
            }
            print(f"\n{'='*60}")
            print(f"  PIPELINE FAILED — output file missing")
            print(f"  Expected: {master_mp4}")
            print(f"{'='*60}")
    except Exception as exc:
        result = {"status": "failed", "error": str(exc), "timeline_path": timeline_path}
        print(f"\n{'='*60}")
        print(f"  PIPELINE FAILED: {exc}")
        print(f"{'='*60}")
    finally:
        # Commit agent memory to git regardless of success/failure
        try:
            from agent_memory import commit_memory
            commit_result = commit_memory(f"run: {brief[:60]}")
            logger.info("Agent memory commit: %s", commit_result)
        except Exception as exc:
            logger.warning("Agent memory commit failed (non-blocking): %s", exc)

        # CRITICAL: Destroy VMs to stop credit burn
        try:
            print("\n  [CLEANUP] Destroying VMs...")
            _destroy_all_vms()
            print("  [CLEANUP] VMs destroyed.")
        except Exception as exc:
            logger.warning("VM cleanup failed (non-blocking): %s", exc)

        # Release lock
        _release_pipeline_lock(lock_file)

    return result


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
    parser.add_argument("--agent-url", action="append", default=[], metavar="NODE=URL", help="Override agent URL (e.g. --agent-url scenario=http://localhost:9001). Repeatable.")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # Handle Ctrl+C gracefully — destroy VMs before exiting
    _provisioner_for_signal = None

    def _sigint_handler(signum, frame):
        print("\n\n[INTERRUPT] Ctrl+C received — destroying VMs...")
        try:
            _destroy_all_vms()
        except Exception as exc:
            print(f"[INTERRUPT] VM cleanup error: {exc}")
        # Release lock if held
        try:
            lock = os.path.join(args.output_dir, ".pipeline.lock")
            if os.path.exists(lock):
                os.remove(lock)
        except Exception:
            pass
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigint_handler)

    agent_urls = {}
    for entry in args.agent_url:
        if "=" in entry:
            node, url = entry.split("=", 1)
            agent_urls[node] = url

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
        agent_urls=agent_urls if agent_urls else None,
    ))

    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
