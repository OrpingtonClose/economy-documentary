# B2-backed Incremental Model Cache with Warmup for Vast.ai Workers

## Problem

Workers on Vast.ai VMs download full models (Qwen3, LTX) on every boot, causing long startup times and redundant downloads.

## Root Cause

The current download logic in worker agents (e.g., worker_tts.py, worker_video.py) has no caching mechanism; it always pulls from original sources without checksum verification.

## Fix

Implement a B2-backed model cache using Backblaze B2 to store model artifacts. On worker boot, check local cache; if missing, download incremental changes using b2 sync or list_versions. Verify checksums (sha256) of downloaded files against a registry. Add a warmup phase in the main worker loop that blocks job acceptance until all required models are verified and loaded. If a checksum mismatch occurs, fall back to a local cached copy if available; otherwise, trigger a full redownload from B2. The model registry should track versions and checksums, updated via a pipeline hook after successful training. This reduces startup time from minutes to seconds and eliminates redundant network transfers.

## Files to Modify

- `worker_tts.py`
- `worker_video.py`
- `b2_cache_client.py`
- `model_registry.py`
- `worker_main.py`

## Estimated Effort

medium
