"import sys
from pathlib import Path

# Add tests/mocks directory to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from mock_base import create_mock_agent_app
from effects import QueueJob, DurationAdjusted, ReconciliationComplete

async def audio_turn(run_id, state, event_store):
    slots = state.get("otio", {}).get("slots", {})
    jobs = state.get("jobs", {}).get("jobs", {})
    
    dirty_slots = [addr for addr, s in slots.items() if s["status"] == "scripted"]
    tts_jobs = [j for j in jobs.values() if j["job_type"] == "tts" and j["slot_id"] in dirty_slots]
    
    if dirty_slots:
        addr = dirty_slots[0]
        slot = slots[addr]
        # Check if we already queued TTS job
        job_for_slot = next((j for j in tts_jobs if j["slot_id"] == addr), None)
        if not job_for_slot:
            return QueueJob(
                run_id=run_id,
                agent="audio",
                job_id="tts_job_intro",
                job_type="tts",
                scene_num=slot["scene_num"],
                block_id=slot["block_id"],
                slot_id=addr,
                params={"voice": slot["speaker"], "text": slot["text"]}
            )
        elif job_for_slot["status"] == "completed":
            return DurationAdjusted(
                run_id=run_id,
                agent="audio",
                block_id=slot["block_id"],
                slot_id=addr,
                scene_num=slot["scene_num"],
                voice_role=slot["speaker"],
                scripted_sec=slot["scripted_sec"],
                measured_sec=job_for_slot["duration_sec"]
            )
    else:
        # Check if ReconciliationComplete has already been emitted
        reconciliation_complete = state.get("jobs", {}).get("reconciliation_complete", False)
        if not reconciliation_complete and slots:
            return ReconciliationComplete(
                run_id=run_id,
         
<truncated 309 bytes>