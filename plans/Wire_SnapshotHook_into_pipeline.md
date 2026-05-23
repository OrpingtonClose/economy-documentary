# Plan: Wire SnapshotHook into pipeline

# Implementation Plan: Wire SnapshotHook into Pipeline

## 1. Overview

The `SnapshotHook` class exists in `server/tracing/snapshot_hooks.py` but is not being used in the pipeline execution. We need to:
1. Import `SnapshotHook` in `run_strands.py`
2. Pass it as a hook provider to `build_documentary_graph()`
3. Ensure proper initialization with the run_id

## 2. Exact File Changes

### File: `server/strands_agents/run_strands.py`

#### Change 1: Add import for SnapshotHook (line ~40)

**Current code (lines 38-44):**
```python
from strands_agents.hooks.pipeline_hooks import (  # noqa: E402
    BudgetHook,
    ApprovalGateHook,
    ImmutabilityHook,
    ShellGuardHook,
)
```

**New code:**
```python
from strands_agents.hooks.pipeline_hooks import (  # noqa: E402
    BudgetHook,
    ApprovalGateHook,
    ImmutabilityHook,
    ShellGuardHook,
)
from tracing.snapshot_hooks import SnapshotHook  # noqa: E402
```

#### Change 2: Modify `build_documentary_graph()` call in `run_documentary()` function (line ~270)

**Current code (around line 270):**
```python
graph = build_documentary_graph(
    model=model,
    base_url=base_url,
    api_key=api_key,
    hooks=[
        BudgetHook(budget_limit=budget),
        ApprovalGateHook(approval_mode=approval_mode),
        ImmutabilityHook(),
        ShellGuardHook(),
    ],
    run_id=run_id,
    output_dir=output_dir,
)
```

**New code:**
```python
graph = build_documentary_graph(
    model=model,
    base_url=base_url,
    api_key=api_key,
    hooks=[
        BudgetHook(budget_limit=budget),
        ApprovalGateHook(approval_mode=approval_mode),
        ImmutabilityHook(),
        ShellGuardHook(),
        SnapshotHook(run_id=run_id),
    ],
    run_id=run_id,
    output_dir=output_dir,
)
```

## 3. Complete Modified Function (for context)

Here's the full `run_documentary()` function with the change applied (showing the relevant section):

```python
async def run_documentary(
    brief: str,
    model: str = _DEFAULT_MODEL,
    base_url: str = _DEFAULT_BASE_URL,
    api_key: str | None = None,
    budget: float = _DEFAULT_BUDGET,
    max_nodes: int = _DEFAULT_MAX_NODES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    approval_mode: str = _DEFAULT_APPROVAL,
    output_dir: str = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    """Run the full documentary pipeline."""
    # ... (existing code before graph construction) ...

    # Build the graph with hooks including SnapshotHook
    graph = build_documentary_graph(
        model=model,
        base_url=base_url,
        api_key=api_key,
        hooks=[
            BudgetHook(budget_limit=budget),
            ApprovalGateHook(approval_mode=approval_mode),
            ImmutabilityHook(),
            ShellGuardHook(),
            SnapshotHook(run_id=run_id),  # NEW: Wire in snapshot tracing
        ],
        run_id=run_id,
        output_dir=output_dir,
    )

    # ... (rest of existing function) ...
```

## 4. Dependencies and Side Effects

### Dependencies:
- `tracing.snapshot_hooks` must be importable (already on sys.path via the `_SERVER_DIR` setup at the top of `run_strands.py`)
- `tracing.snapshot_store` (imported by `snapshot_hooks.py`) must be available
- `SnapshotStore` must be properly initialized (uses `get_store()` singleton pattern)

### Side Effects:
1. **Performance impact**: Each lifecycle event (node start/end, tool call, model call) triggers synchronous file I/O to write snapshots. This could slow down pipeline execution, especially for long-running stages.
2. **Disk usage**: Snapshots accumulate in the store directory. For long runs with many tool calls, this could consume significant disk space.
3. **No failure impact**: If `SnapshotHook` initialization or any callback fails, it will raise an exception that could crash the pipeline. The current hook implementations don't have try/except blocks.

### Risk Mitigation:
The `SnapshotHook` class should be made more resilient. Consider adding error handling to `register_hooks` and each callback method. However, this is outside the scope of the current feature request.

## 5. Testing Approach

### Unit Tests:
1. **Import test**: Verify `SnapshotHook` can be imported from `tracing.snapshot_hooks`
2. **Instantiation test**: Verify `SnapshotHook(run_id="test-run-123")` creates an instance without errors
3. **Hook registration test**: Create a mock `HookRegistry` and verify all 6 callbacks are registered

### Integration Tests:
1. **Pipeline integration test**: Run a minimal pipeline with `SnapshotHook` and verify:
   - No exceptions during graph construction
   - Snapshot files are created in the store directory
   - Graph transitions are recorded
2. **Existing hook compatibility**: Verify `SnapshotHook` works alongside `BudgetHook`, `ApprovalGateHook`, `ImmutabilityHook`, and `ShellGuardHook`

### Manual Testing:
1. Run the pipeline with a simple brief and verify no errors
2. Check the snapshot store directory for created files
3. Verify the pipeline completes successfully

## 6. Rollback Strategy

### Immediate Rollback (if deployment fails):
1. Revert the import line (Change 1)
2. Remove `SnapshotHook(run_id=run_id)` from the hooks list (Change 2)

### Git-based Rollback:
```bash
git checkout -- server/strands_agents/run_strands.py
```

### Conditional Rollback (if issues arise in production):
Add a feature flag or environment variable to control SnapshotHook activation:

```python
# In run_strands.py, around the graph construction:
hooks = [
    BudgetHook(budget_limit=budget),
    ApprovalGateHook(approval_mode=approval_mode),
    ImmutabilityHook(),
    ShellGuardHook(),
]

# Only add SnapshotHook if explicitly enabled
if os.environ.get("ENABLE_SNAPSHOT_HOOK", "0") == "1":
    hooks.append(SnapshotHook(run_id=run_id))

graph = build_documentary_graph(
    model=model,
    base_url=base_url,
    api_key=api_key,
    hooks=hooks,
    run_id=run_id,
    output_dir=output_dir,
)
```

This allows disabling the hook without code changes by setting `ENABLE_SNAPSHOT_HOOK=0` in the environment.

## 7. Implementation Summary

| Change | File | Line(s) | Type |
|--------|------|---------|------|
| Add import | `run_strands.py` | ~40 (after existing hooks import) | Addition |
| Add to hooks list | `run_strands.py` | ~270 (in `build_documentary_graph()` call) | Addition |

**Total lines changed**: 2 lines added
**Risk level**: Low (adding a hook provider that follows the same pattern as existing hooks)
**Testing effort**: Low (basic import and integration test)