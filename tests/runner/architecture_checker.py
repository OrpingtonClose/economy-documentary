import os
import sys
import httpx
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def load_docs():
    """Load the contents of the testing and simulation cover documentation."""
    docs_text = ""
    vault_dir = PROJECT_ROOT / "obsidian-vault"
    doc_paths = [
        vault_dir / "08 - Testing, Concurrency, and Rollout.md",
        vault_dir / "10 - Simulation Covers.md"
    ]
    for p in doc_paths:
        if p.exists():
            docs_text += f"\n=== DOCUMENTATION FILE: {p.name} ===\n"
            docs_text += p.read_text(encoding="utf-8")
        else:
            print(f"⚠️ Warning: Documentation file not found: {p}")
    return docs_text

def audit_file(file_path, docs_context, api_key):
    """Deeply audit a single test file against documentation using DeepSeek API to look for sneaky sabotage."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return file_path.name, f"Failed to read file: {e}"

    system_prompt = (
        "You are a paranoid security and architecture auditor auditing unit/integration test files for compliance with system invariants.\n"
        "Assume the developer has attempted SNEAKY SABOTAGE by bypassing the rules using custom wrappers, overriding libraries manually, "
        "or hiding cheats from standard static analysis checkers.\n\n"
        "Carefully analyze all imports, functions, variables, decorators, loops, assertions, and helper functions.\n\n"
        "=== CRITICAL CONTEXT: TEST FILE CATEGORIES ===\n"
        "The test suite consists of three distinct categories of files:\n"
        "1. **Simulation Covers (SC)**: These are the 10 files listed in the master registry tables (specifically: "
        "`test_gsa_wal_concurrency_isolation.py`, `test_scenario_agent_live_prompt_turn.py`, `test_ssh_handshake_and_docker_health.py`, "
        "`test_audio_agent_tts_job_queueing.py`, `test_coordinate_timeline_dynamic_drift.py`, `test_vast_create_and_destroy_lifecycle.py`, "
        "`test_provisioner_vast_offers_search.py`, `test_budget_limit_aborted_gate.py`, `test_audio_loudness_normalizer_compilation.py`, "
        "`test_perplexity_verify_live.py`). These 10 files MUST connect to real-world boundaries (live network, real API calls, real shell commands, real ffmpeg runs) "
        "and fail immediately if those dependencies are missing. They must have absolutely NO mocking, no stubs, and no simulator capabilities.\n"
        "2. **BDD Integration / Capacity Test Launchers**: These are integration/scenario tests (often named `test_bdd_*` or `test_max_*`). "
        "They verify complex agent behaviors, recovery flows, or scaling capacities. They are explicitly permitted to use **Inline Simulation** (defining or importing simulator capabilities/models/classes "
        "such as `GenericAudioSimulator`, `GenericVideoSimulator`, `TtsSingleBlockSimulator`, `TtsJobSimulator`, `WorkerHealthSimulator`, etc.) to run test scenarios in simulated environments by driving production "
        "agents via database events and HTTP requests, and may run BDD Judge validation. However, they must still be free of standard mocking libraries (unittest.mock, pytest_mock, MagicMock, patch), "
        "free of skips, free of time-based timeouts, and free of trivial/circular assertions.\n"
        "3. **Standard Unit/Regression Tests**: All other files (e.g. `test_parse_duration_all_formats.py`, `test_event_store_*.py`, `test_*_projection_*.py`) "
        "are standard Python unit tests of local code/data structure logic. They run completely in memory and do not need live networks or GPUs. They must still be mock-free "
        "(no unittest.mock/pytest_mock), skip-free (no pytest.skip), trivial-assertion-free, and timeout-free.\n\n"
        "=== SPECIAL CLARIFICATIONS ===\n"
        "- **`test_sim_` Mappings**: Files prefixed with `test_sim_` (e.g., `test_sim_voice_continuity_expanded.py`, `test_sim_gsa_wal_expanded.py`, "
        "`test_sim_otio_assembly_expanded.py`, `test_sim_provisioner_expanded.py`) are **Standard Unit/Regression Tests** (Category 3) verifying local logic in memory. "
        "Despite the `test_sim_` name prefix, they are NOT Simulation Covers (SC) and are completely exempt from the live boundary / live infrastructure requirement. They are permitted to run locally/offline.\n"
        "- **Temporary Directories**: Creating and testing EventStore or databases inside `tempfile.TemporaryDirectory()` or `tempfile.mkdtemp()` is standard for local unit tests and is NOT a database mocking violation.\n"
        "- **Sleeps & Performance Timing**: Short sleeps using `time.sleep(n)` for passive waiting/polling in loops or thread synchronization in tests are allowed, provided there is no hard timeout deadline/limit on the loop. Performance timing measurements (e.g., measuring elapsed time of operations) are allowed and do not violate the timeout policy.\n"
        "- **Unused Imports**: The presence of unused imports is a code quality issue, but does NOT constitute a security violation unless it directly imports forbidden mocking libraries (unittest.mock/pytest_mock).\n\n"
        "Rules to enforce:\n"
        "1. Forbidden Mocking Libraries: No imports or usage of unittest.mock, pytest_mock, MagicMock, Mock, PropertyMock, patch, etc.\n"
        "2. No Boundary Mocking: For **Simulation Covers**, no interception, mocking, or monkey-patching of the production code path or physical/network boundaries. "
        "Ensure there are no silent dry-run or offline fallbacks. BDD Integration launchers may use inline simulation as described above, but must not use standard mock libraries.\n"
        "3. No Skips: No pytest.skip, no skip() calls, no @pytest.mark.skip decorators, and no conditional returns/checks that effectively bypass execution.\n"
        "4. No Trivial Assertions: No `assert True`, `assert False`, `assert 1 == 1`, or comparisons of two literal constants. "
        "Note: Feeding manual events to a projection class (e.g. BudgetProjection, Timeline, Jobs) and asserting that the projection's state "
        "updates correctly (e.g. spent_usd == 2.20 or exceeded is True) is a standard projection unit test, NOT trivial reasoning or circular logic.\n"
        "5. No Time-Based Timeouts: Time-based timeouts/limits on execution are strictly forbidden. Network calls and subprocesses must not specify "
        "`timeout=` parameters. Loop conditions must not check elapsed time limits (e.g. `while time.time() - start_time < 300` or a fixed loop iteration cap with sleep that acts as a hard deadline). "
        "Note: Short sleeps using `time.sleep(n)` for passive waiting/polling in loops or tests are allowed, provided there is no hard timeout deadline/limit on the loop.\n\n"
        "Below is the relevant system documentation:\n"
        f"{docs_context}\n\n"
        "If you find any violation, reply with each violated rule and a detailed explanation showing the exact line/context.\n"
        "If there are absolutely no violations, reply with PASS."
    )

    user_content = (
        f"Target File: {file_path.name}\n"
        f"Target Code Content:\n"
        "```python\n"
        f"{content}\n"
        "```\n"
    )

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
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=45.0)
        if resp.status_code != 200:
            return file_path.name, f"DeepSeek API returned error status {resp.status_code}: {resp.text}"
        
        response_text = resp.json()["choices"][0]["message"]["content"].strip()
        if response_text.strip().upper() == "PASS" or response_text.strip().upper().startswith("PASS"):
            return file_path.name, None
        else:
            return file_path.name, response_text
    except Exception as e:
        return file_path.name, f"DeepSeek API call failed: {e}"

def run_agentic_architecture_test():
    print("🔍 Running Agentic Architecture Test on tests/units...")
    
    # 1. Load API key
    if not os.path.exists(DEEPSEEK_KEY_PATH):
        print("❌ ARCHITECTURE TEST FAILURE! DeepSeek API key is missing for the agentic audit.")
        sys.exit(1)
        
    with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
        api_key = f.read().strip()
        
    if not api_key:
        print("❌ ARCHITECTURE TEST FAILURE! DeepSeek API key is empty.")
        sys.exit(1)

    # 2. Load documentation context
    docs_context = load_docs()
    if not docs_context.strip():
        print("❌ ARCHITECTURE TEST FAILURE! Could not load system documentation.")
        sys.exit(1)

    # 3. Collect Python test files in tests/units/
    tests_dir = PROJECT_ROOT / "tests" / "units"
    if not tests_dir.exists():
        print("✅ Architecture Test: No tests directory found.")
        return

    py_files = []
    for f in tests_dir.glob("*.py"):
        if f.name not in ("__init__.py", "harness.py"):
            py_files.append(f)

    if not py_files:
        print("✅ Architecture Test: No test files to scan.")
        return

    print(f"🕵️ Auditing {len(py_files)} test files deeply via DeepSeek...")

    violations = []
    # Concurrently scan all files with ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(audit_file, f, docs_context, api_key): f
            for f in py_files
        }
        
        for future in as_completed(futures):
            filename, result = future.result()
            if result is not None:
                violations.append(f"File: {filename}\n{result}")
                print(f"❌ {filename}: FAIL")
            else:
                print(f"✅ {filename}: PASS")

    if violations:
        print("\n❌ ARCHITECTURE TEST FAILURE! Test runner execution aborted.")
        for violation in violations:
            print(f"\n--------------------------------------------------\n{violation}")
        sys.exit(1)
    else:
        print("\n✅ Architecture Test: Passed (all tests compliant with simulation cover invariants).\n")
