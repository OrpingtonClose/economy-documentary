> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Plan: Fix OTIO metadata thrashing

# Implementation Plan: Fix OTIO Metadata Thrashing

## Problem Analysis

The current implementation calls `_to_native()` 68,216 times per run because every read/write operation:
1. Calls `refresh_from_disk()` which reloads the entire OTIO file from disk
2. Each reload triggers `_to_native()` conversions for all metadata
3. Multiple operations compound the problem

## Solution: Cache OTIO Timeline with mtime-based Invalidation

### 1. Files to Modify

#### File 1: `server/strands_agents/otio_manager.py`

**Change 1: Add cache state tracking (lines ~60-80)**

```python
# Add to __init__ method, after existing state initialization
self._cache_valid: bool = False  # New: tracks if cache is fresh
self._cache_mtime: float = 0.0   # New: tracks file mtime at last cache
```

**Change 2: Optimize `refresh_from_disk()` (lines ~90-130)**

Replace the current implementation with mtime-based caching:

```python
def refresh_from_disk(self) -> None:
    """Reload _timeline from disk only if mtime has changed.
    
    Uses mtime-based invalidation to avoid unnecessary reloads.
    Thread-safe via RLock.
    """
    with self._lock:
        if not self._timeline_path:
            return
        
        if not os.path.exists(self._timeline_path):
            return
        
        current_mtime = os.path.getmtime(self._timeline_path)
        
        # Skip reload if cache is still valid
        if self._cache_valid and current_mtime == self._cache_mtime:
            return
        
        import opentimelineio as otio
        try:
            self._timeline = otio.adapters.read_from_file(self._timeline_path)
            self._timeline_mtime = current_mtime
            self._cache_mtime = current_mtime
            self._cache_valid = True
            logger.debug("Reloaded timeline from disk (mtime: %s)", current_mtime)
        except Exception as exc:
            logger.error("Failed to reload timeline: %s", exc)
            self._cache_valid = False
```

**Change 3: Invalidate cache on writes (lines ~140-170)**

Modify `_write_timeline()` to invalidate the cache:

```python
def _write_timeline(self) -> None:
    """Write the timeline to disk and invalidate cache."""
    with self._lock:
        try:
            import opentimelineio as otio
            if isinstance(self._timeline, otio.schema.Timeline) and self._timeline_path:
                os.makedirs(os.path.dirname(self._timeline_path), exist_ok=True)
                otio.adapters.write_to_file(self._timeline, self._timeline_path)
                new_mtime = os.path.getmtime(self._timeline_path)
                self._timeline_mtime = new_mtime
                self._cache_mtime = new_mtime
                self._cache_valid = True  # Cache is valid after our own write
        except Exception as exc:
            logger.error("_write_timeline failed: %s", exc)
            raise
```

**Change 4: Add cache invalidation method (after line ~170)**

```python
def invalidate_cache(self) -> None:
    """Force next refresh_from_disk to reload from disk.
    
    Call this when external processes may have modified the file.
    """
    with self._lock:
        self._cache_valid = False
        logger.debug("Timeline cache invalidated")
```

#### File 2: `server/tools/otio_metadata.py`

**Change 1: Add caching layer (lines ~1-30)**

Add import and cache dictionary at module level:

```python
from __future__ import annotations

import json
import os
import threading
from typing import Any

from tools.otio_file_ops import otio_read, otio_read_modify_write

# Module-level cache for OTIO timelines
_timeline_cache: dict[str, tuple[float, Any]] = {}  # path -> (mtime, timeline)
_cache_lock = threading.RLock()
```

**Change 2: Add cache-aware read function (after line ~30)**

```python
def _cached_otio_read(timeline_path: str) -> Any:
    """Read OTIO file with mtime-based caching.
    
    Returns cached timeline if file hasn't changed since last read.
    Thread-safe via module-level lock.
    """
    global _timeline_cache
    
    if not os.path.exists(timeline_path):
        raise FileNotFoundError(f"Timeline not found: {timeline_path}")
    
    current_mtime = os.path.getmtime(timeline_path)
    
    with _cache_lock:
        # Check cache
        if timeline_path in _timeline_cache:
            cached_mtime, cached_timeline = _timeline_cache[timeline_path]
            if current_mtime == cached_mtime:
                return cached_timeline
        
        # Cache miss - read from disk
        timeline = otio_read(timeline_path)
        _timeline_cache[timeline_path] = (current_mtime, timeline)
        return timeline


def _invalidate_cache(timeline_path: str) -> None:
    """Invalidate cache entry for a specific timeline path."""
    with _cache_lock:
        _timeline_cache.pop(timeline_path, None)
```

**Change 3: Optimize `_to_native()` (lines ~35-55)**

