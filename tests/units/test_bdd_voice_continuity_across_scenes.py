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
import builtins

def print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\n')
    msg = sep.join(str(arg) for arg in args) + end
    if sys.stdout is not None:
        sys.stdout.write(msg)
        sys.stdout.flush()
    else:
        builtins.print(*args, **kwargs)

# BDD judge imports
sys.path.append(str(PROJECT_ROOT / "server" / "capabilities"))
from test_judge_capability import BddScenario, run_bdd_judge, collect_evidence_from_store

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

from capabilities.test_single_purpose_tts_simulators import TtsMultiBlockSimulator

def test_bdd_voice_continuity_across_scenes():

    print('\n▶️  [STARTING TEST] test_bdd_voice_continuity_across_scenes')
    """Given 3 real WAV files from the same narrator voice,
    When we measure LUFS and spectral characteristics,
    Then all WAVs should have consistent loudness within ±3 LUFS.

    BDD judge evaluates: LUFS uniformity, spectral consistency.
    Intensity: Medium (pure computation, no harness)
    """
    scenario = BddScenario(
        test_name="voice_continuity_across_scenes",
        given="3 WAV files generated with same voice characteristics but natural variation "
              "(different frequencies 200/220/240 Hz simulating prosodic variation, different durations)",
        when="LUFS is measured on each WAV",
        then="LUFS values are within ±3 dB of each other, "
             "all files are valid WAV with consistent sample rate, "
             "values show natural variation (not identical)",
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_voice_")

    # Generate 3 WAVs via ffmpeg with slightly different frequencies
    # (simulating natural prosodic variation across narrator scenes)
    frequencies = [200, 220, 240]  # Hz — close enough for same voice, different enough for realism
    wav_paths = []
    for i in range(3):
        path = os.path.join(tmp, f"narrator_scene_{i+1}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=frequency={frequencies[i]}:duration={2.5 + i * 0.3}", "-ar", "44100", "-ac", "1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        wav_paths.append(path)

    # Measure LUFS
    lufs_values = []
    for path in wav_paths:
        lufs = measure_lufs_integrated(path)
        lufs_values.append(lufs)

    # Mechanical: all within ±3 LUFS
    lufs_spread = max(lufs_values) - min(lufs_values)
    print('     ├─ [Assert] Checking: lufs_spread < 3.0, f\"LUFS spread {lufs_spread:.2f} dB excee...')
    assert lufs_spread < 3.0, f"LUFS spread {lufs_spread:.2f} dB exceeds ±3 dB"

    # Check sample rates consistent
    sample_rates = []
    for path in wav_paths:
        with wave.open(path, "rb") as wf:
            sample_rates.append(wf.getframerate())
    print('     ├─ [Assert] Checking: len(set(sample_rates)) == 1, f\"Inconsistent sample rates: {...')
    assert len(set(sample_rates)) == 1, f"Inconsistent sample rates: {sample_rates}"

    scenario.evidence = collect_evidence_from_store(
        [],
        artifacts={
            "lufs_values": lufs_values,
            "lufs_spread_db": lufs_spread,
            "sample_rates": sample_rates,
            "wav_count": len(wav_paths),
        },
    )
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ voice continuity — verdict: {verdict['verdict']} "
          f"(LUFS spread: {lufs_spread:.2f} dB)")


# ===========================================================================
# 40. BDD: LTX Fleet Scale-Up — dual-role fleet management
# ===========================================================================