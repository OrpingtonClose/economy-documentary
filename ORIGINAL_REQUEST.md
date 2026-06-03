# Original User Request

## Initial Request — 2026-06-03T00:38:12Z

Implement a comprehensive Behavior-Driven Development (BDD) test suite to thoroughly cover edge cases, error conditions, concurrent locks, and endpoint intervention protocols in the documentary-production pipeline, strictly aligned with the principles documented in the Obsidian vault.

Working directory: /Users/orpington/Documents/economy-documentary-work
Integrity mode: development

## Requirements

### R1. Edge Case & VM Preemption Recovery BDD Tests
Implement tests validating that if worker VMs time out, fail to boot, or are preempted/terminated during active jobs:
- The Provisioner detects the failure and condemns the VM.
- The pipeline recovers its state correctly on replay/retry without double-allocating resources or leaking subprocesses.

### R2. Concurrent Endpoint & Lock Contention BDD Tests
Verify the concurrency invariants and LoopBoundLock serialization under high load:
- Concurrent wakeups or task submissions to active agents must be serialized or rejected cleanly (returning 409 Conflict if busy).
- GET health queries on `/` must run concurrently and return immediately without being blocked by the execution lock.

### R3. POST vs. PUT API Endpoint Intervention BDD Tests
Implement BDD tests verifying the explicit HTTP verb separation:
- A `POST /` request to a busy agent must return `409 Conflict`.
- A `PUT /` request must cancel the active asyncio task, instantly terminate any running bash subprocesses, and start a new turn.

### R4. Networking and macOS localhost Fix
Ensure all existing and new tests use `127.0.0.1` instead of `localhost` for all HTTP request destinations. On macOS, `localhost` resolves to IPv6 `[::1]`, which fails to connect to Uvicorn servers bound strictly to `127.0.0.1`.

## Acceptance Criteria

### Test Validation
- [ ] All edge case scenarios are written as Gherkin features in the `tests/units/features/` directory.
- [ ] The step definitions are implemented using `pytest-bdd` and integrated into the test files in `tests/units/`.
- [ ] The entire test suite executes and passes cleanly using the project's virtualenv python (`.venv/bin/pytest`).
- [ ] Subprocess tracking and cleanup are programmatically verified (e.g., verifying that cancelled/interrupted commands leave no orphan processes on the system).
- [ ] The existing tests are refactored to replace `localhost` with `127.0.0.1` so that they pass successfully on macOS.
