import os

path = "/Users/orpington/Documents/economy-documentary-work/.venv/lib/python3.12/site-packages/mem0/memory/main.py"
print("Reading", path)
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "migrations" in line.lower() or "qdrant" in line.lower():
        print(f"{i+1}: {line.strip()}")

path_qdrant_cfg = "/Users/orpington/Documents/economy-documentary-work/.venv/lib/python3.12/site-packages/mem0/configs/vector_stores/qdrant.py"
if os.path.exists(path_qdrant_cfg):
    print("Reading", path_qdrant_cfg)
    with open(path_qdrant_cfg, "r", encoding="utf-8") as f:
        print(f.read())
