> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Plan: Add resilient LLM retry with backoff

# Implementation Plan: Resilient LLM Retry with Exponential Backoff

## Overview
Add exponential backoff retry logic to LLM API calls in the strands pipeline to handle transient DeepSeek API connection drops gracefully.

## 1. Files to Modify

### 1.1 Create New File: `server/strands_agents/retry_utils.py`

This file will contain the retry decorator and utility functions.

```python
"""
Retry utilities for LLM API calls with exponential backoff.

Provides a decorator and async wrapper for retrying operations that may
fail due to transient network issues, rate limits, or server errors.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

# Type variable for the return type of the wrapped function
T = TypeVar("T")

# Default retry configuration
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BASE_DELAY: float = 1.0
DEFAULT_MAX_DELAY: float = 30.0
DEFAULT_BACKOFF_FACTOR: float = 2.0
DEFAULT_JITTER: float = 0.1  # 10% jitter

# Status codes that should trigger a retry
RETRYABLE_STATUS_CODES: set[int] = {
    408,  # Request Timeout
    429,  # Too Many Requests
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
}

# Exception types that should trigger a retry
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    asyncio.TimeoutError,
    OSError,
)


def is_retryable_error(error: Exception) -> bool:
    """Check if an exception should trigger a retry."""
    if isinstance(error, RETRYABLE_EXCEPTIONS):
        return True
    
    # Check for common API client errors
    error_str = str(error).lower()
    retryable_keywords = [
        "timeout",
        "connection",
        "reset",
        "refused",
        "unavailable",
        "rate limit",
        "too many requests",
        "server error",
        "internal error",
        "bad gateway",
        "gateway timeout",
        "service unavailable",
    ]
    return any(keyword in error_str for keyword in retryable_keywords)


def calculate_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    jitter: float = DEFAULT_JITTER,
) -> float:
    """Calculate delay with exponential backoff and jitter.
    
    Args:
        attempt: Current attempt number (0-indexed)
        base_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_factor: Multiplier for each retry
        jitter: Random jitter as fraction of delay (0.0 to 1.0)
    
    Returns:
        Delay in seconds
    """
    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
    jitter_amount = delay * jitter * random.random()
    return delay + jitter_amount


def retry_with_backoff(
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    jitter: float = DEFAULT_JITTER,
    retryable_exceptions: Optional[tuple[type[Exception], ...]] = None,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for retrying async functions with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_factor: Multiplier for each retry
        jitter: Random jitter as fraction of delay
        retryable_exceptions: Tuple of exception types to retry on
        on_retry: Callback function(attempt, exception, delay) called before each retry
    
    Returns:
        Decorated async function
    """
    if retryable_exceptions is None:
        retryable_exceptions = RETRYABLE_EXCEPTIONS

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Optional[Exception] = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    
                    # Check if this is the last attempt
                    if attempt >= max_retries:
                        logger.error(
                            "All %d retry attempts exhausted for %s: %s",
                            max_retries,
                            func.__name__,
                            str(e),
                        )
                        raise
                    
                    # Check if exception is retryable
                    if not is_retryable_error(e):
                        logger.warning(
                            "Non-retryable exception in %s (attempt %d/%d): %s",
                            func.__name__,
                            attempt + 1,
                            max_retries + 1,
                            str(e),
                        )
                        raise
                    
                    # Calculate delay
                    delay = calculate_delay(
                        attempt=attempt,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        backoff_factor=backoff_factor,
                        jitter=jitter,
                    )
                    
                    # Log retry
                    logger.warning(
                        "Retry %d/%d for %s after %.2fs: %s",
                        attempt + 1,
                        max_retries,
                        func.__name__,
                        delay,
                        str(e),
                    )
                    
                    # Call on_retry callback if provided
                    if on_retry:
                        on_retry(attempt + 1, e, delay)
                    
                    # Wait before retry
                    await asyncio.sleep(delay)
            
            # This should never be reached, but just in case
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected error in retry logic")
        
        return wrapper
    
    return decorator


# Convenience function for wrapping stream_async calls
async def stream_with_retry(
    stream_func: Callable[..., Any],
    *args: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    **kwargs: Any,
) -> Any:
    """Wrap a streaming async function with retry logic.
    
    This is specifically designed for Agent.stream_async() calls.
    It will retry the entire stream if it fails before completion.
    
    Args:
        stream_func: The async streaming function to call
        *args: Positional arguments to pass to stream_func
        max_retries: Maximum number of retry attempts
        **kwargs: Keyword arguments to pass to stream_func
    
    Returns:
        The result of the stream function
    """
    last_exception: Optional[Exception] = None
    
    for attempt in range(max_retries + 1):
        try:
            return await stream_func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            
            if attempt >= max_retries:
                logger.error(
                    "All %d retry attempts exhausted for stream: %s",
                    max_retries,
                    str(e),
                )
                raise
            
            if not is_retryable_error(e):
                raise
            
            delay = calculate_delay(attempt=attempt)
            
            logger.warning(
                "Stream retry %d/%d after %.2fs: %s",
                attempt + 1,
                max_retries,
                delay,
                str(e),
            )
            
            await asyncio.sleep(delay)
    
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected error in stream retry logic")
```