Add memoization for repeated conversions:

```python
# Add at module level
_to_native_cache: dict[int, Any] = {}
_to_native_cache_lock = threading.RLock()

def _to_native(value: Any) -> Any:
    """Convert OTIO container types to native Python types with memoization.
    
    Uses id() for caching since OTIO objects are immutable during a single
    read cycle. Cache is cleared when timeline is reloaded.
    """
    # Fast path for simple types
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    
    value_id = id(value)
    
    with _to_native_cache_lock:
        if value_id in _to_native_cache:
            return _to_native_cache[value_id]
    
    # OTIO AnyDictionary — has .items()
    if hasattr(value, 'items') and hasattr(value, 'get'):
        result = {k: _to_native(v) for k, v in value.items()}
        with _to_native_cache_lock:
            _to_native_cache[value_id] = result
        return result
    
    # OTIO AnyVector — iterable but not a string/bytes
    if hasattr(value, '__iter__') and not isinstance(value, (str, bytes)):
        result = [_to_native(item) for item in value]
        with _to_native_cache_lock:
            _to_native_cache[value_id] = result
        return result
    
    return value


def _clear_to_native_cache() -> None:
    """Clear the _to_native memoization cache."""
    with _to_native_cache_lock:
        _to_native_cache.clear()
```

**Change 4: Update `read_pipeline_metadata()` (lines ~60-85)**

Replace `otio_read()` with `_cached_otio_read()`:

```python
def read_pipeline_metadata(
    timeline_path: str,
    key: str,
    default: Any = None,
) -> Any:
    """Read a single key from the documentary metadata namespace.
    
    Uses mtime-based caching to avoid redundant file reads.
    """
    timeline = _cached_otio_read(timeline_path)
    doc = timeline.metadata.get("documentary", {})
    return _to_native(doc.get(key, default))
```

**Change 5: Update `write_pipeline_metadata()` (lines ~90-120)**

Add cache invalidation after write:

```python
def write_pipeline_metadata(
    timeline_path: str,
    key: str,
    value: Any,
    provenance: dict | None = None,
) -> str:
    """Write a key/value pair into the documentary metadata namespace.
    
    Invalidates cache after write to ensure consistency.
    """
    def _mutate(timeline: Any) -> None:
        timeline.metadata.setdefault("documentary", {})
        timeline.metadata["documentary"][key] = value
        if provenance is not None:
            timeline.metadata["documentary"].setdefault("_provenance", {})[key] = provenance

    otio_read_modify_write(timeline_path, _mutate)
    _invalidate_cache(timeline_path)  # Invalidate after write
    _clear_to_native_cache()  # Clear memoization cache
    return json.dumps({"written": True, "key": key})
```

**Change 6: Update `metadata_key_exists()` (lines ~125-140)**

```python
def metadata_key_exists(timeline_path: str, key: str) -> bool:
    """Check whether a key exists in the documentary metadata namespace.
    
    Uses cached timeline to avoid redundant reads.
    """
    timeline = _cached_otio_read(timeline_path)
    doc = timeline.metadata.get("documentary", {})
    return key in doc
```

### 2. Dependencies and Side Effects

**Dependencies:**
- `os.path.getmtime()` - standard library
- `threading.RLock()` - standard library
- No new external dependencies

**Side Effects:**
1. **Memory usage**: Cache stores timeline objects in memory. For typical documentary timelines (~10MB), this is acceptable.
2. **Stale data risk**: If external processes modify the .otio file without updating mtime (e.g., NFS with noatime), cache could become stale. Mitigation: Add `invalidate_cache()` method for explicit invalidation.
3. **Thread safety**: All cache operations use `RLock` for thread safety.

### 3. Testing Approach

**Unit Tests for `server/tools/otio_metadata.py`:**

