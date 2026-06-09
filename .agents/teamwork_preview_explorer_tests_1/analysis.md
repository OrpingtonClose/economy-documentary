# 🧪 Test Suite Compliance Analysis Report

This report presents a comprehensive paragraph-to-paragraph compliance check of all Python files under `tests/` against the technical specifications in the `obsidian-vault/` directory.

---

## 1. Summary of Core Findings

1. **Local Execution IP Binding (`localhost` vs `127.0.0.1`)**: There are **10 test files** containing hardcoded `localhost` references. Under macOS, this violates the local execution guidelines due to IPv6 loopback routing delay issues.
2. **HTTP Endpoint Concurrency Constraints (`POST /` conflict)**: The system specification requires `POST /` to immediately return `409 Conflict` (agent busy) if a heavy turn is already running. However:
   - The server implementation blocks the request via `async with lock:` instead of returning `409`.
   - The BDD test suite checks for this *incorrect blocking behavior* and passes.
   - The unit test suite checks for the *correct 409 Conflict behavior* and fails.
3. **Timeout Policy Violations**: Five test files contain `timeout=` HTTP parameters without the mandatory `# health probe` (or similar) comment. This causes them to fail the compliance checker `cheat_check.py`.
4. **GET / Health Status Logical Deadlock**: The agent health check endpoint executes inside the turn-serialization lock. Because of this, it is impossible for a client to ever observe the state `"busy"`, as the request blocks until the turn finishes.
5. **Coding Standards Violations (`cheat_check.py` scan)**: There are **118 violations** under the `tests/` directory (swallowed exceptions, fixed loops) and **34 violations** under the `server/` directory.

---

## 2. Detailed Discrepancy & Violation Log

### 2.1 Local Execution IP Binding Violations (`localhost` vs `127.0.0.1`)

