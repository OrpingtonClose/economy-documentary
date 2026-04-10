#!/usr/bin/env python3
"""
Event Replay — reconstruct pipeline state from dashboard event store.

Usage:
    python scripts/replay_pipeline.py --run-id <run_id> [--db-path <path>]
    python scripts/replay_pipeline.py --list-runs [--db-path <path>]

This is useful for post-mortem analysis of pipeline runs:
  - Reconstruct the timeline of events
  - Identify slow phases or failed tool calls
  - Generate HTML reports from historical data
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB = os.environ.get(
    "DASHBOARD_DB_DIR", "/tmp/documentary-pipeline/dashboard"
)


def list_runs(db_path: str) -> None:
    """List all pipeline runs in the event store."""
    db_file = os.path.join(db_path, "pipeline_events.db")
    if not os.path.exists(db_file):
        print(f"No database found at {db_file}")
        return

    conn = sqlite3.connect(db_file)
    cursor = conn.execute(
        "SELECT run_id, topic, status, created_at, finished_at FROM runs ORDER BY created_at DESC LIMIT 20"
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No runs found.")
        return

    print(f"{'Run ID':<40} {'Topic':<20} {'Status':<12} {'Created':<20} {'Finished':<20}")
    print("-" * 112)
    for row in rows:
        run_id, topic, status, created, finished = row
        print(f"{run_id:<40} {(topic or '-'):<20} {(status or 'running'):<12} {(created or '-'):<20} {(finished or '-'):<20}")


def replay_run(db_path: str, run_id: str) -> None:
    """Replay events for a specific pipeline run."""
    db_file = os.path.join(db_path, "pipeline_events.db")
    if not os.path.exists(db_file):
        print(f"No database found at {db_file}")
        return

    conn = sqlite3.connect(db_file)

    # Get run info
    cursor = conn.execute(
        "SELECT run_id, topic, status, created_at, finished_at, metadata FROM runs WHERE run_id = ?",
        (run_id,),
    )
    run = cursor.fetchone()
    if not run:
        print(f"Run not found: {run_id}")
        conn.close()
        return

    print(f"\n=== Pipeline Run: {run[0]} ===")
    print(f"Topic: {run[1] or '-'}")
    print(f"Status: {run[2] or 'running'}")
    print(f"Created: {run[3]}")
    print(f"Finished: {run[4] or '-'}")

    if run[5]:
        try:
            metadata = json.loads(run[5])
            print(f"Metadata: {json.dumps(metadata, indent=2)}")
        except json.JSONDecodeError:
            pass

    # Get events
    cursor = conn.execute(
        "SELECT event_type, event_data, created_at FROM events WHERE run_id = ? ORDER BY created_at ASC",
        (run_id,),
    )
    events = cursor.fetchall()
    conn.close()

    if not events:
        print("\nNo events recorded.")
        return

    print(f"\n--- Events ({len(events)} total) ---\n")
    for event_type, event_data, created_at in events:
        data = {}
        if event_data:
            try:
                data = json.loads(event_data)
            except json.JSONDecodeError:
                pass

        timestamp = created_at or "?"
        detail = ""

        if event_type == "phase_start":
            detail = f"Phase started: {data.get('name', '?')}"
        elif event_type == "phase_end":
            detail = f"Phase ended: {data.get('name', '?')} ({data.get('status', '?')})"
        elif event_type == "tool_start":
            detail = f"Tool: {data.get('tool_name', '?')} ({data.get('agent', '?')})"
        elif event_type == "tool_end":
            detail = f"Tool done: {data.get('tool_name', '?')} ({data.get('duration', 0):.1f}s)"
        elif event_type == "llm_start":
            detail = f"LLM call: {data.get('agent', '?')}"
        elif event_type == "llm_end":
            detail = f"LLM done: {data.get('agent', '?')} ({data.get('duration', 0):.1f}s)"
        elif event_type == "force_end":
            detail = f"FORCE END: {data.get('reason', 'context limit')}"
        else:
            detail = f"{event_type}: {json.dumps(data)[:100]}"

        print(f"  [{timestamp}] {detail}")


def main():
    parser = argparse.ArgumentParser(description="Replay pipeline events")
    parser.add_argument("--list-runs", action="store_true", help="List all runs")
    parser.add_argument("--run-id", type=str, help="Run ID to replay")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB, help="Path to dashboard DB directory")
    args = parser.parse_args()

    if args.list_runs:
        list_runs(args.db_path)
    elif args.run_id:
        replay_run(args.db_path, args.run_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
