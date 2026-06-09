path = "/Users/orpington/Documents/economy-documentary-work/.venv/lib/python3.12/site-packages/mem0/memory/main.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
for i in range(359, 420):
    if i < len(lines):
        print(f"{i+1}: {lines[i]}", end="")
