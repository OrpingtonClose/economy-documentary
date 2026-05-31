"import json
import re
from pathlib import Path

transcript_path = Path("/Users/orpington/.gemini/antigravity/brain/0550465d-2f73-41d5-a145-289052189570/.system_generated/logs/transcript.jsonl")
repo_root = Path("/Users/orpington/Documents/economy-documentary-work")

if not transcript_path.exists():
    print(f"Error: {transcript_path} does not exist.")
    exit(1)

# Map from file path to current content
files_state = {}

def clean_arg(val):
    if not isinstance(val, str):
        return val
    # Clean surrounding quotes if any
    if val.startswith('"') and val.endswith('"'):
        try:
            val = json.loads(val)
        except Exception:
            pass
    elif val.startswith("'") and val.endswith("'"):
        val = val[1:-1]
    
    # Unescape common JSON characters
    val = val.replace('\\
', '\
').replace('\\	', '\	').replace('\\"', '"').replace('\\\\', '\\')
    return val

print("Replaying transcript file actions...")
with open(transcript_path, "r", encoding="utf-8") as f:
    for line_num, line in enumerate(f):
        try:
            data = json.loads(line)
            step_idx = data.get("step_index")
            tool_calls = data.get("tool_calls") or []
            for call in tool_calls:
                name = call.get("name")
                args = call.get("args") or {}
                
                # Check target file
                target_file = args.get("TargetFile") or args.get("target_file")
                if not target_file:
                    continue
                
                target_file = clean_arg(target_file).strip()
                # We only want to restore files in tests/units/ or tests/mocks/
                if not ("tests/units/" in target_file or "tests/mocks/" in target_file):
                    continue
                
                if name == "write_to_file":
                    code = args.get("CodeContent") or args.get("code_content") or 
<truncated 4196 bytes>