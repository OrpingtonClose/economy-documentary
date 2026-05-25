"""Single-unit test: Audio Agent in isolation.

NO MOCKS. Everything is real:
- Audio agent uses real DeepSeek API
- Agent reads real OTIO timeline via bash
- Agent creates real jobs in SQLite queue via bash
- We verify real jobs exist in the queue

Usage:
    python test_single_unit_audio.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import opentimelineio as otio

from pydantic_deep_agents.audio_agent import app, _startup
from fastapi.testclient import TestClient

from job_queue import clear_all_jobs, get_queue_summary


async def test_audio_agent_isolation():
    """Test audio agent alone: reads script, creates jobs."""
    print("=" * 60)
    print("SINGLE-UNIT TEST: Audio Agent (NO MOCKS)")
    print("=" * 60)

    # 1. Clear queue
    clear_all_jobs()
    print("[1/5] Queue cleared")

    # 2. Create OTIO timeline with script metadata
    with tempfile.TemporaryDirectory() as tmpdir:
        timeline_path = os.path.join(tmpdir, "documentary.otio")

        timeline = otio.schema.Timeline(name="documentary")
        stack = otio.schema.Stack(name="tracks")
        timeline.tracks = stack
        stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
        stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))

        timeline.metadata.setdefault("documentary", {})
        timeline.metadata["documentary"]["narration_v1"] = (
            "Every rainbow you've ever seen is a lie — there's no pot of gold, "
            "no end to chase. But that doesn't make it any less real."
        )
        timeline.metadata["documentary"]["narration_v2"] = (
            "A rainbow forms when sunlight refracts through water droplets at exactly "
            "42 degrees, splitting white light into the full visible spectrum."
        )
        timeline.metadata["documentary"]["narration_v3"] = (
            "Ancient cultures called it a bridge between worlds. Today, we know it's "
            "just light bending through rain. But the wonder remains."
        )
        timeline.metadata["documentary"]["visual_notes"] = (
            "Slow-motion shot of sunlight piercing storm clouds, followed by macro "
            "footage of a single raindrop catching and splitting light."
        )
        timeline.to_json_file(timeline_path)
        print(f"[2/5] OTIO timeline created: {timeline_path}")

        # 3. Start audio agent
        print("[3/5] Starting audio agent...")
        await _startup()
        client = TestClient(app)
        print("[3/5] Audio agent ready")

        # 4. Send task to agent
        print("[4/5] Sending task to audio agent...")
        task = (
            f"Generate narration audio for all scenes.\n\n"
            f"The script is stored in the OTIO timeline at:\n"
            f"{timeline_path}\n\n"
            f"Read it with this bash command:\n"
            f'python3 -c "import opentimelineio as otio; '
            f"t = otio.schema.Timeline.from_json_file('{timeline_path}'); "
            f"print(t.metadata.get('documentary', {{}}))" \"\n\n"
            f"Create one job per voice (V1, V2, V3) in the audio queue.\n"
            f"Use the EXACT narration text from the script for each voice.\n\n"
            f"After creating all jobs, report what you created:\n"
            f"Scene 1:\n"
            f"Generate narration audio for V1: [exact text]\n"
            f"Generate narration audio for V2: [exact text]\n"
            f"Generate narration audio for V3: [exact text]\n"
        )

        resp = client.post(
            "/",
            content=task,
            headers={"Content-Type": "text/plain"},
        )
        print(f"[4/5] Agent responded (status={resp.status_code}, {len(resp.text)} chars)")
        print("--- Agent output ---")
        print(resp.text[:1500])
        print("---")

        # 5. Verify jobs in queue
        print("[5/5] Verifying job queue...")
        summary = get_queue_summary("audio")
        print(f"[5/5] Queue summary: {summary}")
        total = sum(summary.values())

        if total >= 3:
            print(f"[PASS] Audio agent created {total} job(s)")
        elif total > 0:
            print(f"[PARTIAL] Audio agent created {total} job(s), expected 3")
        else:
            print("[FAIL] No jobs created")

        # Show job details
        from job_queue import get_completed_jobs, get_failed_jobs
        from job_queue import _conn
        with _conn() as conn:
            rows = conn.execute(
                "SELECT job_id, scene_num, payload, status FROM jobs WHERE stage = 'audio'"
            ).fetchall()
            for row in rows:
                import json
                payload = json.loads(row["payload"])
                print(f"  Job {row['job_id']}: scene={row['scene_num']}, "
                      f"voice={payload.get('voice', '?')}, status={row['status']}")

    clear_all_jobs()
    print("[CLEANUP] Queue cleared")


if __name__ == "__main__":
    asyncio.run(test_audio_agent_isolation())
