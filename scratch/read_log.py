"import ast
from pathlib import Path

log_path = Path("/Users/orpington/.gemini/antigravity/brain/0550465d-2f73-41d5-a145-289052189570/.system_generated/tasks/task-1432.log")
if not log_path.exists():
    print("Log file does not exist")
    exit()

content = log_path.read_text()

idx = content.rfind("'json_data':")
if idx == -1:
    print("Could not find 'json_data' in log")
    exit()

start = content.find('{', idx)
if start == -1:
    print("Could not find opening brace after 'json_data'")
    exit()

# Parse braces while respecting strings and escapes
brace_count = 0
in_str = None  # None, "'" or '"'
escape = False
dict_str = ""

for i in range(start, len(content)):
    char = content[i]
    
    if escape:
        escape = False
    elif char == '\\':
        escape = True
    elif in_str:
        if char == in_str:
            in_str = None
    elif char in ("'", '"'):
        in_str = char
    elif char == '{':
        brace_count += 1
    elif char == '}':
        brace_count -= 1
        
    if brace_count == 0:
        dict_str = content[start:i+1]
        break

if not dict_str:
    print("Could not find matching closing brace")
    exit()

try:
    data = ast.literal_eval(dict_str)
    messages = data.get("messages", [])
    print(f"Total messages in conversation: {len(messages)}")
    
    start_idx = max(0, len(messages) - 5)
    for index in range(start_idx, len(messages)):
        msg = messages[index]
        role = msg.get("role")
        content_val = msg.get("content")
        tool_calls = msg.get("tool_calls")
        
        print(f"\
=====================================")
        print(f"Message {index+1} [{role.upper()}]")
        print(f"=====================================")
        
        if content_val:
            print(content_val)
        if tool_calls:
            print("Tool Calls:")
            for tc in tool_calls:
                func = tc.get('function', {})

<truncated 203 bytes>