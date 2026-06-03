# plan.md — BDD Test Suite & Concurrency Plan

## Milestones

| Milestone | Target | Description | Status |
|---|---|---|---|
| **M1** | Refactor Localhost | Replace `localhost` with `127.0.0.1` in all existing tests to fix macOS IPv6 resolution issues. | DONE |
| **M2** | Concurrency & Endpoint logic | Implement HTTP 409 Conflict on POST, task cancellation + subprocess group termination on PUT. | DONE |
| **M3** | Concurrency BDD Tests | Implement BDD features and step definitions verifying R2 & R3 requirements and subprocess tracking. | IN_PROGRESS |
| **M4** | VM Preemption BDD Tests | Implement BDD features and step definitions verifying R1 (preemption/condemn/recovery). | PLANNED |

## Detailed Technical Design

### M1: Refactoring Localhost
- Search for `localhost` inside `tests/` directory.
- Replace `localhost` with `127.0.0.1` in all HTTP requests to ensure uvicorn bound on `127.0.0.1` is reachable under macOS.

### M2: Concurrency & Intervention Endpoints in `server/agent_base.py`
- **POST handler:** If `active_task and not active_task.done()`, return `PlainTextResponse("Agent is busy", status_code=409)`.
- **PUT handler:** If `active_task and not active_task.done()`, cancel `active_task`. Wait for cancellation, then start a new turn.
- **GET handler:** Already does not block on `run_lock_manager` lock, ensuring concurrent queries. We must verify this.
- **Subprocess Group Termination:**
  Update `bash_command` in `server/agent_base.py` to use `preexec_fn=os.setsid` on process spawning. In the `asyncio.CancelledError` handler, kill the entire process group with `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` to terminate all spawned child processes.

### M3: Concurrency BDD Tests
- **Feature File:** `tests/units/features/concurrency_intervention.feature`
- **Step Definitions:** `tests/units/test_concurrency_intervention_bdd.py`
- **Scenarios to cover:**
  1. POST query returns 409 Conflict when the agent is busy.
  2. GET health queries can run concurrently without being blocked by the execution lock.
  3. PUT request cancels active turn, terminates any running bash subprocesses, and starts a new turn.
  4. Programmatic check that no orphan/zombie child processes are left in the process list after PUT cancellation.

### M4: VM Preemption BDD Tests
- **Feature File:** `tests/units/features/vm_preemption_recovery.feature`
- **Step Definitions:** `tests/units/test_vm_preemption_recovery_bdd.py`
- **Scenarios to cover:**
  1. Active job running on worker VM -> VM is preempted (simulated offline/not_found).
  2. Provisioner detects failure via status check and condemns the VM.
  3. Pipeline recovers its state cleanly on retry without double-allocating resources or leaking processes.

## Verification Criteria
- Run `.venv/bin/pytest` and verify that 100% of existing and new BDD tests pass.
- No orphan processes left on the system.
- Check code with `cheat_check.py` to ensure compliance.
