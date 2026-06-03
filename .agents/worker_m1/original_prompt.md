## 2026-06-03T00:39:22Z
You are teamwork_preview_worker.
Your working directory is /Users/orpington/Documents/economy-documentary-work/.agents/worker_m1/.
Your task is to refactor all existing tests to replace "localhost" with "127.0.0.1" in all HTTP request destinations.
This is required to fix macOS connectivity issues where "localhost" resolves to IPv6 [::1], causing failures with Uvicorn servers bound to 127.0.0.1.

Requirements:
1. Find all files in the tests/ directory containing "localhost".
2. Refactor them by replacing "localhost" with "127.0.0.1" in HTTP request URIs (e.g. http://localhost:8000/ -> http://127.0.0.1:8000/).
   Make sure you do NOT break regex checks or other logic that expects "localhost" or "127.0.0.1" specifically (such as check logic that allows either, e.g. test_agent_search_tools.py).
3. Run the existing tests using the project's virtualenv pytest: .venv/bin/pytest.
4. Record your progress in /Users/orpington/Documents/economy-documentary-work/.agents/worker_m1/progress.md.
5. Create a handoff report in /Users/orpington/Documents/economy-documentary-work/.agents/worker_m1/handoff.md containing the results, commands run, and test outputs.
6. Once complete, send a message to the orchestrator.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
