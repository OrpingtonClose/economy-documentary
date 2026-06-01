import subprocess
import re

def main():
    print("Finding all objects matching test files in git history...")
    cmd = ["git", "rev-list", "--objects", "--all"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("Failed to run git rev-list:", result.stderr)
        return

    lines = result.stdout.strip().split("\n")
    targets = [
        "test_scenario_bdd.py",
        "test_real_vast_provisioning_bdd.py",
        "test_real_video_provisioner_bdd.py",
        "test_video_provisioner_bdd.py",
        "test_simple_bdd.py",
        "test_agent_base.py"
    ]

    found = []
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        obj_hash, path = parts
        # Check if path ends with any target
        for target in targets:
            if path.endswith(target):
                found.append((obj_hash, path))
                break

    print(f"Found {len(found)} candidate objects in history. Checking for uncorrupted versions...")
    for obj_hash, path in found:
        # Get content
        content_cmd = ["git", "cat-file", "-p", obj_hash]
        content_res = subprocess.run(content_cmd, capture_output=True, text=True)
        if content_res.returncode == 0:
            content = content_res.stdout
            is_corrupted = content.startswith('"import') or "<truncated" in content
            snippet = content[:60].replace("\n", "\\n")
            print(f"Hash: {obj_hash} | Path: {path} | Corrupted: {is_corrupted} | Len: {len(content)} | Snippet: {snippet}")
            if not is_corrupted and len(content) > 100:
                print(">>> UNCORRUPTED VERSION FOUND!")
                # print(content[:500])

if __name__ == "__main__":
    main()
