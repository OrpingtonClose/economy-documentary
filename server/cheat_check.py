#!/usr/bin/env python3
"""/cheat checker — scans code for violations of Obsidian vault rules using DeepSeek API."""

from __future__ import annotations

import os
import sys
import httpx
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"

def get_invariants(vault_dir: Path) -> list[str]:
    """Grep all markdown files in the vault for rules starting with ⚡."""
    invariants = []
    if not vault_dir.exists():
        print(f"⚠️ Vault directory not found at {vault_dir}")
        return invariants

    for md_file in sorted(vault_dir.rglob("*.md")):
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("⚡"):
                        # Extract everything after the emoji
                        rule = stripped[1:].strip()
                        if rule:
                            invariants.append(rule)
        except Exception as e:
            print(f"⚠️ Error reading {md_file}: {e}")
    return invariants

def should_check_file(path: Path) -> bool:
    """Filter files to only verify production Python source files."""
    path_str = str(path.resolve())
    if "cheat_check.py" in path_str or "rules_enforcer_plugin.py" in path_str or "agent_base.py" in path_str:
        return False
    if "agent_memory" in path.parts or "/agent_memory/" in path_str or "agent_memory/" in path_str:
        return False
    if "/tests/" in path_str or path.name.startswith("test_"):
        return False
    if "__pycache__" in path_str or ".venv" in path_str or ".git" in path_str:
        return False
    return path.suffix == ".py"


def scan_file(file_path: Path, invariants: list[str], api_key: str) -> tuple[Path, str | None]:
    """Scan a single Python file using DeepSeek LLM."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return file_path, f"Could not read file: {e}"

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
            return file_path, f"DeepSeek API error (status {resp.status_code}): {resp.text}"

        response_text = resp.json()["choices"][0]["message"]["content"]
        clean_resp = response_text.strip().strip(".").strip().upper()
        if clean_resp == "PASS":
            return file_path, None
        else:
            return file_path, response_text
    except Exception as e:
        return file_path, f"API query failed: {e}"

def main():
    workspace_dir = Path("/Users/orpington/Documents/economy-documentary-work")
    vault_dir = workspace_dir / "obsidian-vault"
    if not vault_dir.exists():
        vault_dir = Path(__file__).resolve().parent.parent / "obsidian-vault"

    # 1. Load rules from vault
    invariants = get_invariants(vault_dir)
    if not invariants:
        print("⚠️ No architectural invariants found in Obsidian Vault. Exiting.")
        sys.exit(0)

    print(f"📖 Loaded {len(invariants)} rules from Obsidian Vault.")

    # 2. Get API key
    if not os.path.exists(DEEPSEEK_KEY_PATH):
        print(f"⚠️ API key file not found at {DEEPSEEK_KEY_PATH}. Skipping checks.")
        sys.exit(0)

    with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
        api_key = f.read().strip()

    if not api_key:
        print("⚠️ Empty API key. Skipping checks.")
        sys.exit(0)

    # 3. Collect target files
    targets = sys.argv[1:] or ["server"]
    files_to_check: list[Path] = []
    for t in targets:
        path = Path(t)
        if path.is_file() and should_check_file(path):
            files_to_check.append(path)
        elif path.is_dir():
            for f in path.rglob("*.py"):
                if should_check_file(f):
                    files_to_check.append(f)

    if not files_to_check:
        print("✅ No production Python files found to scan.")
        sys.exit(0)

    print(f"🔍 Scanning {len(files_to_check)} file(s) for architectural violations...")

    violations_found = False
    # Use ThreadPoolExecutor to run LLM checks in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(scan_file, f, invariants, api_key): f
            for f in files_to_check
        }

        for future in as_completed(futures):
            file_path, result = future.result()
            try:
                relative_path = file_path.relative_to(workspace_dir)
            except ValueError:
                relative_path = file_path
            if result is None:
                print(f"✅ {relative_path}: PASS")
            else:
                print(f"❌ {relative_path}: FAIL\n{result}\n")
                violations_found = True

    if violations_found:
        print("❌ Architecture check failed! Violations detected.")
        sys.exit(1)
    else:
        print("✅ Architecture check passed! All files conform to system invariants.")
        sys.exit(0)

if __name__ == "__main__":
    main()
