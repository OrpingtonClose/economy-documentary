import os
import sys
from pathlib import Path

# Setup Python paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import get_local_mem0

print("Initializing mem0 client...")
m = get_local_mem0("scenario")
if m is None:
    print("Mem0 client is None")
    exit(0)

print("Mem0 client initialized. Running get_all...")
try:
    res = m.get_all(filters={"user_id": "scenario"})
    print("get_all response:", res)
except Exception as e:
    print("Error during get_all:", e)
