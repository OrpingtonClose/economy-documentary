#!/usr/bin/env python3
import os
import sys
import json
import httpx

# Path to the DeepSeek API key on the host
DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"

# Extract rules from project documentation
RULES_PROMPT = """
You are the Antigravity Code Quality Enforcer for this project.
Your task is to analyze the content of the modified file and verify compliance with the project's Core Philosophy and System Invariants as defined in the Obsidian documentation:

=== THE 13 HARD PRINCIPLES ===
1. Event log as sole source of truth: All state is derived from events. No hidden or local persistent state. No projection writes state independently. The SQLite event file (e.g., events.db) is the sole durable storage.
2. Effects as only legal mutations: Only typed Pydantic models (Effects) enter the event store. The parser validates against the EffectUnion schema. Direct state mutation outside of appending events to the store is strictly prohibited.
3. No state machine - prompt-based rules: No code-based state machines or transition tables. The system's logical state emerges from folds/projections. Prioritization and decision-making logic live in the agents' natural language system prompts, not in Python code.
4. No timeouts in code: No timeout-based client self-destruction logic, `setTimeout`, `threading.Timer`, or `asyncio.timeout` anywhere in the pipeline or agent code. Subprocess calls and HTTP requests should run to completion. Note:
   - Standard SQLite database connection timeouts (e.g., `sqlite3.connect(..., timeout=30.0)` or `PRAGMA busy_timeout=30000`) are fully allowed to handle DB file contention.
   - HTTP clients in health-probe scripts or test helpers are allowed to use small timeouts (e.g. `timeout=1.0`) to avoid hanging the test harness.
   - Standard polling sleeps (e.g., `await asyncio.sleep(X)`) are fully allowed.
5. Real engines only: No mock or simulation layers for Qwen3-TTS (TTS), LTX-2.3 (Video), or LLM inference. Simulated/mocked components in production files are strictly prohibited.
6. Never regex: No regular expressions are used to extract structured data from agent outputs. Structured extraction must be semantic, category-conditioned via `instructor` and LLM.
7. Natural language only: Agents must output free-form prose and must not emit structured outputs (e.g. no JSON, XML, markdown tables, `EFFECT:` labels, or section tags). All extraction complexity must live inside the parser, never in the agent prompt or output format.
8. Provisioner is an agent: The Provisioner (port 8081) must be an LLM agent using `bash_command` as its only tool. It is not implemented as deterministic code.
9. Agent memory does not persist in process: Agents hold no session state. Each turn is rebuilt from projection summaries and a bounded message history (last 5 turns). No in-memory session variables between POSTs.
10. No automatic stale-state detection: VM workers do not have heartbeats, timers, or self-destruct logic. Stale state/hung VM detection is handled by the Provisioner reasoning about GSA projection state.
11. Serialized execution: Agent handlers must use a global lock to serialize concurrent updates to the database.
12. Tick-driven: Agents are HTTP services that poll the GSA via GET requests. There is no central orchestrator or central watcher loop.
13. Prompt-only HTTP interface: No fields from JSON request payloads or custom request headers/query parameters (such as notification_type or context) may be consumed or processed by any agent or platform logic. The HTTP interface serves solely to trigger execution or transmit prompts. All context must be dynamically resolved from the environment/filesystem (e.g., scanning the directory for the events database).

=== THE 6 SYSTEM INVARIANTS ===
1. Only GSA reads the store: The Global State Agent (GSA) on port 8000 is the sole component allowed to query or read the SQLite event store files. Agents, Provisioners, and worker nodes never read the SQLite files directly.
2. No agent writes the store: Agents never append to the event store database directly. The agent endpoint handler appends the semantic parser's extracted effects to the event store after the agent finishes generating text.
3. All agents read GSA frequently: Every agent (including the Provisioner) queries the GSA via `GET /` to obtain the current projection state.
4. Only `GET /` and `POST /` everywhere: All agent servers and the GSA expose ONLY bare `GET /` and `POST /` paths on their HTTP surfaces. No sub-endpoints or paths (e.g. no `/health`, no `/status`, no `/data`, no `/events`, no `/logs`) are allowed on any HTTP surface. All routing is on the root path `/`.
5. Only agents have LLM: The LLM is used only inside LLM agents (Scenario, Audio, Video, Assembly, Provisioner, Maintainer). No other pipeline components or VM Workers use LLM.
6. Provisioner is an agent: The Provisioner is an LLM-driven agent using `bash_command` as its tool, reading state from GSA via `GET /`.

=== ARCHITECTURAL BOUNDARY CLARIFICATIONS (CRITICAL) ===
- The Python agent hosting/framework infrastructure (specifically the server endpoints, background handlers, `execute_agent_turn`, and autonomous loop runner functions in files like `agent_base.py`) represents the hosting harness/platform, not the agent itself. This harness is fully permitted to read the SQLite database (e.g., via `event_store.read_all` or `read_last_n_effects`) to query history, check active execution, build conversation memory, and to write/append parsed effects to the SQLite database. This does not violate System Invariants 1 or 2, which govern the LLM agent's internal logic, tools, and behavior.
- All GET queries and POST requests to GSA, agents, or VM Workers must be bare requests to the root path / with no query parameters and no custom headers. Request bodies/payload JSON fields must not be consumed by handlers to pass semantic data; the HTTP interface serves solely to trigger execution or transmit prompts. All context must be dynamically resolved from the environment/filesystem (e.g., scanning the directory for the events database).
- The standard AgentHealthResponse schema returned by agent `GET /` (containing status, agent, last_run, current_task, last_error, idle_since) is the defined, compliant layout for agent health probes.
- The autonomous loop runner (started inside the hosting server process of each agent HTTP service to check GSA and decide when that specific agent should act, and calls the turn executor) is the standard tick-driven harness of the system. It is fully permitted to query state, read events, and append effects to execute the agent's turn. It is not considered a central orchestrator under Principle 12.
- The simple checks in the autonomous loop runner to determine whether to trigger an agent turn (e.g. checking for unfilled slots, failed jobs, or reconciliation needs) are simple activation triggers. They do not constitute a state machine under Principle 3, because they do not manage transitions, maintain state variables, or define business logic. All agent decisions and logical rules remain inside the agent's LLM prompt.
- Principle 6 ("Never regex") applies to the semantic parser and the extraction of structured effects from agent outputs. It does NOT forbid utility string scanning or command validation using Python's regular expressions inside platform functions (like path checks in `bash_command`).
- Test code, test suites, and test helper scripts (specifically files located in the `tests/` directory or prefixed with `test_`) are completely exempt from the System Invariants and Core Principles, as they do not run in production and are designed to inspect the database, filesystem, or ports directly to verify system behavior.

Review the file path and file content below and determine if any of these rules or invariants have been violated.

CRITICAL INSTRUCTION ON JSON RESPONSE FORMAT:
- Conduct your step-by-step analysis and compliance checks inside the "reasoning" key.
- The "violations" list must contain ONLY actual, verified violations. Do NOT list candidate rules, non-violations, or items that you conclude are compliant in the "violations" list.
- If there are zero actual violations, you MUST return "status": "PASS" and "violations": [].

You MUST respond in JSON format with the following keys:
{
  "reasoning": "Step-by-step analysis of each rule and invariant, determining if it is compliant or violated.",
  "status": "PASS" or "FAIL",
  "violations": [
    "Violation 1 description with file and line if visible",
    "Violation 2..."
  ]
}
"""

