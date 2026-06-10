import re
from pathlib import Path

log_path = Path("/Users/orpington/.gemini/antigravity/brain/33beca10-33a6-47ac-915f-990b840d1541/.system_generated/tasks/task-659.log")
log_content = log_path.read_text(encoding="utf-8")

blocks = log_content.split("--------------------------------------------------")

for block in blocks:
    block = block.strip()
    if not block:
        continue
    
    file_match = re.search(r"File:\s*([a-zA-Z0-9_\-\.]+)", block)
    if file_match:
        filename = file_match.group(1)
        if "FAIL" in block or "VIOLATION" in block:
            print(f"==================================================")
            print(f"FILE: {filename}")
            print(f"==================================================")
            # print first 30 lines of the report
            lines = block.split("\n")
            for line in lines[:30]:
                print(line)
            if len(lines) > 30:
                print("... [TRUNCATED] ...")
            print()
