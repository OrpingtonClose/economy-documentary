import sys
import os
import pathlib
import json

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "9a9d73f5-125e-452b-bd8f-576eaf147bf8"

from tests.runner.run_cli import run_subagent_audit, TEST_CASES

sc_tests = [tc[0] for tc in TEST_CASES if tc[2] == "Simulation Cover"]

print("# Simulation Cover Congruence Audit Report\n")
print("| Test Case | Verdict | Reasoning |")
print("|---|---|---|")

for test_name in sc_tests:
    try:
        res = run_subagent_audit(test_name)
        verdict = res.get("verdict", "FAIL")
        reasoning = res.get("reasoning", "").replace("\n", " ").replace("|", "\\|")
        v_emoji = "✅ PASS" if verdict == "PASS" else "❌ FAIL" if verdict == "FAIL" else "⚪ N/A"
        print(f"| `{test_name}` | {v_emoji} | {reasoning} |")
    except Exception as e:
        print(f"| `{test_name}` | ❌ ERROR | {str(e)} |")
