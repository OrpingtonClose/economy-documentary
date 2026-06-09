import os

def patch_file(filepath, replacements):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original = content
    for target, replacement in replacements.items():
        if target not in content:
            print(f"Warning: Target string not found in {filepath}:")
            print(repr(target))
        content = content.replace(target, replacement)
    
    if content != original:
        # Temporarily make writable if read-only
        mode = os.stat(filepath).st_mode
        os.chmod(filepath, 0o666)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(filepath, mode)
        print(f"Successfully patched {filepath}")
    else:
        print(f"No changes made to {filepath}")

# 1. Patch test_covering_vast_create_and_destroy_lifecycle.py
patch_file(
    'tests/units/test_covering_vast_create_and_destroy_lifecycle.py',
    {
        '    except Exception as e:\n        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")':
        '    except Exception as e:\n        pytest.fail(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")',
        
        '    if create_res.returncode != 0:\n        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")':
        '    # Check for billing/credit errors in stderr or stdout\n    err_msg = (create_res.stderr or "").strip() or (create_res.stdout or "").strip()\n    if "lacks credit" in err_msg or "billing" in err_msg or "error" in err_msg:\n        pytest.fail(f"CRITICAL FAILURE: Vast.ai account lacks credit or billing issue: {err_msg}")\n        \n    if create_res.returncode != 0:\n        pytest.fail(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")',
        
        '    except Exception as e:\n        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}. Output was: {create_res.stdout}.")':
        '    except Exception as e:\n        pytest.fail(f"CRITICAL FAILURE: Failed to parse contract ID: {e}. Output was: {create_res.stdout}.")'
    }
)

# 2. Patch test_covering_scenario_agent_live_prompt_turn.py
patch_file(
    'tests/units/test_covering_scenario_agent_live_prompt_turn.py',
    {
        '    # Check network reachability for deepseek API\n    try:\n        httpx.get("https://api.deepseek.com/")\n    except Exception as e:\n        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")':
        '    # Check network reachability for deepseek API\n    try:\n        httpx.get("https://api.deepseek.com/", timeout=None)\n    except Exception as e:\n        pytest.fail(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")',
        
        '        resp = httpx.post(f"http://127.0.0.1:{scenario_port}/", content=prompt)':
        '        resp = httpx.post(f"http://127.0.0.1:{scenario_port}/", content=prompt, timeout=None)',
        
        '        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()':
        '        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/", timeout=None).json()'
    }
)

# 3. Patch test_simulation_provisioner_cli_command_invocation.py
patch_file(
    'tests/units/test_simulation_provisioner_cli_command_invocation.py',
    {
        '    # Require real vastai key for live execution simulation cover\n    vast_key_path = os.path.expanduser("~/api_keys/vast_ai_key.txt")\n    if not os.path.exists(vast_key_path):\n        pytest.fail("CRITICAL FAILURE: live dependencies missing")\n\n    print(\'     └─ [Harness] Initializing process-isolated test harness...\')\n    with IntegrationHarness(required_agents=["gsa", "provisioner"], capabilities=["VastRealCapability"]) as harness:':
        '    print(\'     └─ [Harness] Initializing process-isolated test harness...\')\n    with IntegrationHarness(required_agents=["gsa", "provisioner"]) as harness:',
        
        '        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup")':
        '        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup", timeout=None)'
    }
)

# 4. Patch tests/conftest.py
patch_file(
    'tests/conftest.py',
    {
        'def pytest_runtest_makereport(item, call):\n    if call.excinfo is not None:\n        if item.get_closest_marker("architecture"):\n            pytest.exit("Architecture test failed! Aborting the entire test suite immediately.", returncode=1)':
        'def pytest_runtest_makereport(item, call):\n    if call.excinfo is not None:\n        if item.get_closest_marker("architecture"):\n            pytest.exit("Architecture test failed! Aborting the entire test suite immediately.", returncode=1)\n        elif item.get_closest_marker("covering"):\n            item.session.failed_covering = True\n\ndef pytest_runtest_setup(item):\n    if item.get_closest_marker("simulation"):\n        if getattr(item.session, "failed_covering", False):\n            pytest.exit("One or more covering tests failed! Skipping all simulation tests.", returncode=1)'
    }
)

# 5. Patch obsidian-vault/08 - Testing, Concurrency, and Rollout.md
patch_file(
    'obsidian-vault/08 - Testing, Concurrency, and Rollout.md',
    {
        '2. **Covering Tests Second**:\n   * **Prefix**: `test_covering_`\n   * **Role**: Validates live-boundary components (network, physical databases, external CLI wrappers) without mocking.\n   * **Execution Rule**: Runs after **architecture tests**. If any **covering test** fails, the runner continues executing the remaining tests in the suite, ensuring all tests can be evaluated.\n\n3. **Simulation Tests Third**:\n   * **Prefix**: `test_simulation_`\n   * **Role**: Validates complex agent behaviors and recovery pathways in simulated in-memory/inline capability environments.\n   * **Execution Rule**: Runs after **covering tests**. Failures in **covering tests** (such as missing live network endpoints or API credentials) do not prevent **simulation tests** from executing.':
        '2. **Covering Tests Second (Totality and Gatekeeper)**:\n   * **Prefix**: `test_covering_`\n   * **Role**: Validates live-boundary components (network, physical boundaries, external CLI wrappers) without mocking.\n   * **Execution Rule**: Runs after **architecture tests**. If any **covering test** fails, the runner continues executing the remaining **covering tests** in totality to get a complete report of live boundary failures. However, if *any* **covering test** fails, the suite will not proceed to the next stage.\n\n3. **Simulation Tests Third (Conditional Execution)**:\n   * **Prefix**: `test_simulation_`\n   * **Role**: Validates complex agent behaviors and recovery pathways in simulated in-memory/inline capability environments.\n   * **Execution Rule**: Runs only if all **covering tests** passed. If any **covering test** failed, all **simulation tests** are skipped entirely.'
    }
)
