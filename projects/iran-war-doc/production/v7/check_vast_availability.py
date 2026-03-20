#!/usr/bin/env python3
"""Check Vast.ai for available A100 80GB or similar high-VRAM GPUs."""
import json, urllib.request, urllib.parse

API_KEY = "VAST_API_KEY"

def search_offers():
    """Search for GPUs with >=45GB VRAM."""
    try:
        # vastai CLI search
        import subprocess
        result = subprocess.run(
            ["vastai", "search", "offers", "gpu_ram>=45000 rentable=true", 
             "-o", "dph_total", "--limit", "20", "--raw"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            offers = json.loads(result.stdout)
            return offers
    except Exception as e:
        print(f"Error: {e}")
    return []

offers = search_offers()
if offers:
    print(f"FOUND {len(offers)} high-VRAM GPU(s) available!")
    for o in offers[:10]:
        print(f"  {o['id']} | {o.get('num_gpus',1)}x {o.get('gpu_name','?')} | {o.get('gpu_ram',0)/1024:.0f}GB | ${o.get('dph_total',0):.3f}/hr")
else:
    print("No high-VRAM GPUs available on Vast.ai marketplace right now.")
