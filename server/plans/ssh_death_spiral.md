# SSH Death Spiral Mitigation via Multiplexed Connections and Cached Health Checks

## Problem

Every tool call to a VM worker initiates a new SSH connection, causing high latency and resource exhaustion under load.

## Root Cause

Lack of SSH connection reuse and health check caching in the Vast.ai worker module. The current implementation (in vast_worker.py or similar) creates a new SSH client per call and performs health checks without caching.

## Fix

Implement SSH ControlMaster persistence to allow multiplexing over a single TCP connection, reducing overhead. Add a health check cache with TTL so that repeated calls do not re-check worker readiness. Switch worker readiness detection from arbitrary commands to a plain text GET / endpoint for faster, stateless validation. Introduce a connection pool for SSH connections to limit concurrent connections and reuse idle ones. Configuration for ControlMaster paths, cache TTL, and pool size will be added to the application's settings.

## Files to Modify

- `vast_worker.py`
- `ssh_manager.py`
- `health_check.py`
- `config/settings.py`

## Estimated Effort

medium