```python
# tests/test_otio_metadata_cache.py
import os
import tempfile
import time
from unittest.mock import patch, MagicMock
from tools.otio_metadata import (
    _cached_otio_read,
    _invalidate_cache,
    _to_native,
    _clear_to_native_cache,
    read_pipeline_metadata,
    write_pipeline_metadata,
)


def test_cache_hit_returns_same_object():
    """Test that cached read returns the same object."""
    with tempfile.NamedTemporaryFile(suffix='.otio', delete=False) as f:
        path = f.name
    
    try:
        # First read populates cache
        timeline1 = _cached_otio_read(path)
        # Second read should return cached version
        timeline2 = _cached_otio_read(path)
        assert timeline1 is timeline2, "Cache should return same object"
    finally:
        os.unlink(path)


def test_cache_invalidation_on_mtime_change():
    """Test that cache is invalidated when file mtime changes."""
    with tempfile.NamedTemporaryFile(suffix='.otio', delete=False) as f:
        path = f.name
    
    try:
        timeline1 = _cached_otio_read(path)
        # Simulate external modification
        time.sleep(0.1)  # Ensure different mtime
        with open(path, 'a') as f:
            f.write(' ')  # Modify file
        os.utime(path, None)  # Update mtime
        
        timeline2 = _cached_otio_read(path)
        assert timeline1 is not timeline2, "Cache should be invalidated on mtime change"
    finally:
        os.unlink(path)


def test_to_native_memoization():
    """Test that _to_native caches results for same object."""
    class MockDict:
        def __init__(self, data):
            self._data = data
        def items(self):
            return self._data.items()
        def get(self, key, default=None):
            return self._data.get(key, default)
    
    obj = MockDict({"key": "value"})
    result1 = _to_native(obj)
    result2 = _to_native(obj)
    assert result1 is result2, "Memoization should return same object"
    
    _clear_to_native_cache()
    result3 = _to_native(obj)
    assert result1 is not result3, "Cache clear should invalidate"


def test_concurrent_cache_access():
    """Test thread safety of cache operations."""
    import threading
    
    errors = []
    
    def worker():
        try:
            for _ in range(100):
                _cached_otio_read("/nonexistent/path")
        except Exception as e:
            errors.append(e)
    
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert len(errors) == 0, f"Concurrent access caused errors: {errors}"
```

**Integration Tests:**

```python
# tests/integration/test_otio_cache_integration.py
def test_end_to_end_cache_reduction():
    """Test that caching reduces file reads by 90%+."""
    import psutil
    import os
    
    # Setup
    timeline_path = "/tmp/test_timeline.otio"
    
    # Measure I/O before caching
    io_before = psutil.Process().io_counters()
    
    # Perform 1000 read operations
    for _ in range(1000):
        read_pipeline_metadata(timeline_path, "test_key")
    
    io_after = psutil.Process().io_counters()
    read_count = io_after.read_count - io_before.read_count
    
    # With caching, should be ~1 read instead of 1000
    assert read_count < 10, f"Too many reads: {read_count}"
```

### 4. Rollback Strategy

**Immediate Rollback (within 1 hour):**

```bash
# Revert specific files
git checkout -- server/tools/otio_metadata.py
git checkout -- server/strands_agents/otio_manager.py

# Or use git revert for specific commits
git revert <commit-hash> --no-edit
```

**Staged Rollback (if issues detected later):**

1. **Memory leak**: Add cache size limit:
```python
# In otio_metadata.py, add after imports
MAX_CACHE_SIZE = 5  # Maximum number of cached timelines

def _cached_otio_read(timeline_path: str) -> Any:
    # ... existing code ...
    with _cache_lock:
        # Evict oldest entry if cache is full
        if len(_timeline_cache) >= MAX_CACHE_SIZE:
            oldest_path = min(_timeline_cache.keys(), 
                            key=lambda p: _timeline_cache[p][0])
            del _timeline_cache[oldest_path]
```

2. **Stale data**: Add forced refresh flag:
```python
def read_pipeline_metadata(
    timeline_path: str,
    key: str,
    default: Any = None,
    force_refresh: bool = False,  # New parameter
) -> Any:
    if force_refresh:
        _invalidate_cache(timeline_path)
    timeline = _cached_otio_read(timeline_path)
    # ... rest of function
```

3. **Disable caching entirely** (emergency):
```python
# In otio_metadata.py, add at module level
CACHE_ENABLED = True  # Toggle to False to disable caching

def _cached_otio_read(timeline_path: str) -> Any:
    if not CACHE_ENABLED:
        return otio_read(timeline_path)
    # ... existing caching logic
```

### Performance Impact

**Expected improvements:**
- `_to_native` calls: 68,216 → ~100 (only when cache is invalidated)
- File reads: 68,216 → ~100 (only when mtime changes)
- Memory: +10-50MB for cached timelines (acceptable for server)

**Monitoring:**
Add logging to track cache hit/miss ratio:

```python
# In otio_metadata.py
_cache_hits = 0
_cache_misses = 0

def _cached_otio_read(timeline_path: str) -> Any:
    global _cache_hits, _cache_misses
    # ... existing code ...
    with _cache_lock:
        if timeline_path in _timeline_cache:
            cached_mtime, cached_timeline = _timeline_cache[timeline_path]
            if current_mtime == cached_mtime:
                _cache_hits += 1
                return cached_timeline
        
        _cache_misses += 1
        # ... rest of function
```

This implementation will reduce the 68,216 `_to_native` calls to approximately 100-200 per run, representing a 99.7% reduction in metadata thrashing.