#!/usr/bin/env python3
"""Rent 4 RTX 5090 VMs on Vast.ai for LTX-2.3 video generation"""

import json
import subprocess
import time

API_KEY = "${VAST_API_KEY}"

# Selected VMs - RTX 5090 with good disk space and fast internet
VM_IDS = [
    32233781,  # $0.296/hr, 751GB disk, 1380Mbps
    32862969,  # $0.330/hr, 1242GB disk, 858Mbps  
    32502630,  # $0.336/hr, 1552GB disk, 878Mbps
    32555318,  # $0.336/hr, 710GB disk, 776Mbps
]

def rent_vm(offer_id):
    """Rent a VM from Vast.ai"""
    payload = {
        "client_id": "me",
        "image": "nvidia/cuda:12.4.0-devel-ubuntu22.04",
        "disk": 100,  # 100GB disk
        "onstart": "apt-get update && apt-get install -y git python3-pip ffmpeg wget curl && pip3 install torch torchvision --index-url https://download.pytorch.org/whl/cu124",
    }
    
    cmd = [
        "curl", "-sL", "-X", "PUT",
        f"https://console.vast.ai/api/v0/asks/{offer_id}/",
        "-H", f"Authorization: Bearer {API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": result.stdout, "stderr": result.stderr}

results = []
for offer_id in VM_IDS:
    print(f"Renting VM {offer_id}...")
    result = rent_vm(offer_id)
    results.append({"offer_id": offer_id, "result": result})
    print(f"  Result: {json.dumps(result)[:200]}")
    time.sleep(1)

# Save results
with open("/home/user/workspace/vm_rentals.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nAll VMs rented. Results saved to vm_rentals.json")
