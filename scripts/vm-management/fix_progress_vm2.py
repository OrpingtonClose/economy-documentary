#!/usr/bin/env python3
import json
progress = {"completed": ["clip168", "clip169"], "failed": []}
json.dump(progress, open("/root/v6_progress.json", "w"), indent=2)
print(f"Fixed progress: {len(progress['completed'])} completed")
