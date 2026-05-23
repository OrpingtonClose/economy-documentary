# Resilient LLM Retry Logic with Exponential Backoff, Jitter, and Fallback

## Problem

DeepSeek API connection drops cause pipeline crashes mid-stream, no retry mechanism.

## Root Cause

RecoveryShell has resume=False, no retry on ConnectionError; no exponential backoff or fallback.

## Fix

Implement a robust retry layer using the tenacity library. Add exponential backoff with jitter to avoid thundering herd. Detect transient errors (ConnectionError, TimeoutError, HTTP 5xx) and retry up to 3 times. Before each retry, checkpoint the current state to allow safe resume. After max retries, gracefully degrade to a fallback LLM model (e.g., GPT-3.5-turbo) and log a warning. Modify RecoveryShell to accept a retry policy and enable resume=True for checkpoint-based recovery. Integrate the retry logic into the Strands pipeline's LLM agent call, ensuring all calls through the Strands framework benefit from resilience.

## Files to Modify

- `pipeline.py`
- `agents/llm_agent.py`
- `utils/retry.py`
- `utils/recovery.py`

## Estimated Effort

medium
