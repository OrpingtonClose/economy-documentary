import sys
from pathlib import Path

# Add tests/mocks directory to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from mock_base import create_mock_agent_app
from effects import QueueJob, MergeIntoOTIO

async def video_turn(run_id, state, event_store):
    slots = state.get("otio", {}).get("slots", {})
    jobs = state.get("jobs", {}).get("jobs", {})
    reconciled = state.get("jobs", {}).get("reconciliation_complete", False)
    
    if reconciled:
        video_slots = [addr for addr, s in slots.items() if s["status"] in ("measured", "delivered")]
        ltx_jobs = [j for j in jobs.values() if j["job_type"] == "ltx" and j["slot_id"] in video_slots]
        
        if video_slots:
            addr = video_slots[0]
            slot = slots[addr]
            job_for_slot = next((j for j in ltx_jobs if j["slot_id"] == addr), None)
            if not job_for_slot:
                return QueueJob(
                    run_id=run_id,
                    agent="video",
                    job_id="ltx_job_intro",
                    job_type="ltx",
                    scene_num=slot["scene_num"],
                    block_id=slot["block_id"],
                    slot_id=addr,
                    params={"prompt": "Skyscrapers showing financial growth."}
                )
            elif job_for_slot["status"] == "completed" and slot["status"] != "delivered":
                return MergeIntoOTIO(
                    run_id=run_id,
                    agent="video",
                    job_id=job_for_slot["job_id"],
                    block_id=slot["block_id"],
                    scene_num=slot["scene_num"],
                    slot_id=addr,
                    artifact_uri="/tmp/video.mp4",
                    track_name="V1_Video",
                    duration_sec=6.8
                )
    return None

app = create_mock_agent_app("video", video_turn)
