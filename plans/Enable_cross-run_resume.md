> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Plan: Enable cross-run resume

# Implementation Plan: Enable Cross-Run Resume

## Overview
Currently, `RecoveryShell.resume` is hardcoded to `False`, preventing the pipeline from resuming from checkpoints. This plan enables reading `metadata.json` on startup to detect incomplete runs and seed the timeline from the last checkpoint.

## 1. Files to Modify

### 1.1 `server/strands_agents/run_strands.py`

#### Change 1: Add checkpoint detection function (after line 40, before `_HangMonitor` class)

```python
# =============================================================================
# CHECKPOINT DETECTION — enable cross-run resume
# =============================================================================

def _find_incomplete_run(output_dir: str) -> dict[str, Any] | None:
    """Scan checkpoints for incomplete runs and return metadata if found.

    Returns the metadata dict of the most recent incomplete run, or None.
    """
    checkpoints_dir = os.path.join(output_dir, "checkpoints")
    if not os.path.isdir(checkpoints_dir):
        return None

    # List all run directories sorted by modification time (newest first)
    runs = []
    for entry in os.listdir(checkpoints_dir):
        run_dir = os.path.join(checkpoints_dir, entry)
        if not os.path.isdir(run_dir):
            continue
        meta_path = os.path.join(run_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, "r") as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Check if run is incomplete (no final completion marker)
        lifecycle = metadata.get("lifecycle_state", "")
        if lifecycle not in ("completed", "failed"):
            runs.append((os.path.getmtime(run_dir), run_dir, metadata))

    if not runs:
        return None

    # Return the most recent incomplete run
    runs.sort(key=lambda x: x[0], reverse=True)
    _, run_dir, metadata = runs[0]
    metadata["_run_dir"] = run_dir
    return metadata
```

#### Change 2: Modify `run_documentary` function to accept resume parameters

Replace the existing `run_documentary` function (starting around line 200) with:

```python
async def run_documentary(
    brief: str,
    resume_run_id: str | None = None,
    resume_timeline_path: str | None = None,
) -> dict[str, Any]:
    """Run the documentary pipeline, optionally resuming from a checkpoint.

    Args:
        brief: The documentary brief/prompt.
        resume_run_id: If set, resume from this run's checkpoint.
        resume_timeline_path: Path to the OTIO timeline file to resume from.
    """
    run_id = resume_run_id or str(uuid.uuid4())[:8]
    output_dir = _DEFAULT_OUTPUT_DIR
    lock_file = os.path.join(output_dir, ".pipeline.lock")

    # Acquire pipeline lock
    if not _acquire_pipeline_lock(lock_file):
        return {"status": "locked", "error": "Another pipeline instance is running"}

    # Initialize auto-tracer
    try:
        from strands_agents.hooks.pipeline_hooks import AutoTracer
        auto_tracer = AutoTracer(run_id=run_id, output_dir=output_dir)
        auto_tracer.start()
    except Exception as exc:
        logger.warning("AutoTracer init failed (non-blocking): %s", exc)
        auto_tracer = _NullTracer()

    # Determine if we're resuming
    is_resume = resume_run_id is not None and resume_timeline_path is not None

    # Build the graph with resume support
    graph = build_documentary_graph(
        brief=brief,
        run_id=run_id,
        output_dir=output_dir,
        resume=is_resume,
        resume_timeline_path=resume_timeline_path,
    )

    # ... rest of the function remains the same from here ...
    # (The existing code from the original function continues below)
```

#### Change 3: Modify `main()` to detect and offer resume

Replace the existing `main()` function with:

