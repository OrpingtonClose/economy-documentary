import os
import sys
import time
import tempfile
import sqlite3
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from event_store import EventStore
from projections import Jobs, Timeline, VMs, BudgetProjection, StateProjection

def test_gsa_wal_concurrency_isolation():
    print('\n▶️  [STARTING TEST] test_gsa_wal_concurrency_isolation')
    
    with tempfile.TemporaryDirectory() as db_dir:
        # 1. Initialize DB and set to WAL mode
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        db_file = os.path.join(db_dir, "events.db")
        
        # Verify WAL mode is set on SQLite DB
        conn = sqlite3.connect(db_file)
        res = conn.execute("PRAGMA journal_mode").fetchone()
        assert res[0].lower() == "wal", f"Database is not in WAL mode: {res[0]}"
        conn.close()
        
        # Spawn 5 parallel microservice subprocesses to write events concurrently using direct SQLite inserts
        writers = []
        # Python script code to run in subprocess
        writer_code = (
            "import sys, sqlite3, time, uuid, json\n"
            "db_path = sys.argv[1]\n"
            "thread_id = sys.argv[2]\n"
            "for i in range(100):\n"
            "    conn = sqlite3.connect(db_path, isolation_level=None)\n"
            "    conn.execute('PRAGMA busy_timeout=30000')\n"
            "    conn.execute('PRAGMA journal_mode=WAL')\n"
            "    conn.execute('BEGIN IMMEDIATE')\n"
            "    effect_id = str(uuid.uuid4())\n"
            "    kind = 'queue_audio_job'\n"
            "    effect_json = json.dumps({\n"
            "        'effect_id': effect_id,\n"
            "        'kind': kind,\n"
            "        'agent': f'writer_{thread_id}',\n"
            "        'timestamp': time.time(),\n"
            "        'job_id': f'job_{thread_id}_{i}',\n"
            "        'scene_num': 1,\n"
            "        'block_id': f'block_{thread_id}_{i}',\n"
            "        'slot_id': f'A1:1:block_{thread_id}_{i}',\n"
            "        'params': {}\n"
            "    })\n"
            "    conn.execute(\n"
            "        'INSERT INTO events (effect_id, kind, effect_json, otio_hash_before, agent, timestamp) VALUES (?, ?, ?, ?, ?, ?)',\n"
            "        (effect_id, kind, effect_json, '', f'writer_{thread_id}', time.time())\n"
            "    )\n"
            "    conn.execute('COMMIT')\n"
            "    conn.close()\n"
        )
        
        print("Spawning 5 parallel microservice writers...")
        for i in range(5):
            p = subprocess.Popen([sys.executable, "-c", writer_code, db_file, str(i)])
            writers.append(p)
            
        # Reconstruct projections from sequence 0 repeatedly while writes are occurring (SC-10)
        ticks_count = 0
        while any(p.poll() is None for p in writers):
            # Instantiate clean projections
            jobs = Jobs()
            timeline = Timeline()
            vms = VMs()
            budget = BudgetProjection()
            state = StateProjection()
            
            # Reconstruct from sequence 0 from physical SQLite file
            jobs.tick(event_store)
            timeline.tick(event_store)
            vms.tick(event_store)
            budget.tick(event_store)
            state.tick(event_store)
            ticks_count += 1
            time.sleep(0.05)
            
        # Wait for all processes to complete
        for p in writers:
            p.wait()
            assert p.returncode == 0, "Subprocess writer failed"
            
        # Final reconstruction verification
        jobs = Jobs()
        jobs.tick(event_store)
        events = event_store.replay()
        
        # Verify sequence numbers and database integrity (500 events + initial empty table setup)
        assert len(events) == 500, f"Expected 500 events in database, found {len(events)}"
        assert len(jobs.jobs) == 500, f"Expected 500 jobs reconstructed, found {len(jobs.jobs)}"
        print(f"✓ WAL concurrency isolation verified successfully. Reconstructed {ticks_count} times during writes.")
