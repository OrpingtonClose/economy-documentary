path = "/Users/orpington/Documents/economy-documentary-work/.venv/lib/python3.12/site-packages/mem0/memory/main.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "mem0_dir" in line:
        print(f"{i+1}: {line.strip()}")