* **Specification**: Local execution guidelines on macOS (specified in project rules and implied in `08 - Testing, Concurrency, and Rollout.md` for deterministic networking) require all local client requests to target `127.0.0.1` rather than `localhost` to prevent connection resolve issues on macOS (where `localhost` tries to bind/resolve to IPv6 `[::1]` first).
* **Violations**: The following 10 test files contain hardcoded `localhost` references:
  1. `tests/units/test_agent_search_tools.py`:
     - Line 24: `if "localhost:8000" in url_str or "127.0.0.1:8000" in url_str:`
     - Line 41: `gsa_url="http://localhost:8000/",`
  2. `tests/units/test_hour_movie_scaffolding_bdd.py`:
     - Line 147: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     - Line 182: `resp = httpx.get(f"http://localhost:{self.assembly_port}/", timeout=1.0)  # health probe`
     - Line 362: `resp = httpx.post(f"http://localhost:{scaffold_helper.assembly_port}/", content="Wake up and check GSA")`
     - Line 422: `worker_url=f"http://localhost:888{idx + 1}",`
     - Line 678: `resp = httpx.post(f"http://localhost:{scaffold_helper.assembly_port}/", content="Wake up and check GSA")`
  3. `tests/units/test_longform_readiness_bdd.py`:
     - Line 64: `resp = httpx.get("http://localhost:8000/", timeout=1.0)`
     - Line 107: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)`
     - Line 196: `resp = httpx.post(f"http://localhost:{host_helper.agent_port}/", content="Wake up and check GSA")`
     - Line 201: `resp = httpx.post(f"http://localhost:{host_helper.agent_port}/", content="Wake up and check GSA")`
  4. `tests/units/test_provisioning_happy_path_bdd.py`:
     - Line 53: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     - Line 79: `resp = httpx.get(f"http://localhost:{self.provisioner_port}/", timeout=1.0)  # health probe`
     - Line 158: `worker_url="http://localhost:8881",`
     - Line 187: `worker_url="http://localhost:8882",`
     - Line 229: `worker_url="http://localhost:8883",`
     - Line 238: `worker_url="http://localhost:8884",`
     - Line 310: `worker_url="http://localhost:8882",`
     - Line 345: `worker_url="http://localhost:8883",`
  5. `tests/units/test_real_assembly_bdd.py`:
     - Line 59: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     - Line 93: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health probe`
     - Line 175: `resp = httpx.put(f"http://localhost:{assembly_helper.agent_port}/", content="Wake up and check GSA")`
  6. `tests/units/test_real_audio_reconciliation_bdd.py`:
     - Line 59: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     - Line 93: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health probe`
     - Line 175: `resp = httpx.put(f"http://localhost:{audio_helper.agent_port}/", content="Wake up and check GSA")`
     - Line 180: `resp = httpx.put(f"http://localhost:{audio_helper.agent_port}/", content="Wake up and check GSA")`
     - Line 233: `resp = httpx.get(f"http://localhost:{audio_helper.agent_port}/", timeout=1.0)  # health probe`
     - Line 235: `resp = httpx.put(f"http://localhost:{audio_helper.agent_port}/", content="Wake up and check GSA")`
  7. `tests/units/test_real_scenario_bdd.py`:
     - Line 53: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     - Line 90: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health probe`
     - Line 172: `resp = httpx.put(f"http://localhost:{scenario_helper.agent_port}/", content="Wake up and check GSA")`
  8. `tests/units/test_real_self_correction_bdd.py`:
     - Line 56: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     - Line 90: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health probe`
     - Line 172: `resp = httpx.put(f"http://localhost:{sc_helper.agent_port}/", content="Wake up and check GSA")`
  9. `tests/units/test_real_vast_provisioning_bdd.py`:
     - Line 64: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health check`
     - Line 98: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health check`
     - Line 190: `resp = httpx.post(f"http://localhost:{host_helper.agent_port}/", content="Wake up and check GSA")`
  10. `tests/units/test_real_video_provisioner_bdd.py`:
      - Line 63: `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health check`
      - Line 97: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health check`
      - Line 189: `resp = httpx.post(f"http://localhost:{host_helper.agent_port}/", content="Wake up and check GSA")`

---

### 2.2 HTTP Endpoint Concurrency Constraints Discrepancy

* **Specification**:
  - `01 - Philosophy and Topology.md` Section 1.10.1:
    `2. POST (POST / or POST /{prompt}): Non-interrupting standard/scheduled execution run. If the agent is already busy executing a turn, it immediately returns 409 Conflict (agent busy) instead of interrupting it.`
  - `08 - Testing, Concurrency, and Rollout.md` Section 3.1:
    `5. Agent Busy Safeguards: If an agent is processing a turn, its HTTP endpoints return a safe, immediate response (no double-processing).`
* **Discrepancy (Server Implementation vs. Specification)**:
  - In `server/agent_base.py`, the `post_handler` is defined as:
    ```python
    @app.post("/")
    async def post_handler(request: Request):
        # ...
        lock = run_lock_manager.get_lock()
        async with lock:  # Blocks if turn is active!
            # ...
    ```
    Instead of returning a `409 Conflict` immediate response when the agent is busy, it uses `async with lock:` which blocks the request until the previous turn is completed.
* **Discrepancy (BDD Test vs. Specification)**:
  - In `tests/units/test_concurrency_intervention_bdd.py` and `tests/units/features/concurrency_intervention.feature`, the test checks for the *incorrect blocking behavior*:
    ```gherkin
    Scenario: POST requests block to wait for active turns to finish
    ```
    This test runs and passes, but it enforces the wrong behavior (blocking) and ignores the specification requirement for a `409 Conflict` (immediate response).
* **Discrepancy (Unit Test vs. Server Implementation)**:
  - In `tests/units/test_concurrency_and_intervention.py`, `test_post_handler_rejects_with_409_when_busy` correctly asserts that the endpoint should return 409:
    ```python
    resp2 = await client.post("/", content="Second Prompt")
    assert resp2.status_code == 409
    assert resp2.text == "Agent is busy"
    ```
    This test **fails** in practice because the implementation blocks and returns `200` after the first request releases the lock.

---

### 2.3 Timeout Policy Violations

* **Specification**: `08 - Testing, Concurrency, and Rollout.md` Section 3.2:
  `The compliance scanner (cheat_check.py) scans code for timeout= properties on HTTP requests. Probing exceptions must be marked with a # health probe comment or contain the word health / probe to pass verification.`
