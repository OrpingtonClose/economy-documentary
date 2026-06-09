import os
import sys
import time
import wave
import math
import httpx
import pytest
import subprocess
import numpy as np
import asyncio
from pathlib import Path

# Setup Python paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved,
    VMAllocated, VMDeallocated, VMObserved, VMProvisionFailed,
    DurationAdjusted, ReconciliationComplete, ReconciliationFailed,
    MergeIntoOTIO, DeleteScene, DeleteFromOTIO, ReorderScenes,
    AudioMeasured, AudioGenerated, NoOp, HumanInstruction,
    AgentLoopDetected, MeasurementRequested, VideoMeasured,
    ProductionFailed, SuggestedFix,
    parse_duration, Effect, KIND_TO_MODEL, EffectUnion,
)
from projections import (
    Timeline, Jobs, VMs, BudgetProjection, StateProjection,
    JobState, VMRecord,
)
from coordinate_timeline import CoordinateTimeline, IntervalSpan


# BDD judge imports
sys.path.append(str(PROJECT_ROOT / "server" / "capabilities"))



def test_simulation_assemble_final_cut_execution():

    print('\n▶️  [STARTING TEST] test_assemble_final_cut_execution')
    """Verify that run_movie_assembly compiles a movie from an OTIO timeline with placeholders."""
    import opentimelineio as otio
    from agent_base import run_movie_assembly
    
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Build a simple OTIO timeline
        timeline = otio.schema.Timeline(name="test_timeline")
        video_track = otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video)
        audio_track = otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio)
        
        # Add clips
        v_clip = otio.schema.Clip(
            name="v_clip_1",
            media_reference=otio.schema.ExternalReference(target_url="placeholder_video.mp4"),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(72, 24) # 3.0s
            )
        )
        video_track.append(v_clip)
        
        a_clip = otio.schema.Clip(
            name="a_clip_1",
            media_reference=otio.schema.ExternalReference(target_url="placeholder_audio.wav"),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(72, 24) # 3.0s
            )
        )
        audio_track.append(a_clip)
        
        timeline.tracks.append(video_track)
        timeline.tracks.append(audio_track)
        
        timeline_path = os.path.join(db_dir, "test.otio")
        otio.adapters.write_to_file(timeline, timeline_path)
        
        output_mp4 = os.path.join(db_dir, "output.mp4")
        
        # Execute actual run_movie_assembly
        result = run_movie_assembly(
            output_path=output_mp4,
            timeline_path=timeline_path,
            include_placeholders=True,
            target_duration=3.0,
            event_store_instance=event_store,
            log_dir=db_dir
        )
        
        # Verify output mp4 was generated and is valid
        print('     ├─ [Assert] Checking: os.path.exists(output_mp4)')
        assert os.path.exists(output_mp4)
        print('     ├─ [Assert] Checking: os.path.getsize(output_mp4) > 0')
        assert os.path.getsize(output_mp4) > 0

        # Probe parameters using ffprobe (checks real functionality)
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", output_mp4],
            capture_output=True, text=True, check=True
        )
        compiled_duration = float(res.stdout.strip())
        print('     ├─ [Assert] Checking: abs(compiled_duration - 3.0) < 0.5, f\"Expected movie durati...')
        assert abs(compiled_duration - 3.0) < 0.5, f"Expected movie duration near 3.0s, got {compiled_duration:.2f}s"

        res_audio = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_mp4],
            capture_output=True, text=True, check=True
        )
        audio_stream_codec = res_audio.stdout.strip()
        print('     ├─ [Assert] Checking: audio_stream_codec != \"\", \"Compiled movie container is mi...')
        assert audio_stream_codec != "", "Compiled movie container is missing an audio stream track"


# ===========================================================================
# 17. Provisioner CLI Command Invocation (Strict Mock-Free Integration check)
# ===========================================================================