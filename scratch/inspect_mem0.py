import os
import sys

# Print python path
print("Python executable:", sys.executable)
print("Python path:", sys.path)

try:
    from mem0 import Memory
    print("Mem0 imported successfully")
except Exception as e:
    print("Failed to import mem0:", e)
