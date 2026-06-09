import os

file_path = "/Users/orpington/Documents/economy-documentary-work/tests/runner/run_cli.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

target = 'timeout=5.0'
replacement = 'timeout=45.0'

if target in content:
    content = content.replace(target, replacement)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("Successfully updated timeout to 45.0s")
else:
    print("Could not find timeout=5.0 in run_cli.py")