* **Violations**: The following five test files contain `timeout=` HTTP request properties that lack the required comments, causing them to fail the compliance check:
  1. `tests/units/run_test_12_dynamic_shift.py`:
     - Line 99: `resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)`
  2. `tests/units/run_test_13_parallel_multitrack.py`:
     - Line 97: `resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)`
  3. `tests/units/test_pipeline_faults_bdd.py`:
     - Line 67: `resp = httpx.get("http://127.0.0.1:8000/", headers={"accept": "application/json"}, timeout=1.0)`
  4. `tests/units/test_longform_readiness_bdd.py`:
     - Line 64: `resp = httpx.get("http://localhost:8000/", timeout=1.0)`
     - Line 107: `resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)`

---

### 2.4 Coding Standards Violations (`cheat_check.py`)

* **Specification**: `server/cheat_check.py` scans for:
  - Swallowed exceptions: `pass` or `logger.debug` within 5 lines of `except` (Category: `SWALLOWED_EXCEPTION`).
  - Fixed polling: `time.sleep` or `asyncio.sleep` inside `while` or `for` loops without dynamic backoff (Category: `FIXED_POLLING`).
* **Violations**:
  - `tests/` directory contains **118 violations**:
    - Mostly `SWALLOWED_EXCEPTION` in BDD setup/cleanup routines (e.g. `except Exception: pass` when attempting to delete temp directories).
    - `FIXED_POLLING` in startup loops waiting for local servers to start.
  - `server/` directory contains **34 violations**:
    - Widespread empty `pass` blocks in `agent_base.py`, `otio_timeline_model.py`, and `slot_detail_model.py`.
    - Fixed polling loops in `agent_base.py` (Line 1112 and 1130).

---

## 3. GET / Health Status Logical Deadlock Analysis

* **Specification**: `01 - Philosophy and Topology.md` Section 1.10.1:
  `GET ... Accept: application/json -> Returns JSON payload: { status: healthy | busy | error, agent: agent_name, last_run: float, current_task: string, last_error: string, idle_since: float }`
* **Discrepancy**:
  In `server/agent_base.py`, the `health` check endpoint is wrapped in the turn execution lock:
  ```python
  @app.get("/")
  async def health(request: Request):
      lock = run_lock_manager.get_lock()
      async with lock:  # Blocks if turn is active!
          # ...
          return _agent_health
  ```
* **Impact**:
  Because it blocks on the lock, if an agent is currently running a heavy turn, any incoming `GET /` query will block and wait. It will only return a response *after* the heavy turn is finished and the lock has been released.
  At that point, the state `_agent_health["status"]` is set back to `"healthy"`. Therefore, a client querying `GET /` can **never observe the state "busy"**, which creates a logical deadlock in the monitoring design.

---

## 4. Proposed Fixes (Code Snippets)

### 4.1 Resolving Local IP Binding
In all flagged tests (e.g. `tests/units/test_real_audio_reconciliation_bdd.py`), replace `localhost` with `127.0.0.1`:
```python
# Before
resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe

# After
resp = httpx.get("http://127.0.0.1:8000/", timeout=1.0)  # health probe
```

### 4.2 Resolving `POST /` 409 Conflict Logic
In `server/agent_base.py`, modify the `post_handler` to check the lock status without blocking:
```python
# Before
@app.post("/")
async def post_handler(request: Request):
    # ...
    lock = run_lock_manager.get_lock()
    async with lock:
        # append and return ...

# After
@app.post("/")
async def post_handler(request: Request):
    lock = run_lock_manager.get_lock()
    if lock.locked():
        return PlainTextResponse("Agent is busy", status_code=409)
    async with lock:
        # append and return ...
```

### 4.3 Resolving `GET /` Health Deadlock
Remove `async with lock:` from the `health` handler so that it responds immediately even during active turns:
```python
# Before
@app.get("/")
async def health(request: Request):
    lock = run_lock_manager.get_lock()
    async with lock:
        # ...
        return response

# After
@app.get("/")
async def health(request: Request):
    # Retrieve current health state immediately without waiting for lock
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return _agent_health
    return PlainTextResponse(f"I am the {role} agent. Status: {_agent_health['status']}")
```

### 4.4 Resolving Timeout scanner warnings in Tests
Add `  # health probe` comment to the end of lines with `timeout=`:
```python
# Before
resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)

# After
resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)  # health probe
```