def main():
    if len(sys.argv) < 2:
        print("✅ Antigravity Plugin: No file argument passed. PASS.")
        sys.exit(0)

    file_path = sys.argv[1]

    # Skip checking the enforcer plugin itself
    if "rules_enforcer_plugin.py" in file_path:
        print("✅ Antigravity Plugin: Skipping self-check. PASS.")
        sys.exit(0)

    if not os.path.exists(file_path):
        print(f"✅ Antigravity Plugin: File {file_path} does not exist (possibly deleted). PASS.")
        sys.exit(0)

    # Read API Key
    if not os.path.exists(DEEPSEEK_KEY_PATH):
        print(f"⚠️ Antigravity Plugin: API key file not found at {DEEPSEEK_KEY_PATH}. Skipping checks.")
        sys.exit(0)

    with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
        api_key = f.read().strip()

    if not api_key:
        print("⚠️ Antigravity Plugin: Empty API key. Skipping checks.")
        sys.exit(0)

    print(f"🔍 Antigravity Plugin: Analyzing file {file_path} using DeepSeek...")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"⚠️ Antigravity Plugin: Could not read {file_path}: {e}")
        sys.exit(0)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": RULES_PROMPT},
            {"role": "user", "content": f"File Path: {file_path}\n\nFile Content:\n```python\n{content}\n```"}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}
    }

    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=60.0)
        if resp.status_code != 200:
            print(f"❌ Antigravity Plugin: DeepSeek API error (status {resp.status_code}): {resp.text}")
            sys.exit(1)

        raw_content = resp.json()["choices"][0]["message"]["content"]
        try:
            result = json.loads(raw_content)
        except json.JSONDecodeError as jde:
            print(f"❌ Antigravity Plugin: Failed to parse JSON response. Raw content:\n{raw_content}")
            raise jde
        if result.get("status") == "FAIL":
            print("\n❌ ANTIGRAVITY RULES VIOLATION DETECTED!")
            for v in result.get("violations", []):
                print(f" - {v}")
            print("\nPlease revert or fix the rule violations.\n")
            sys.exit(1)
        else:
            print("✅ Antigravity Plugin: File conforms to core principles. PASS.")
            sys.exit(0)
    except Exception as e:
        print(f"❌ Antigravity Plugin: Verification failed for {file_path}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
