import os
import sys
import subprocess
from pathlib import Path

def test_provisioner_vast_offers_search():
    print('\n▶️  [STARTING TEST] test_provisioner_vast_offers_search')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key file is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")
        
    # Run the real search command (SC-02 & SC-07)
    cmd = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    
    assert res.returncode == 0, f"vastai command failed with code {res.returncode}: {res.stderr}"
    output = res.stdout
    assert "GPU_name" in output or "GPU" in output or "Price" in output
    
    # Parse lines to check for valid prices and GPUs
    lines = output.strip().split("\n")
    found_offer = False
    for line in lines:
        parts = line.split()
        if len(parts) >= 8 and parts[0].isdigit():
            try:
                price = float(parts[-1])
                gpu_name = parts[2]
                found_offer = True
            except ValueError:
                continue
    assert found_offer, "Could not parse any valid offers from vastai output"
    print("✓ Vast.ai offers search verified successfully.")
