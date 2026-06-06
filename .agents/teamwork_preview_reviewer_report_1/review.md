# Codebase Compliance Audit Review Report

## Review Summary

**Verdict**: REQUEST_CHANGES

This review evaluates the compliance audit report located at `/Users/orpington/Documents/economy-documentary-work/codebase_compliance_report.md` against the V7.1 technical specifications in `obsidian-vault/` and active codebase files under `server/`, `pipeline/`, `scripts/`, and `tests/`.

The audited report is of exceptionally high quality, detail, and precision. It accurately identifies critical architectural deviations in the REST endpoint protocols (lock serialization deadlocks), long-term agent memory management (event store bypass), and conversational boundary constraints. 

However, the report is not yet complete due to a coverage gap (missing shell scripts) and a minor compliance format gap (missing code snippets for certain reported discrepancies). Therefore, the verdict is **REQUEST_CHANGES** (FAILing the acceptance criteria for full coverage).

---

## Quality Review Findings

### [Critical] Finding 1: Incomplete File Coverage (Shell Scripts Omitted)
- **What**: Several shell scripts present in the `server/` and `scripts/` directories were completely omitted from the compliance audit report mapping.
- **Where**: `codebase_compliance_report.md` Section 2.1 and Section 2.3.
- **Why**: This violates user follow-up Requirement **R1** ("Verify every python file and shell script under `server/`, `pipeline/`, `scripts/`, and `tests/` against the technical invariants...") and the audit coverage acceptance criteria ("Every active file in `server/`, `pipeline/`, `scripts/`, and `tests/` has been checked against the obsidian-vault specs.").
- **Suggestion**: Update the audit mapping tables under Sections 2.1 and 2.3 to include these missing shell scripts, mapping them to their corresponding Obsidian Vault documents:
  - `server/run` -> `04 - Agent Architecture and Systems.md` (Status: **Compliant**)
  - `scripts/central_bootstrap.sh` -> `01 - Philosophy and Topology.md` (Status: **Compliant**)
  - `scripts/gpu_bootstrap.sh` -> `05 - Provisioning and GPU Infrastructure.md` (Status: **Compliant**)
  - `scripts/langfuse_bootstrap.sh` -> `07 - Security, Traceability, and Auditing.md` (Status: **Compliant**)
  - `scripts/ltx_video_worker_bootstrap.sh` -> `05 - Provisioning and GPU Infrastructure.md` (Status: **Compliant**)
  - `scripts/playground_staging_bootstrap.sh` -> `05 - Provisioning and GPU Infrastructure.md` (Status: **Compliant**)
  - `scripts/qwen3_tts_worker_bootstrap.sh` -> `05 - Provisioning and GPU Infrastructure.md` (Status: **Compliant**)
  - `scripts/vm_onstart_ltx.sh` -> `05 - Provisioning and GPU Infrastructure.md` (Status: **Compliant**)
  - `scripts/vm_onstart_tts.sh` -> `05 - Provisioning and GPU Infrastructure.md` (Status: **Compliant**)

### [Minor] Finding 2: Missing Individual Code Snippets for macOS localhost and Static Violations
- **What**: Individual code snippets are missing for reported discrepancies under Section 7.1 (macOS localhost violations) and Section 7.2 (static validation summary).
- **Where**: `codebase_compliance_report.md` Section 7.1 and Section 7.2.
- **Why**: The discrepancy reporting acceptance criteria states: "Each reported discrepancy contains the file path, line number, code snippet, and the specific obsidian-vault section it violates." Section 7.1 lists paths and line numbers but lacks representative snippets, and Section 7.2 only summarizes counts.
- **Suggestion**: Add representative code snippets or template patterns (e.g., `httpx.get("http://localhost:8000/", timeout=1.0)`) to the report.

### [Minor] Finding 3: Missing Specific Obsidian Vault Section/Document Citations on Individual Workers
- **What**: Discrepancy details for legacy worker scripts (`gpu_worker.py` and `tts_worker.py`) under Section 4.2 do not explicitly cite the spec files they violate, but instead rely on the section intro.
- **Where**: `codebase_compliance_report.md` Section 4.2.
- **Why**: Acceptance criteria states: "Each reported discrepancy contains the file path, line number, code snippet, and the specific obsidian-vault section it violates."
- **Suggestion**: Explicitly repeat the violated spec section for each individual discrepancy item under Section 4.2 (e.g., `05 - Provisioning and GPU Infrastructure.md` and `01 - Philosophy and Topology.md` Core Philosophies).

---

## Verified Claims

- **GET health queries on `/` block on lock** -> Verified via inspection of `server/agent_base.py` lines 940-942 -> **Pass**
- **POST requests block on lock instead of returning 409** -> Verified via inspection of `server/agent_base.py` line 977 -> **Pass**
- **BDD test suite has a scenario asserting incorrect blocking behavior** -> Verified via inspection of `tests/units/features/concurrency_intervention.feature` and `tests/units/test_concurrency_intervention_bdd.py` -> **Pass**
- **`scripts/gpu_worker.py` and `scripts/tts_worker.py` emit structured strings violating conversational natural language** -> Verified via inspection of `scripts/gpu_worker.py` lines 161-165 and `scripts/tts_worker.py` lines 78-81 -> **Pass**
- **Undocumented subclass definitions in `server/effects.py`** -> Verified via inspection of `server/effects.py` lines 120-230 -> **Pass**
- **SQLite event store creates undocumented `agent_memories` table** -> Verified via inspection of `server/event_store.py` lines 46-51 -> **Pass**
- **`noop` events are intercepted and blocked at `event_store.py` append boundary** -> Verified via inspection of `server/event_store.py` lines 71-76 -> **Pass**

---

## Coverage Gaps

- **Shell scripts coverage gap** — risk level: **High** — recommendation: investigate and update the compliance report to include the 9 missing shell scripts as detailed in Finding 1.

---

## Unverified Items

- **Large-scale integration tests execution** — reason not verified: the tests require external LLM calls (OpenAI/DeepSeek API endpoints) which are blocked/fail in the local offline test execution environment due to lack of API keys.

---

## Adversarial Review & Challenge Report

**Overall risk assessment**: MEDIUM

### [Medium] Challenge 1: Local Test Execution Dependency on Live LLM Call
- **Assumption challenged**: The test file `tests/units/test_agent_search_tools.py` assumes that invoking the scenario agent is testable in a unit-test sandbox.
- **Attack scenario**: If the internet connection is lost, API keys are missing, or LLM endpoints are unavailable (as is common in sandboxed environments or offline testing), this test hangs indefinitely or fails.
- **Blast radius**: Halts CI/CD pipelines and local developer testing loops.
- **Mitigation**: Replace the real LLM call in `test_agent_search_tool_execution` with a structured mock of the DeepSeek model completion using Uvicorn-level mocks or `unittest.mock.patch` of `llm_complete`.

### [Low] Challenge 2: Synchronous Blocking of Event Loop via `subprocess.run`
- **Assumption challenged**: The PUT endpoint assumes it can abort active agent turns.
- **Attack scenario**: If the Assembly agent is running a heavy synchronous task like `ffmpeg` via `subprocess.run`, the main event loop thread blocks entirely. The FastAPI server cannot receive or execute a PUT cancel request.
- **Blast radius**: Stalls the agent server and prevents instant intervention when a pipeline goes runaway.
- **Mitigation**: Wrap `subprocess.run` invocations in an executor via `asyncio.to_thread` or use `asyncio.create_subprocess_exec` to make subprocess execution non-blocking.
