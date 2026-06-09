import glob
import os
import re

tests_dir = "/Users/orpington/Documents/economy-documentary-work/tests/units"

# Find all test files
test_files = glob.glob(os.path.join(tests_dir, "test_*.py"))
# Also check max capacity pipeline
test_files.append(os.path.join(tests_dir, "test_max_capacity_pipeline.py"))

for file_path in test_files:
    if not os.path.exists(file_path):
        continue
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    modified = False
    
    # We want to match: httpx.post(f"http://127.0.0.1:{...}/", content=...)
    # But only if it doesn't already contain timeout=
    # Let's find all httpx.post lines
    pattern = r'(httpx\.post\(\s*f?"http://127\.0\.0\.1:[^"\n]+"\s*,\s*content=[^,\n\)]+)(\))'
    
    # Let's do a findall to see what matches
    matches = re.findall(pattern, content)
    if matches:
        # Replace matches with timeout=None
        new_content = re.sub(pattern, r'\1, timeout=None\2', content)
        if new_content != content:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"Updated: {os.path.basename(file_path)}")
            modified = True
            
print("Timeout injection complete.")
