# OTIO Metadata Caching with Dirty-Bit Tracking

## Problem

OTIOStateManager.refresh_from_disk() reloads the entire timeline from disk before every operation, causing 68,216 _to_native calls per run. This makes pipeline operations extremely slow and disk-bound.

## Root Cause

The current design in OTIOStateManager (likely otio_state_manager.py) calls refresh_from_disk() unconditionally before each operation, re-parsing the entire OTIO file. Functions _clip_counts and _record_transition also recompute values from scratch each time.

## Fix

Implement an in-memory cache for the OTIO timeline object. Maintain a dirty bit that is set when any mutation occurs. On read operations, return cached data if not dirty; only reload from disk when dirty or on explicit refresh requests. Batch write operations to disk only when a batch is complete or after a configurable interval. Use lazy serialization: convert to native OTIO format only when necessary for persistence. Additionally, cache the results of expensive derived computations like _clip_counts and _record_transition, invalidating them when the underlying timeline changes. This will reduce _to_native calls to only those needed for actual disk writes.

## Files to Modify

- `src/pipeline/otio_state_manager.py`
- `src/pipeline/timeline_operations.py`

## Estimated Effort

medium
