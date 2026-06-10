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


def test_real_qwen3_tts_script_execution():

    print('\n▶️  [STARTING TEST] test_real_qwen3_tts_script_execution')
    """Run the actual run_qwen3_tts.py main() with GPU deps mocked in-memory.

    Verifies: arg parsing, voice mapping, _np_to_wav_bytes conversion,
    WAV file structure (channels, sample width, frame count, duration).
    """
    import types
    import importlib
    import tempfile

    saved_modules = {}
    mock_keys = ["torch", "qwen_tts", "model_pin", "huggingface_hub"]

    try:
        # --- Mock torch ---
        mock_torch = types.ModuleType("torch")
        mock_torch.bfloat16 = "bfloat16"
        saved_modules["torch"] = sys.modules.get("torch")
        sys.modules["torch"] = mock_torch

        # --- Mock qwen_tts with a model that returns a real numpy waveform ---
        mock_qwen_tts = types.ModuleType("qwen_tts")

        class FakeQwen3TTSModel:
            @classmethod
            def from_pretrained(cls, path, device_map=None, dtype=None):
                return cls()

            def generate_custom_voice(self, text, language, speaker):
                # Generate a real 1-second sine wave at 24kHz
                sr = 24000
                t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
                waveform = 0.5 * np.sin(2 * np.pi * 440 * t)  # 440 Hz A4 note
                return [waveform], sr

        mock_qwen_tts.Qwen3TTSModel = FakeQwen3TTSModel
        saved_modules["qwen_tts"] = sys.modules.get("qwen_tts")
        sys.modules["qwen_tts"] = mock_qwen_tts

        # --- Mock model_pin (no model files on Mac) ---
        mock_model_pin = types.ModuleType("model_pin")
        mock_model_pin.QWEN3_TTS_PIN = None
        mock_model_pin.verify_pin = lambda pin, path: None
        saved_modules["model_pin"] = sys.modules.get("model_pin")
        sys.modules["model_pin"] = mock_model_pin

        # --- Mock huggingface_hub ---
        mock_hf = types.ModuleType("huggingface_hub")
        mock_hf.snapshot_download = lambda *a, **kw: "/fake"
        saved_modules["huggingface_hub"] = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = mock_hf

        # Load the actual production script as a module
        script_path = str(PROJECT_ROOT / "scripts" / "run_qwen3_tts.py")
        spec = importlib.util.spec_from_file_location("run_qwen3_tts", script_path)
        tts_module = importlib.util.module_from_spec(spec)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_wav = os.path.join(tmpdir, "test_output.wav")

            # Patch sys.argv to simulate CLI invocation
            original_argv = sys.argv
            sys.argv = [
                "run_qwen3_tts.py",
                "--text", "Dopamine drives motivation and reward-seeking behavior.",
                "--voice", "V1",
                "--output", output_wav,
            ]

            try:
                spec.loader.exec_module(tts_module)
                exit_code = tts_module.main()
            finally:
                sys.argv = original_argv

            # Assert clean exit
            print('     ├─ [Assert] Checking: exit_code == 0, f\"run_qwen3_tts.py main() returned {exit_co...')
            assert exit_code == 0, f"run_qwen3_tts.py main() returned {exit_code}"

            # Assert output WAV exists and has content
            print('     ├─ [Assert] Checking: os.path.exists(output_wav), \"TTS output WAV was not written...')
            assert os.path.exists(output_wav), "TTS output WAV was not written"
            print('     ├─ [Assert] Checking: os.path.getsize(output_wav) > 0, \"TTS output WAV is empty\"')
            assert os.path.getsize(output_wav) > 0, "TTS output WAV is empty"

            # Verify WAV structure using wave module
            with wave.open(output_wav, "rb") as wf:
                print('     ├─ [Assert] Checking: wf.getnchannels() == 1, f\"Expected mono, got {wf.getnchanne...')
                assert wf.getnchannels() == 1, f"Expected mono, got {wf.getnchannels()} channels"
                print('     ├─ [Assert] Checking: wf.getsampwidth() == 2, f\"Expected 16-bit, got {wf.getsampw...')
                assert wf.getsampwidth() == 2, f"Expected 16-bit, got {wf.getsampwidth() * 8}-bit"
                print('     ├─ [Assert] Checking: wf.getframerate() == 24000, f\"Expected 24kHz, got {wf.getfr...')
                assert wf.getframerate() == 24000, f"Expected 24kHz, got {wf.getframerate()}"
                n_frames = wf.getnframes()
                duration = n_frames / wf.getframerate()
                print(f"TTS output: {n_frames} frames, {duration:.2f}s, {wf.getframerate()}Hz")
                print('     ├─ [Assert] Checking: 0.9 <= duration <= 1.1, f\"Expected ~1.0s duration, got {dur...')
                assert 0.9 <= duration <= 1.1, f"Expected ~1.0s duration, got {duration:.2f}s"

            # Verify loudness is non-silent
            lufs = measure_lufs_integrated(output_wav)
            print(f"TTS output LUFS: {lufs:.2f}")
            print('     ├─ [Assert] Checking: lufs > -60.0, f\"Output appears silent: {lufs:.2f} LUFS\"')
            assert lufs > -60.0, f"Output appears silent: {lufs:.2f} LUFS"

    finally:
        # Restore original sys.modules state
        for key in mock_keys:
            if saved_modules.get(key) is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = saved_modules[key]


# ===========================================================================
# 19. Real LTX-2.3 Video Script Execution (GPU-mocked, logic real)
# ===========================================================================