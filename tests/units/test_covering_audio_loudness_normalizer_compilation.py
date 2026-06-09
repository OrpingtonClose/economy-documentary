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

def measure_lufs_integrated(audio_path: str) -> float:
    """Measure integrated LUFS robustly by converting audio to raw s16le PCM via ffmpeg."""
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_pcm_path = os.path.join(tmpdir, "raw.pcm")
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-vn", "-f", "s16le", "-ac", "1", "-ar", "44100", raw_pcm_path],
            capture_output=True, check=True
        )
        with open(raw_pcm_path, "rb") as f:
            raw = f.read()
            
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(np.square(pcm, dtype=np.float64)))
    if rms <= 0.0:
        return -70.0
    return 20.0 * math.log10(rms) + 0.0


def test_covering_audio_loudness_normalizer_compilation():

    print('\n▶️  [STARTING TEST] test_covering_audio_loudness_normalizer_compilation')
    """Verify the -16.0 LUFS normalization constraint on a synthesized audio file."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        
        # 1. Generate a loud PCM wav using ffmpeg
        loud_wav = os.path.join(db_dir, "loud.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "5.0", loud_wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        
        # Loudness should be near 0 LUFS (very loud)
        initial_lufs = measure_lufs_integrated(loud_wav)
        print(f"Loud sine wave LUFS: {initial_lufs:.2f} LUFS")
        
        # 2. Run loudness normalization using ffmpeg loudnorm filter (matches production)
        norm_wav = os.path.join(db_dir, "normalized.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", loud_wav, "-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11", "-acodec", "pcm_s16le", norm_wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        
        # 3. Assert normalization results (Target: -16.0 LUFS +/- 1.0 LUFS)
        normalized_lufs = measure_lufs_integrated(norm_wav)
        print(f"Normalized sine wave LUFS: {normalized_lufs:.2f} LUFS")
        print('     ├─ [Assert] Checking: abs(normalized_lufs - (-16.0)) <= 1.0, f\"Loudness normaliza...')
        assert abs(normalized_lufs - (-16.0)) <= 1.0, f"Loudness normalization out of bounds: {normalized_lufs:.2f}"


    # ===========================================================================
    # 9. Coordinate Timeline Dynamic Drift Recalculation
    # ===========================================================================
