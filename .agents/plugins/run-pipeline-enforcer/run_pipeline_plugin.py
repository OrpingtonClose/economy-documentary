#!/usr/bin/env python3
import sys
import argparse
import os

# ==========================================
# ON-OFF SWITCH
# ==========================================
ENABLED = False

def main():
    if not ENABLED:
        print("✅ Run-Pipeline Enforcer: DISABLED. PASS.")
        sys.exit(0)

    parser = argparse.ArgumentParser(description="Enforces strict run-pipeline endpoints-only constraint")
    parser.add_argument("--tool", required=True, help="Tool name being executed")
    parser.add_argument("--command", help="CommandLine argument for run_command")
    parser.add_argument("--file", help="TargetFile argument for file writes/edits")

    args = parser.parse_args()

    if args.tool == "run_command":
        cmd = args.command or ""
        
        # Must be a curl command or sleep command
        is_curl = "curl " in cmd
        is_sleep = cmd.strip().startswith("sleep ")
        
        # Disallow structural commands
        forbidden_keywords = ["python", "sqlite3", "git ", "pip ", "kill ", "lsof ", "vastai ", "ps ", "rm ", "mv "]
        has_forbidden = any(kw in cmd for kw in forbidden_keywords)
        
        if not (is_curl or is_sleep) or has_forbidden:
            print(f"❌ ERROR: Command violates the strict endpoints-only constraint!")
            print(f"Command attempted: {cmd}")
            print(f"You are ONLY permitted to use curl requests to query and trigger agent endpoints.")
            sys.exit(1)
            
    elif args.tool in ["write_to_file", "replace_file_content", "multi_replace_file_content"]:
        file_path = args.file or ""
        normalized_path = os.path.abspath(file_path)
        
        project_root = "/Users/orpington/Documents/economy-documentary-work"
        allowed_path = os.path.abspath(os.path.join(project_root, "failures.md"))
        
        if normalized_path != allowed_path:
            print(f"❌ ERROR: File write/edit violates the strict perimeter constraints!")
            print(f"File targeted: {file_path}")
            print(f"You are ONLY allowed to write or edit a single comments file: failures.md")
            sys.exit(1)

    print(f"✅ Run-Pipeline Enforcer: Action approved under tool '{args.tool}'.")
    sys.exit(0)

if __name__ == "__main__":
    main()