```python
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

    # =========================================================================
    # NEW: Check for incomplete runs and offer resume
    # =========================================================================
    resume_run_id = None
    resume_timeline_path = None

    incomplete_run = _find_incomplete_run(_DEFAULT_OUTPUT_DIR)
    if incomplete_run:
        run_dir = incomplete_run["_run_dir"]
        run_id = os.path.basename(run_dir)
        lifecycle = incomplete_run.get("lifecycle_state", "unknown")
        completed_stages = incomplete_run.get("completed_stages", [])

        print(f"\n{'='*60}")
        print(f"  INCOMPLETE RUN DETECTED")
        print(f"  Run ID: {run_id}")
        print(f"  State: {lifecycle}")
        print(f"  Completed stages: {', '.join(completed_stages) if completed_stages else 'none'}")
        print(f"{'='*60}")

        # Check if there's a valid timeline to resume from
        timeline_path = os.path.join(run_dir, "otio", "timeline.otio")
        if os.path.isfile(timeline_path):
            print(f"\n  Found existing timeline: {timeline_path}")
            print(f"  Last modified: {time.ctime(os.path.getmtime(timeline_path))}")

            # Auto-resume (no user prompt needed for automation)
            resume_run_id = run_id
            resume_timeline_path = timeline_path
            print(f"\n  [AUTO-RESUME] Continuing from run {run_id}")
        else:
            print(f"\n  No timeline found at {timeline_path}")
            print(f"  Starting fresh run instead.")
    else:
        print("\n  No incomplete runs found. Starting fresh pipeline.")

    # =========================================================================
    # END NEW
    # =========================================================================

    print(f"\n[MONITOR] Starting pipeline for: {brief}")
    start_time = time.time()
    result: dict[str, Any] = {}
    try:
        result = asyncio.run(run_documentary(
            brief=brief,
            resume_run_id=resume_run_id,
            resume_timeline_path=resume_timeline_path,
        ))
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
```

### 1.2 `server/strands_agents/graph_pipeline.py`

#### Change 4: Modify `build_documentary_graph` to accept resume parameters

Find the `build_documentary_graph` function (around line 300) and modify its signature and implementation:

```python
def build_documentary_graph(
    brief: str,
    run_id: str,
    output_dir: str,
    resume: bool = False,
    resume_timeline_path: str | None = None,
) -> Graph:
    """Build the documentary pipeline graph.

    Args:
        brief: The documentary brief.
        run_id: Unique run identifier.
        output_dir: Base output directory.
        resume: If True, seed the timeline from the last checkpoint.
        resume_timeline_path: Path to the OTIO timeline file to resume from.
    """
    # ... existing code ...

    # =========================================================================
    # NEW: Configure RecoveryShell for resume
    # =========================================================================
    from strands_agents.hooks.pipeline_hooks import get_recovery_shell

    shell = get_recovery_shell()
    if shell and resume:
        shell.resume = True
        if resume_timeline_path and os.path.isfile(resume_timeline_path):
            # Seed the timeline from the checkpoint
            from tools.otio_file_ops import otio_read
            try:
                timeline = otio_read(resume_timeline_path)
                # Store the timeline in the shell for later use
                shell.resume_timeline = timeline
                shell.resume_timeline_path = resume_timeline_path
                logger.info("Resume enabled: seeded timeline from %s", resume_timeline_path)
            except Exception as exc:
                logger.warning("Failed to load resume timeline: %s", exc)
                shell.resume = False
        else:
            logger.warning("Resume requested but no timeline path provided")
            shell.resume = False
    # =========================================================================
    # END NEW
    # =========================================================================

    # ... rest of the function ...
```

### 1.3 `server/strands_agents/hooks/pipeline_hooks.py`

#### Change 5: Add resume support to `RecoveryShell`

Find the `RecoveryShell` class and modify it:

```python
class RecoveryShell:
    """Tracks pipeline recovery state across stages."""

    def __init__(self) -> None:
        self.completed_stages: set[str] = set()
        self.resume: bool = False  # Changed from hardcoded False
        self.resume_timeline: Any = None  # OTIO timeline object
        self.resume_timeline_path: str | None = None
        self.last_checkpoint: str | None = None  # Path to last checkpoint dir
```

#### Change 6: Add method to load checkpoint metadata

Add this method to `RecoveryShell`:

```python
    def load_checkpoint_metadata(self, checkpoint_dir: str) -> dict[str, Any]:
        """Load metadata from a checkpoint directory.

        Returns empty dict if no metadata.json exists.
        """
        meta_path = os.path.join(checkpoint_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            return {}
        try:
            with open(meta_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load checkpoint metadata from %s", meta_path)
            return {}
```

#### Change 7: Add method to save checkpoint metadata

Add this method to `RecoveryShell`:

```python
    def save_checkpoint_metadata(self, checkpoint_dir: str, metadata: dict[str, Any]) -> None:
        """Save metadata to a checkpoint directory."""
        os.makedirs(checkpoint_dir, exist_ok=True)
        meta_path = os.path.join(checkpoint_dir, "metadata.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(metadata, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to save checkpoint metadata: %s", exc)
```

