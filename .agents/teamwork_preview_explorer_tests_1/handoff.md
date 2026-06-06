# Handoff Report — Test Suite Compliance Analysis

This report documents the findings and verification of the python test files compliance scan against the project specifications.

## 1. Observation

1. **Local Execution IP Binding (`localhost` vs `127.0.0.1`)**:
   - Multiple tests bind/call `localhost` directly. For example, `tests/units/test_hour_movie_scaffolding_bdd.py` at line 147:
     `resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe`
     and line 422:
     `worker_url=f"http://localhost:888{idx + 1}",`
   - A search for `localhost` in the `tests/units/` directory found occurrences in 10 test files (detailed in `analysis.md`).

2. **HTTP Endpoint Concurrency Constraints (`POST /` conflict)**:
   - In `server/agent_base.py` (line 1092):
     ```python
     @app.post("/")
     async def post_handler(request: Request):
         ...
         lock = run_lock_manager.get_lock()
         async with lock:
     ```
     This endpoint acquires the serialization lock, which blocks the incoming `POST` request rather than returning `409 Conflict`.
   - In `tests/units/test_concurrency_and_intervention.py` (line 43), the unit test `test_post_handler_rejects_with_409_when_busy` asserts:
     ```python
     assert resp2.status_code == 409
     ```
     This test fails with `AssertionError: assert 200 == 409` when executed on the current server app implementation.
   - In `tests/units/test_concurrency_intervention_bdd.py` (line 208), the BDD scenario `"POST requests block to wait for active turns to finish"` asserts blocking behavior, which is in direct conflict with the 409 spec.

3. **Timeout Scanner Failures (`cheat_check.py`)**:
   - Running the compliance scanner command:
     `python server/cheat_check.py server tests`
     resulted in **152 violations**: 34 in `server/` and 118 in `tests/`.
   - Five test files have `timeout=` HTTP calls that lack `# health probe` comments (e.g. `tests/units/test_pipeline_faults_bdd.py` line 67: `timeout=1.0`), which fails scanner validation category `TIMEOUT`.

4. **GET / Health Check Logical Deadlock**:
   - In `server/agent_base.py` (line 978):
     ```python
     @app.get("/")
     async def health(request: Request):
         lock = run_lock_manager.get_lock()
         async with lock:
     ```
     This endpoint blocks on the turn lock, meaning it will never return a `"busy"` status value since the request will only resolve after the lock is released (and status has reset to `"healthy"`).

---

## 2. Logic Chain

1. **Mac OS Loopback Latency**: Hardcoded `localhost` references in 10 test files resolve to both IPv4 and IPv6 loopback addresses on macOS systems. In environments where uvicorn only binds to `127.0.0.1`, this results in connection delays or timeouts.
2. **Server/Test Inconsistency**:
   - The specifications require that overlapping `POST` requests receive a `409 Conflict` (agent busy) response.
   - However, the server handler `post_handler` uses `async with lock:` which blocks instead.
   - Because the BDD tests were updated to assert that `POST` blocks, the BDD tests pass, but they validate the *wrong* behavior.
   - The unit tests that check for the *correct* 409 behavior fail, confirming a real regression and design gap.
3. **Timeout Scanning Validation**: `cheat_check.py` checks for `timeout=` on any line that contains `httpx`, `requests`, or `urllib` and rejects it if the line does not contain the word `health` or `probe`. Several test files lack these comments, failing the scanner check.
4. **Logical deadlocks on status reporting**: Wrapping `GET /` inside the `async with lock:` statement means that status checks are serialized with agent turns. A client cannot query the agent while it is busy to determine that it is busy, because the query blocks until the agent is no longer busy.

---

## 3. Caveats

* **Execution in CODE_ONLY mode**: Integration BDD tests that instantiate real agent servers (such as `test_real_audio_reconciliation_bdd.py`) call chat APIs (e.g. DeepSeek) under the hood. Since the network mode is restricted, these tests will hang or timeout when trying to query external services. We only verified them statically and using simulated unit mock environments.
* **Vast VM Provisioning**: The preemption and Vast provisioning tests utilize subprocess CLI commands (`vastai`) which were not run dynamically with actual cloud infrastructure.

---

## 4. Conclusion

The testing suite contains critical discrepancies between the codebase, the BDD test assumptions, and the technical specification in the vault:
- **Local Hostname binding**: macOS compatibility requires `localhost` to be rewritten to `127.0.0.1`.
- **Concurrency validation**: The implementation of `post_handler` and `health` in `agent_base.py` must be corrected so that `POST /` returns `409` immediately and `GET /` health checks respond without blocking, aligning with both the technical specs and passing unit tests.
- **Scanner Warnings**: `timeout=` arguments in test helpers must have comments to satisfy compliance.

---

## 5. Verification Method

1. Run the compliance scan manually to see compliance warning categories and counts:
   `python server/cheat_check.py server tests`
2. Run the concurrency unit test to observe the failing 409 assertion:
   `pytest tests/units/test_concurrency_and_intervention.py`
3. Check the contents of the generated `analysis.md` report at:
   `/Users/orpington/Documents/economy-documentary-work/.agents/teamwork_preview_explorer_tests_1/analysis.md`
