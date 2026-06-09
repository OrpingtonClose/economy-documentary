import re
from pathlib import Path

def fix_budget_limit():
    filepath = Path("tests/units/test_budget_limit_aborted_gate.py")
    if not filepath.exists():
        print(f"File not found: {filepath}")
        return
    
    content = filepath.read_text(encoding="utf-8")
    
    # Target:
    #         if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:
    #             import pytest
    #             pytest.skip("Vast.ai account lacks credit; skipping live VM lease budget gate test.")
    
    target = (
        '        if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:\n'
        '            import pytest\n'
        '            pytest.skip("Vast.ai account lacks credit; skipping live VM lease budget gate test.")'
    )
    
    replacement = (
        '        if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:\n'
        '            raise RuntimeError("Vast.ai account lacks credit; aborting live VM lease budget gate test.")'
    )
    
    if target in content:
        new_content = content.replace(target, replacement)
        filepath.write_text(new_content, encoding="utf-8")
        print(f"Successfully updated {filepath}")
    else:
        print(f"Target pattern not found in {filepath}")

def fix_lifecycle():
    filepath = Path("tests/units/test_vast_create_and_destroy_lifecycle.py")
    if not filepath.exists():
        print(f"File not found: {filepath}")
        return
    
    content = filepath.read_text(encoding="utf-8")
    
    # Target:
    #         if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:
    #             import pytest
    #             pytest.skip("Vast.ai account lacks credit; skipping live VM lease lifecycle test.")
    
    target = (
        '        if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:\n'
        '            import pytest\n'
        '            pytest.skip("Vast.ai account lacks credit; skipping live VM lease lifecycle test.")'
    )
    
    replacement = (
        '        if "lacks credit" in create_res.stderr or "billing" in create_res.stderr:\n'
        '            raise RuntimeError("Vast.ai account lacks credit; aborting live VM lease lifecycle test.")'
    )
    
    if target in content:
        new_content = content.replace(target, replacement)
        filepath.write_text(new_content, encoding="utf-8")
        print(f"Successfully updated {filepath}")
    else:
        print(f"Target pattern not found in {filepath}")

if __name__ == "__main__":
    fix_budget_limit()
    fix_lifecycle()
