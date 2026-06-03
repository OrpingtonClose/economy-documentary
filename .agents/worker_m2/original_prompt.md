## 2026-06-03T00:41:00Z
You are teamwork_preview_worker.
Your working directory is /Users/orpington/Documents/economy-documentary-work/.agents/worker_m2/.
Your task is to implement the concurrency and intervention logic in server/agent_base.py.

Requirements:
1. Modify `post_handler` in `server/agent_base.py` (around line 792) to reject requests with a 409 Conflict if the agent is busy (i.e., if active_task is running and not done):
   ```python
   # Reject if busy
   if active_task and not active_task.done():
       logger.info(f"Rejecting POST request for {role} because the agent is busy")
       return PlainTextResponse(content="Agent is busy", status_code=409)
   ```
2. Modify `bash_command` in `server/agent_base.py` (around line 281) to ensure all descending processes in the subprocess are terminated when cancelled:
   - Pass `preexec_fn=os.setsid` to `asyncio.create_subprocess_shell` to start the subprocess in a new process group.
   - In the `except asyncio.CancelledError:` block, use `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` to terminate the process group.
3. Run the existing tests using the project's virtualenv pytest: `.venv/bin/pytest`. All tests must pass successfully.
4. Record your progress in /Users/orpington/Documents/economy-documentary-work/.agents/worker_m2/progress.md.
5. Create a handoff report in /Users/orpington/Documents/economy-documentary-work/.agents/worker_m2/handoff.md containing the results, changes made, and test outputs.
6. Once complete, send a message to the orchestrator.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
