# Cross-run Resume for Documentary Pipeline

## Problem

After a pipeline run crashes, subsequent runs start from scratch, wasting compute and time. The pipeline should detect incomplete state from Backblaze B2 checkpoints, re-query Vast.ai for still-alive VMs, and resume from the last completed stage.

## Root Cause

The `resume` flag is hardcoded to `False` in `pipeline_runner.py`, and there is no logic to parse metadata.json, discover checkpoints from B2, reconcile active VMs from Vast.ai, or seed the OTIO timeline with previous progress.

## Fix

First, add a `resume` configuration option (default `False`) in `config.py` that when enabled triggers recovery logic. On startup, the pipeline runner will call a new function `discover_checkpoints` in `b2_manager.py` to list blobs under a run-specific prefix, identify the latest metadata.json, and load it. This metadata includes the last completed stage and VM IDs.

Second, implement VM reconciliation in `vast_ai_manager.py`: query Vast.ai for status of all VMs listed in metadata. VMs still 'running' are kept; dead or missing VMs trigger re-acquisition of similar instances (using previous VM specs) via `vast_ai_manager.py`. The pipeline then loads the last completed OTIO timeline checkpoint from B2 into `otio_handler.py`.

Third, in `pipeline_runner.py`, replace the hardcoded `resume=False` with a conditional: if `resume=True`, skip stages already completed (based on metadata) and start from the next stage. The state manager (`state_manager.py`) should be updated to persist stage completion status per run.

Finally, update the pipeline's orchestration loop to handle partial state, ensuring that reused VMs are properly configured and that timeline updates are additive. All changes should be tested with a simulated crash scenario.

## Files to Modify

- `config.py`
- `pipeline_runner.py`
- `vast_ai_manager.py`
- `b2_manager.py`
- `state_manager.py`
- `otio_handler.py`

## Estimated Effort

medium