### 1.4 `server/strands_agents/stages/preflight.py`

#### Change 8: Add resume-aware preflight check

Add a new function to check if resume is possible:

```python
def check_resume_possible(run_id: str, output_dir: str) -> tuple[bool, str | None]:
    """Check if a run can be resumed.

    Returns:
        (can_resume, timeline_path_or_None)
    """
    from strands_agents.graph_pipeline import checkpoint_dir

    cp_dir = checkpoint_dir(run_id)
    meta_path = os.path.join(cp_dir, "metadata.json")

    if not os.path.isfile(meta_path):
        return False, None

    try:
        with open(meta_path, "r") as f:
            metadata = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False, None

    lifecycle = metadata.get("lifecycle_state", "")
    if lifecycle in ("completed", "failed"):
        return False, None  # Already finished

    # Check for timeline file
    timeline_path = os.path.join(cp_dir, "otio", "timeline.otio")
    if not os.path.isfile(timeline_path):
        return False, None

    return True, timeline_path
```

## 2. Dependencies and Side Effects

### Dependencies
- `os.path` and `json` (already imported)
- `time` (already imported)
- `tools.otio_file_ops` (must be importable)
- `tools.otio_metadata` (must be importable)

### Side Effects
1. **Lock file**: Resume detection reads checkpoint directories but doesn't modify them
2. **Timeline loading**: Loading a large OTIO file could be memory-intensive
3. **Stage skipping**: If resume is enabled, completed stages will be skipped, which could cause issues if stage outputs are inconsistent

## 3. Testing Approach

### Unit Tests

```python
# tests/test_resume.py

import os
import json
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from strands_agents.run_strands import _find_incomplete_run


class TestFindIncompleteRun:
    def test_no_checkpoints_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _find_incomplete_run(tmpdir)
            assert result is None

    def test_empty_checkpoints_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "checkpoints", "run_001"))
            result = _find_incomplete_run(tmpdir)
            assert result is None

    def test_completed_run_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = os.path.join(tmpdir, "checkpoints", "run_001")
            os.makedirs(run_dir)
            meta = {"lifecycle_state": "completed", "run_id": "run_001"}
            with open(os.path.join(run_dir, "metadata.json"), "w") as f:
                json.dump(meta, f)
            result = _find_incomplete_run(tmpdir)
            assert result is None

    def test_incomplete_run_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = os.path.join(tmpdir, "checkpoints", "run_001")
            os.makedirs(run_dir)
            meta = {"lifecycle_state": "in_progress", "run_id": "run_001"}
            with open(os.path.join(run_dir, "metadata.json"), "w") as f:
                json.dump(meta, f)
            result = _find_incomplete_run(tmpdir)
            assert result is not None
            assert result["run_id"] == "run_001"
            assert result["_run_dir"] == run_dir

    def test_multiple_incomplete_runs_returns_newest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two runs with different timestamps
            run1_dir = os.path.join(tmpdir, "checkpoints", "run_001")
            run2_dir = os.path.join(tmpdir, "checkpoints", "run_002")
            os.makedirs(run1_dir)
            os.makedirs(run2_dir)

            meta1 = {"lifecycle_state": "in_progress", "run_id": "run_001"}
            meta2 = {"lifecycle_state": "in_progress", "run_id": "run_002"}

            with open(os.path.join(run1_dir, "metadata.json"), "w") as f:
                json.dump(meta1, f)
            with open(os.path.join(run2_dir, "metadata.json"), "w") as f:
                json.dump(meta2, f)

            # Touch run2 to make it newer
            os.utime(run2_dir, (time.time(), time.time() + 10))

            result = _find_incomplete_run(tmpdir)
            assert result is not None
            assert result["run_id"] == "run_002"


class TestRecoveryShellResume:
    def test_resume_disabled_by_default(self):
        from strands_agents.hooks.pipeline_hooks import RecoveryShell
        shell = RecoveryShell()
        assert shell.resume is False

    def test_resume_enabled(self):
        from strands_agents.hooks.pipeline_hooks import RecoveryShell
        shell = RecoveryShell()
        shell.resume = True
        assert shell.resume is True

    def test_load_checkpoint_metadata(self):
        from strands_agents.hooks.pipeline_hooks import RecoveryShell
        shell = Recovery