### 1.2 Modify `server/strands_agents/graph_pipeline.py`

Add retry logic to the agent execution in the graph pipeline. The key change is in the `_execute_agent_node` function or wherever agents are called.

**Line numbers to modify (approximate):**
- After imports (around line 10): Add import for retry utilities
- Around line 200-250: Modify agent execution to use retry

```python
# Add after existing imports (around line 10)
from strands_agents.retry_utils import stream_with_retry, retry_with_backoff

# Modify the agent execution function (find where stream_async is called)
# This is likely in a function that executes agent nodes in the graph

# Example modification - find the actual function that calls stream_async
# and wrap it with retry logic

# If there's a function like _execute_agent or similar:
async def _execute_agent_with_retry(
    agent: Agent,
    prompt: str,
    context: dict[str, Any],
    max_retries: int = 3,
) -> Any:
    """Execute an agent with retry logic for API failures."""
    try:
        # Wrap the stream_async call with retry
        result = await stream_with_retry(
            agent.stream_async,
            prompt,
            context=context,
            max_retries=max_retries,
        )
        return result
    except Exception as e:
        logger.error("Agent execution failed after retries: %s", str(e))
        raise
```

### 1.3 Modify `server/strands_agents/run_strands.py`

Add retry configuration and integrate with the pipeline execution.

**Line numbers to modify (approximate):**
- Around line 30-40: Add retry configuration constants
- Around line 100-150: Modify `run_documentary` function to use retry

```python
# Add after existing configuration constants (around line 30)
# =============================================================================
# RETRY CONFIGURATION
# =============================================================================
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 1.0  # seconds
_DEFAULT_MAX_DELAY = 30.0  # seconds
_DEFAULT_BACKOFF_FACTOR = 2.0
_DEFAULT_JITTER = 0.1  # 10% jitter
# =============================================================================

# Modify the run_documentary function to pass retry configuration
# Find the function definition (around line 100-150)

# Add retry configuration to the function signature
async def run_documentary(
    brief: str,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    jitter: float = _DEFAULT_JITTER,
) -> dict[str, Any]:
    """Run the documentary pipeline with retry logic."""
    
    # Pass retry configuration to the graph execution
    # This depends on how the graph is executed - look for where agents are called
    
    # Example: If there's a graph.run() or similar call, pass retry config
    # result = await graph.run(
    #     brief=brief,
    #     max_retries=max_retries,
    #     base_delay=base_delay,
    #     ...
    # )
```

## 2. Dependencies and Side Effects

### Dependencies:
- **No new external dependencies** - uses only Python standard library (`asyncio`, `functools`, `logging`, `random`, `time`)
- Depends on existing `strands` library's `Agent.stream_async()` method

### Side Effects:
1. **Increased latency**: Failed API calls will now wait before retrying, potentially increasing total pipeline time
2. **More log output**: Each retry attempt generates warning logs
3. **Potential for duplicate work**: If the API call succeeds but the response is lost, the retry might cause duplicate operations
4. **Memory usage**: Slightly increased due to retry state tracking

### Risk Mitigation:
- Maximum retry count limits the total time spent retrying
- Exponential backoff prevents overwhelming the API
- Jitter prevents thundering herd problems
- Non-retryable exceptions are immediately re-raised

## 3. Testing Approach

