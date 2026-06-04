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

## Follow-up — 2026-06-04T04:34:22Z

Perform a comprehensive, paragraph-to-paragraph compliance check of the entire codebase (including all core server files, script files, and test files) against the canonical V7.1 technical specifications located in the `obsidian-vault/` directory.

Working directory: /Users/orpington/Documents/economy-documentary-work
Integrity mode: development

## Requirements

### R1. Comprehensive Source Code Mapping
Verify every python file and shell script under `server/`, `pipeline/`, `scripts/`, and `tests/` against the technical invariants, processes, and guidelines defined in the corresponding markdown files in `obsidian-vault/`.

### R2. Verification of REST Endpoint Control Protocols
Verify that every HTTP endpoint handler on the root path `/` complies with the documented GET (status check), POST (light commands), and PUT (electric bolt cancellation intervention) protocols:
- `GET /` and `POST /` must serialize execution using the loop-bound locks, performing no heavy inline processing.
- `PUT /` must immediately cancel running execution tasks and launch the new payload in the background, returning `204 No Content`.
- Verify there are no disallowed sub-endpoints (e.g. `/health`, `/status`).

### R3. Natural Language Invariant Check (No Structured Formats)
Confirm that the VM worker agent and GSA communicate using only plain conversational natural language and that no structured key-value status payloads (like `ltx=yes`, `tts=yes` or `tts_loaded: true`) are generated, processed, or expected by any active code.

### R4. Event Store & Schema Alignment
Verify that the `Effect` model classes in `server/effects.py` and the SQL queries in `server/event_store.py` map exactly to the schema models defined in `02 - Event Store and Effect Schemas.md` (no undocumented subclasses or structural mismatch).

### R5. Complete NoOp Elimination Check
Verify that `noop` events are completely blocked from entering the database at the EventStore append boundary.

### R6. Audit Report Generation
Produce a clear and structured audit report summarizing the compliance status of each module, highlighting any discrepancy, and citing the exact file names, line numbers, and the document section violated.

## Acceptance Criteria

### Audit Coverage
- [ ] Every active file in `server/`, `pipeline/`, `scripts/`, and `tests/` has been checked against the obsidian-vault specs.

### Discrepancy Reporting
- [ ] Discrepancies are reported in `codebase_compliance_report.md` in the working directory.
- [ ] Each reported discrepancy contains the file path, line number, code snippet, and the specific obsidian-vault section it violates.

### Compliance Validation
- [ ] If a module is fully compliant, it is marked as such in the report.
