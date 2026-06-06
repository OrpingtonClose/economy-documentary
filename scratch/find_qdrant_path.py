import mem0
import os
import inspect

mem0_dir = os.path.dirname(mem0.__file__)
print("mem0 package directory:", mem0_dir)

# Search for qdrant config or migrations_qdrant in mem0 package
import glob
for path in glob.glob(os.path.join(mem0_dir, "**/*.py"), recursive=True):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            if "qdrant" in content.lower():
                print("Found qdrant in:", path)
            if "migrations" in content.lower():
                print("Found migrations in:", path)
    except Exception as e:
        pass
