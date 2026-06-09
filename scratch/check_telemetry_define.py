path = "/Users/orpington/Documents/economy-documentary-work/.venv/lib/python3.12/site-packages/mem0/memory/telemetry.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
for i in range(min(50, len(lines))):
    print(f"{i+1}: {lines[i]}", end="")
