#!/usr/bin/env python3
"""Antigravity Rules Enforcer Plugin — runs on file edits during development."""

import os
import sys
import httpx
from pathlib import Path

DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"

def get_invariants(vault_dir: Path) -> list[str]:
    """Grep all markdown files in the vault for rules starting with ⚡."""
    invariants = []
    if not vault_dir.exists():
        return invariants

    for md_file in sorted(vault_dir.rglob("*.md")):
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("⚡"):
                        rule = stripped[1:].strip()
                        if rule:
                            invariants.append(rule)
        except Exception:
            pass
    return invariants

def main():
    if len(sys.argv) < 2:
        print("✅ Antigravity Plugin: No file argument passed. PASS.")
        sys.exit(0)

    file_path_str = sys.argv[1]
    file_path = Path(file_path_str)

    # Check if we are modifying the plugin directory or checker scripts
    path_str = str(file_path.resolve())
    if ".agents/plugins/" in path_str or "rules_enforcer_plugin.py" in file_path.name or "cheat_check.py" in file_path.name:
        approval_file = Path("/tmp/antigravity_plugin_approval.txt")
        if not approval_file.exists():
            print("\n❌ ANTIGRAVITY RULES VIOLATION DETECTED!")
            print("Modifications to the rules enforcer plugin directory (.agents/plugins/) or checker scripts require explicit user approval.")
            print("To approve, please create the file /tmp/antigravity_plugin_approval.txt before running the command.\n")
            sys.exit(1)
        else:
            try:
                os.remove(approval_file)
            except Exception:
                pass
            print("✅ Antigravity Plugin: Modification approved. PASS.")
            sys.exit(0)

    if file_path.suffix != ".py":
        print(f"✅ Antigravity Plugin: Non-Python file {file_path_str} skipped. PASS.")
        sys.exit(0)

    if not file_path.exists():
        print(f"✅ Antigravity Plugin: File {file_path_str} does not exist (possibly deleted). PASS.")
        sys.exit(0)

    # Exclude test files and legacy agent memory
    path_str = str(file_path.resolve())
    if "server/capabilities" in path_str:
        try:
            sys.path.append("/Users/orpington/.gemini/config/plugins/sc-guard-enforcer")
            from sc_guard_enforcer import check_file
            passed = check_file(path_str)
            if passed:
                print(f"✅ Antigravity Plugin: Simulation Cover file {file_path_str} passed SC integrity validation.")
                sys.exit(0)
            else:
                print(f"❌ Antigravity Plugin: Simulation Cover integrity violations detected in {file_path_str} (mocking, skipping or trivial assertions).")
                sys.exit(1)
        except Exception as e:
            print(f"❌ Antigravity Plugin: SC Guard Enforcer execution error: {e}")
            sys.exit(1)

    if "/tests/" in path_str or file_path.name.startswith("test_") or "agent_memory" in file_path.parts or "/agent_memory/" in path_str or "agent_memory/" in path_str:
        print(f"✅ Antigravity Plugin: Test or legacy memory file {file_path_str} skipped. PASS.")
        sys.exit(0)

    workspace_dir = Path("/Users/orpington/Documents/economy-documentary-work")
    vault_dir = workspace_dir / "obsidian-vault"
    if not vault_dir.exists():
        vault_dir = Path(__file__).resolve().parent.parent / "obsidian-vault"

    # 1. Load rules from vault
    invariants = get_invariants(vault_dir)
    if not invariants:
        print("⚠️ Antigravity Plugin: No architectural invariants found in Obsidian Vault. PASS.")
        sys.exit(0)

    # 2. Get API key
    if not os.path.exists(DEEPSEEK_KEY_PATH):
        print(f"⚠️ Antigravity Plugin: API key file not found at {DEEPSEEK_KEY_PATH}. Skipping checks.")
        sys.exit(0)

    with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
        api_key = f.read().strip()

    if not api_key:
        print("⚠️ Antigravity Plugin: Empty API key. Skipping checks.")
        sys.exit(0)

    print(f"🔍 Antigravity Plugin: Analyzing file {file_path_str} using DeepSeek...")

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Antigravity Plugin: Could not read {file_path_str}: {e}")
        sys.exit(0)

    invariants_text = "\n".join(f"- {rule}" for rule in invariants)
    system_prompt = (
        "You are an architecture enforcer. Check if the target code violates any of the provided rules. "
        "Analyze all imports, function definitions, parameters, and expressions.\n"
        "If there are any violations, reply with each violated rule and a one-line explanation.\n"
        "If there are absolutely no violations, reply with PASS.\n\n"
        "=== ARCHITECTURAL BOUNDARY CLARIFICATIONS (CRITICAL) ===\n"
        "- The Python agent hosting/framework infrastructure (specifically the server endpoints, background handlers, "
        "`execute_agent_turn`, and autonomous loop runner functions in files like `agent_base.py`) represents the "
        "hosting harness/platform, not the agent itself. This harness is fully permitted to read the SQLite database "
        "(e.g., via `event_store.read_all` or `read_last_n_effects`) to query history, check active execution, "
        "build conversation memory, and to write/append parsed effects to the SQLite database. This does not violate "
        "System Invariants 1 or 2, which govern the LLM agent's internal logic, tools, and behavior.\n"
        "- All GET queries and POST requests to GSA, agents, or VM Workers must be bare requests to the root path / "
        "with no query parameters and no custom headers. Request bodies/payload JSON fields must not be consumed by "
        "handlers to pass semantic data; the HTTP interface serves solely to trigger execution or transmit prompts. "
        "All context must be dynamically resolved from the environment/filesystem (e.g., scanning the directory for the events database).\n"
        "- The standard AgentHealthResponse schema returned by agent `GET /` (containing status, agent, last_run, "
        "current_task, last_error, idle_since) is the defined, compliant layout for agent health probes.\n"
        "- The autonomous loop runner (started inside the hosting server process of each agent HTTP service to check "
        "GSA and decide when that specific agent should act, and calls the turn executor) is the standard tick-driven "
        "harness of the system. It is fully permitted to query state, read events, and append effects to execute the "
        "agent's turn. It is not considered a central orchestrator under Principle 12.\n"
        "- The simple checks in the autonomous loop runner to determine whether to trigger an agent turn (e.g. checking "
        "for unfilled slots, failed jobs, or reconciliation needs) are simple activation triggers. They do not constitute "
        "a state machine under Principle 3, because they do not manage transitions, maintain state variables, or define "
        "business logic. All agent decisions and logical rules remain inside the agent's LLM prompt.\n"
        "- Principle 6 (\"Never regex\") applies to the semantic parser and the extraction of structured effects from "
        "agent outputs. It does NOT forbid utility string scanning or command validation using Python's regular "
        "expressions inside platform functions (like path checks in `bash_command`).\n"
        "- Test code, test suites, and test helper scripts (specifically files located in the `tests/` directory or "
        "prefixed with `test_`) are completely exempt from the System Invariants and Core Principles, as they do not run "
        "in production and are designed to inspect the database, filesystem, or ports directly to verify system behavior.\n"
    )
    user_content = f"""Rules to enforce:
{invariants_text}

Target File: {file_path.name}
Target Code Content:
```python
{content}
```
"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.0,
    }

    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            print(f"❌ Antigravity Plugin: DeepSeek API error (status {resp.status_code}): {resp.text}")
            sys.exit(1)

        response_text = resp.json()["choices"][0]["message"]["content"]
        clean_resp = response_text.strip().strip(".").strip().upper()
        if clean_resp == "PASS":
            print("✅ Antigravity Plugin: File conforms to core principles. PASS.")
            sys.exit(0)
        else:
            print("\n❌ ANTIGRAVITY RULES VIOLATION DETECTED!")
            print(response_text)
            print("\nPlease revert or fix the rule violations.\n")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Antigravity Plugin: Verification failed for {file_path_str}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
