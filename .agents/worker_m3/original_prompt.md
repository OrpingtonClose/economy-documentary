## 2026-06-03T01:32:57Z
You are teamwork_preview_worker.
Your working directory is /Users/orpington/Documents/economy-documentary-work/.agents/worker_m3/.
Your task is to write and run BDD tests using `pytest-bdd` to verify the concurrency and intervention logic in server/agent_base.py.

Requirements:
1. Write the step definition file `tests/units/test_concurrency_intervention_bdd.py` to match the scenarios in `tests/units/features/concurrency_intervention.feature` (already created).
2. The step definitions must:
   - Use `@pytest.mark.anyio` and `async def` for scenario tests and step definitions.
   - Use `httpx.AsyncClient` with `ASGITransport` on `make_agent_app("test_agent")` to run the agent in-process, allowing robust, offline mocking using `unittest.mock.patch`.
   - Verify that concurrent POST requests return 409 Conflict when busy.
   - Verify that GET health queries on `/` run concurrently and return immediately (returning JSON with status "busy").
   - Verify that PUT requests cancel the active turn, instantly terminate any running bash subprocesses (killing the process group via `os.killpg`), and start a new turn.
   - Programmatically verify that no orphan processes from the subprocess group remain on the system (using `os.killpg(pgid, 0)` raising `ProcessLookupError`).
3. Run the new BDD test using the project's virtualenv pytest: `.venv/bin/pytest tests/units/test_concurrency_intervention_bdd.py`. Verify that the test runs and passes 100% successfully.
4. Record your progress in /Users/orpington/Documents/economy-documentary-work/.agents/worker_m3/progress.md.
5. Create a handoff report in /Users/orpington/Documents/economy-documentary-work/.agents/worker_m3/handoff.md containing the results and test outputs.
6. Once complete, send a message to the orchestrator.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
