import sys
import subprocess
from pathlib import Path

def test_checker():
    target_file = Path("tests/units/test_perplexity_verify_live.py")
    backup_file = Path("tests/units/test_perplexity_verify_live.py.bak")
    
    if not target_file.exists():
        print("Error: Target file not found!")
        return

    # 1. Back up the target file
    content_orig = target_file.read_text(encoding="utf-8")
    backup_file.write_text(content_orig, encoding="utf-8")
    print("✓ Created backup of test_perplexity_verify_live.py")

    try:
        # 2. Inject violations
        violating_code = content_orig + "\n\n" + (
            "def dummy_violation_function():\n"
            "    import unittest.mock\n"
            "    pytest.skip('Deliberate test skip for verification')\n"
            "    assert 1 == 1\n"
        )
        target_file.write_text(violating_code, encoding="utf-8")
        print("✓ Injected architecture violations (unittest.mock, pytest.skip, trivial assertion).")

        # 3. Run checker
        print("\n--- Running Architecture Checker on Violating File ---")
        cmd = [".venv/bin/python", "-c", "from tests.runner.run_cli import run_architecture_test; run_architecture_test()"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        
        print(f"Exit code: {res.returncode}")
        print(f"Stdout:\n{res.stdout}")
        print(f"Stderr:\n{res.stderr}")
        print("------------------------------------------------------\n")
        
        if res.returncode != 0 and "Forbidden mock import" in res.stdout and "Forbidden skip call" in res.stdout and "Forbidden trivial assertion" in res.stdout:
            print("🎉 SUCCESS: The Architecture Test checker caught all injected violations correctly!")
        else:
            print("❌ FAILURE: The Architecture Test checker failed to catch some or all violations.")
            
    finally:
        # 4. Restore target file from backup
        if backup_file.exists():
            target_file.write_text(backup_file.read_text(encoding="utf-8"), encoding="utf-8")
            backup_file.unlink()
            print("✓ Restored test_perplexity_verify_live.py to original state.")

if __name__ == "__main__":
    test_checker()
