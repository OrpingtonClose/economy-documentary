#!/usr/bin/env python3
"""
Debug-Gym Infused Pipeline Runner.

Runs the debug-gym static contract verifier BEFORE starting the pipeline,
then launches the pipeline with real-time audit monitoring.

If debug-gym finds critical errors, the pipeline is blocked.
If debug-gym passes (or only has warnings), the pipeline proceeds.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def run_debug_gym(repo_root: str) -> tuple[bool, list[str], list[str]]:
    """Run debug-gym static verifier. Returns (passed, errors, warnings)."""
    print("=" * 60)
    print("DEBUG-GYM STATIC VERIFICATION")
    print("=" * 60)
    
    env = os.environ.copy()
    env["DEBUG_GYM_REPO_ROOT"] = repo_root
    
    proc = subprocess.run(
        [sys.executable, os.path.join(repo_root, "server", "debug_gym_entrypoint.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )
    
    # Print output
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    
    # Parse results from output
    errors = []
    warnings = []
    for line in (proc.stdout + proc.stderr).split("\n"):
        if line.startswith("❌") or ("ERROR" in line and "contract" not in line.lower()):
            errors.append(line.strip())
        elif line.startswith("⚠️") or "WARNING" in line:
            warnings.append(line.strip())
    
    passed = proc.returncode == 0
    return passed, errors, warnings


def run_pipeline(brief: str, api_key: str, model: str, budget: float, 
                 max_nodes: int, output_dir: str) -> int:
    """Run the pipeline with audit. Returns exit code."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.join(script_dir, "..")
    
    audit_script = os.path.join(script_dir, "run_pipeline_with_audit.py")
    
    cmd = [
        sys.executable, audit_script,
        brief,
        "--api-key", api_key,
        "--model", model,
        "--budget", str(budget),
        "--max-nodes", str(max_nodes),
        "--output-dir", output_dir,
    ]
    
    print("\n" + "=" * 60)
    print("PIPELINE RUN")
    print("=" * 60)
    print(f"Brief: {brief[:60]}")
    print(f"Budget: ${budget:.2f}")
    print(f"Max nodes: {max_nodes}")
    print(f"Output: {output_dir}")
    print()
    
    proc = subprocess.run(cmd)
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Debug-Gym infused pipeline runner")
    parser.add_argument("brief", nargs="+", help="Documentary brief")
    parser.add_argument("--api-key", "-k", required=True, help="LLM API key")
    parser.add_argument("--model", "-m", default="deepseek-chat", help="Model ID")
    parser.add_argument("--budget", "-b", type=float, default=15.0, help="Budget USD")
    parser.add_argument("--output-dir", "-o", default="/tmp/documentary-pipeline", help="Output directory")
    parser.add_argument("--max-nodes", type=int, default=200, help="Max node executions")
    parser.add_argument("--skip-debug-gym", action="store_true", help="Skip debug-gym pre-check")
    parser.add_argument("--warn-only", action="store_true", help="Run pipeline even if debug-gym errors (warn only)")
    
    args = parser.parse_args()
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.join(script_dir, "..")
    
    brief = " ".join(args.brief)
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    
    # ── Phase 1: Debug-Gym Static Verification ──
    if not args.skip_debug_gym:
        passed, errors, warnings = run_debug_gym(repo_root)
        
        if errors and not args.warn_only:
            print(f"\n❌ DEBUG-GYM BLOCKED: {len(errors)} critical error(s)")
            print("Use --warn-only to run anyway, or fix the errors above.")
            sys.exit(1)
        
        if errors:
            print(f"\n⚠️  DEBUG-GYM WARNINGS: {len(errors)} error(s) ignored (--warn-only)")
    else:
        print("⚠️  Debug-gym pre-check skipped (--skip-debug-gym)")
    
    # ── Phase 2: Pipeline Run with Audit ──
    print(f"\n🚀 Starting pipeline at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    exit_code = run_pipeline(
        brief=brief,
        api_key=args.api_key,
        model=args.model,
        budget=args.budget,
        max_nodes=args.max_nodes,
        output_dir=output_dir,
    )
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
