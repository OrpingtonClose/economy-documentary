"""
Strands pipeline entry point — zero configuration.

Usage:
    python strands_agents/run_strands.py "Your documentary brief"

All parameters are hardcoded below. Edit the file to change behavior.
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

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402
  # noqa: E402
import opentimelineio as otio  # noqa: E402
  # noqa: E402
from strands_agents.graph_pipeline import build_documentary_graph, STAGE_ORDER  # noqa: E402
from strands_agents.stages.preflight import run_preflight, PreflightError  # noqa: E402
from strands_agents.hooks.pipeline_hooks import (  # noqa: E402
    BudgetHook,
    ApprovalGateHook,
    ImmutabilityHook,
    ShellGuardHook,
)

logger = logging.getLogger(__name__)

# =============================================================================
# HARD-CODED CONFIGURATION — edit these values to change behavior
# =============================================================================
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_OUTPUT_DIR = "/Users/orpington/Documents/documentary-pipeline"
_DEFAULT_BUDGET = 5.0
_DEFAULT_MAX_NODES = 200
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_APPROVAL = "auto_approve"
_API_KEY_PATH = os.path.expanduser("~/api_keys/LLMS/deepseek_api.txt")
# =============================================================================

# ---------------------------------------------------------------------------
# HANG MONITORING — never silently fail again
# ---------------------------------------------------------------------------

class _HangMonitor:
    """Watchdog thread that dumps the main thread's stack every N seconds.

    If the main thread is stuck in the same frame for >threshold seconds,
    prints a loud warning and dumps the full traceback.
    """
    def __init__(self, interval: float = 30.0, threshold: float = 120.0) -> None:
        self.interval = interval
        self.threshold = threshold
        self._thread = threading.Thread(target=self._run, daemon=True, name="hang-monitor")
        self._last_frame: str = ""
        self._last_change = time.time()
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread.start()
        print(f"[MONITOR] Hang monitor started (interval={self.interval}s, threshold={self.threshold}s)")

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            time.sleep(self.interval)
            try:
                main_thread = threading.main_thread()
                frames = sys._current_frames()
                ident = main_thread.ident
                if ident is None:
                    continue
                frame = frames.get(ident)
                if frame is None:
                    continue
                summary = traceback.format_stack(frame, limit=3)[-1]
                now = time.time()
                if summary == self._last_frame:
                    stuck = now - self._last_change
                    if stuck > self.threshold:
                        print(f"\n[MONITOR] ⚠️ MAIN THREAD STUCK for {stuck:.0f}s in:\n{summary}")
                        print("[MONITOR] Dumping full traceback to stderr...")
                        traceback.print_stack(frame)
                else:
                    self._last_frame = summary
                    self._last_change = now
            except Exception as exc:
                print(f"[MONITOR] watchdog error: {exc}")


# sys.monitoring: trace every function call if available (Python 3.12+)
if hasattr(sys, "monitoring"):
    try:
        sys.monitoring.use_tool_id(0, "pipeline-tracer")
        sys.monitoring.set_events(0, sys.monitoring.events.PY_START | sys.monitoring.events.PY_RETURN)
        _call_counts: dict[str, int] = {}
        def _monitor_callback(code, instruction_offset):
            name = code.co_name
            _call_counts[name] = _call_counts.get(name, 0) + 1
            return _monitor_callback
        sys.monitoring.register_callback(0, sys.monitoring.events.PY_START, _monitor_callback)
    except Exception as exc:
        print(f"[MONITOR] sys.monitoring init failed: {exc}")



def _read_api_key() -> str:
    """Read API key from the fixed path. Fail loudly if missing."""
    if not os.path.exists(_API_KEY_PATH):
        raise RuntimeError(
            f"API key file not found: {_API_KEY_PATH}\n"
            f"Create this file with the DeepSeek API key."
        )
    with open(_API_KEY_PATH) as f:
        key = f.read().strip()
    if not key:
        raise RuntimeError(f"API key file is empty: {_API_KEY_PATH}")
    return key


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

    # NO TIMEOUT: per /cheat, the agent decides when something is taking too long.
    # Hardcoded timeouts constrain agent reasoning. Use notify_maintainer instead.
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url or _DEFAULT_BASE_URL,
    )
    # Keep full model ID for OpenRouter; strip prefix only for direct providers
    if "openrouter" in (base_url or _DEFAULT_BASE_URL):
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


async def run_documentary(
    brief: str,
    *,
    model_id: str = _DEFAULT_MODEL,
    api_key: str | None = None,
    base_url: str | None = _DEFAULT_BASE_URL,
    output_dir: str = _DEFAULT_OUTPUT_DIR,
    budget_usd: float = _DEFAULT_BUDGET,
    max_node_executions: int = _DEFAULT_MAX_NODES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    approval_mode: str = _DEFAULT_APPROVAL,
) -> dict[str, Any]:
    api_key = api_key or _read_api_key()
    model = _get_model(model_id, api_key, base_url)

    # Start CPython auto-tracer (sys.monitoring) + SQLite WAL store
    run_id = f"run_{int(time.time())}"
    db_path = os.path.join(output_dir, "traces", "pipeline.db")
    from tracing import AutoTracer
    auto_tracer = AutoTracer(db_path)
    auto_tracer.start_run(run_id, topic=brief[:200])
    auto_tracer.start()

    # Prevent concurrent runs
    lock_file = _acquire_pipeline_lock(output_dir)
    _provisioner = None

    # Make run_id available to tools and callbacks
    os.environ["_RUN_ID"] = run_id

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
    os.environ["PIPELINE_DIR"] = output_dir  # tools still reference this env var

    approval_stages = set() if approval_mode == "auto_approve" else {"scenario", "audio", "video", "assembly"}
    hooks = [ImmutabilityHook(), ApprovalGateHook(gated_stages=approval_stages), ShellGuardHook()]
    # BudgetHook disabled — it can abort mid-run. Log-only tracking instead.
    budget_hook = BudgetHook(budget_usd=budget_usd)
    hooks.append(budget_hook)

    # Snapshot hook — records every tool call and graph transition to SQLite
    from tracing.snapshot_hooks import SnapshotHook
    snapshot_hook = SnapshotHook(run_id=run_id)
    hooks.append(snapshot_hook)

    graph, shell = build_documentary_graph(
        hooks=hooks, max_node_executions=max_node_executions, model=model, run_id=run_id
    )
    shell.max_retries = max_retries

    logger.info("Brief: %s", brief[:80])
    logger.info("Model: %s", model_id)

    # Print stage transitions for visibility
    print(f"\n{'='*60}")
    print("  DOCUMENTARY PIPELINE")
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
        except Exception as exc:
            logger.warning("read_pipeline_metadata failed: %s", exc)

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
            print("  PIPELINE COMPLETE")
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
            print("  PIPELINE FAILED — output file missing")
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

        # Snapshot VM and OTIO state before cleanup
        try:
            from tracing.snapshot_hooks import snapshot_vm_state, snapshot_otio_state
            from worker_provisioner import get_provisioner
            prov = get_provisioner()
            if prov:
                snapshot_vm_state(run_id, prov)
            snapshot_otio_state(run_id, timeline_path)
        except Exception as exc:
            logger.debug("Snapshot before cleanup failed: %s", exc)

        # CRITICAL: Destroy VMs to stop credit burn
        try:
            from worker_provisioner import get_provisioner
            _provisioner = get_provisioner()
            if _provisioner:
                print("\n  [CLEANUP] Destroying VMs...")
                _provisioner.cleanup(destroy_vms=True)
                print("  [CLEANUP] VMs destroyed.")
        except Exception as exc:
            logger.warning("VM cleanup failed (non-blocking): %s", exc)

        # Stop auto-tracer
        try:
            auto_tracer.stop()
            auto_tracer.end_run(status=result.get("status", "unknown"))
        except Exception as exc:
            logger.warning("Tracer stop failed: %s", exc)

        # Release lock
        _release_pipeline_lock(lock_file)

    return result


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python strands_agents/run_strands.py <brief>", file=sys.stderr)
        sys.exit(1)

    brief = " ".join(sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # Start hang monitor
    monitor = _HangMonitor(interval=30.0, threshold=120.0)
    monitor.start()

    # Handle Ctrl+C gracefully — destroy VMs before exiting
    def _sigint_handler(signum: int, frame: Any) -> None:
        print("\n\n[INTERRUPT] Ctrl+C received — destroying VMs...")
        monitor.stop()
        try:
            from worker_provisioner import get_provisioner
            prov = get_provisioner()
            if prov:
                prov.cleanup(destroy_vms=True)
        except Exception as exc:
            print(f"[INTERRUPT] VM cleanup error: {exc}")
        # Release lock if held
        try:
            lock = os.path.join(_DEFAULT_OUTPUT_DIR, ".pipeline.lock")
            if os.path.exists(lock):
                os.remove(lock)
        except Exception as exc:
            print(f"[INTERRUPT] lock release error: {exc}")
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigint_handler)

    print(f"\n[MONITOR] Starting pipeline for: {brief}")
    start_time = time.time()
    result: dict[str, Any] = {}
    try:
        result = asyncio.run(run_documentary(brief=brief))
    except Exception as exc:
        elapsed = time.time() - start_time
        print(f"\n[MONITOR] Pipeline FAILED after {elapsed:.0f}s: {exc}")
        traceback.print_exc()
        result = {"status": "failed", "error": str(exc), "elapsed_sec": elapsed}
    finally:
        monitor.stop()
        elapsed = time.time() - start_time
        print(f"\n[MONITOR] Pipeline finished in {elapsed:.0f}s")
        if result.get("status") != "completed":
            print(f"[MONITOR] FAILURE REASON: {result.get('error', 'unknown')}")

    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