### Unit Tests (create `tests/test_retry_utils.py`):

```python
"""Tests for retry utilities."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from strands_agents.retry_utils import (
    calculate_delay,
    is_retryable_error,
    retry_with_backoff,
    stream_with_retry,
    RETRYABLE_EXCEPTIONS,
)


class TestCalculateDelay:
    """Tests for delay calculation."""
    
    def test_basic_backoff(self):
        """Test that delay increases with attempts."""
        delays = [calculate_delay(i, jitter=0.0) for i in range(5)]
        for i in range(1, len(delays)):
            assert delays[i] > delays[i-1], f"Delay should increase: {delays}"
    
    def test_max_delay_cap(self):
        """Test that delay doesn't exceed max_delay."""
        delay = calculate_delay(100, base_delay=1.0, max_delay=30.0, jitter=0.0)
        assert delay <= 30.0, f"Delay should be capped at 30.0: {delay}"
    
    def test_jitter_range(self):
        """Test that jitter is within expected range."""
        delays = [calculate_delay(0, jitter=0.1) for _ in range(100)]
        for delay in delays:
            assert 0.9 <= delay <= 1.1, f"Delay with jitter out of range: {delay}"


class TestIsRetryableError:
    """Tests for retryable error detection."""
    
    def test_connection_error(self):
        """Test that ConnectionError is retryable."""
        assert is_retryable_error(ConnectionError("connection refused"))
    
    def test_timeout_error(self):
        """Test that TimeoutError is retryable."""
        assert is_retryable_error(TimeoutError("timed out"))
    
    def test_value_error(self):
        """Test that ValueError is not retryable."""
        assert not is_retryable_error(ValueError("invalid value"))
    
    def test_retryable_keyword(self):
        """Test that errors with retryable keywords are detected."""
        assert is_retryable_error(Exception("rate limit exceeded"))
        assert is_retryable_error(Exception("server error 500"))
        assert not is_retryable_error(Exception("invalid input"))


class TestRetryWithBackoff:
    """Tests for retry decorator."""
    
    @pytest.mark.asyncio
    async def test_successful_call(self):
        """Test that successful calls don't retry."""
        mock_func = AsyncMock(return_value="success")
        decorated = retry_with_backoff(max_retries=3)(mock_func)
        result = await decorated()
        assert result == "success"
        mock_func.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Test that retryable failures trigger retries."""
        mock_func = AsyncMock(side_effect=[ConnectionError("fail"), "success"])
        decorated = retry_with_backoff(max_retries=3)(mock_func)
        result = await decorated()
        assert result == "success"
        assert mock_func.call_count == 2
    
    @pytest.mark.asyncio
    async def test_exhaust_retries(self):
        """Test that exhausting retries raises the last exception."""
        mock_func = AsyncMock(side_effect=ConnectionError("persistent failure"))
        decorated = retry_with_backoff(max_retries=2)(mock_func)
        with pytest.raises(ConnectionError, match="persistent failure"):
            await decorated()
        assert mock_func.call_count == 3  # original + 2 retries
    
    @pytest.mark.asyncio
    async def test_non_retryable_exception(self):
        """Test that non-retryable exceptions are not retried."""
        mock_func = AsyncMock(side_effect=ValueError("non-retryable"))
        decorated = retry_with_backoff(max_retries=3)(mock_func)
        with pytest.raises(ValueError, match="non-retryable"):
            await decorated()
        mock_func.assert_called_once()


class TestStreamWithRetry:
    """Tests for stream_with_retry function."""
    
    @pytest.mark.asyncio
    async def test_successful_stream(self):
        """Test that successful streams don't retry."""
        mock_stream = AsyncMock(return_value="stream result")
        result = await stream_with_retry(mock_stream, "arg1", key="value")
        assert result == "stream result"
        mock_stream.assert_called_once_with("arg1", key="value")
    
    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Test that retryable failures trigger retries."""
        mock_stream = AsyncMock(side_effect=[ConnectionError("fail"), "success"])
        result = await stream_with_retry(mock_stream, "test", max_retries=2)
        assert result == "success"
        assert mock_stream.call_count == 2
    
    @pytest.mark.asyncio
    async def test_exhaust_retries(self):
        """Test that exhausting retries raises the last exception."""
        mock_stream = AsyncMock(s