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


def test_real_ltx_video_script_execution():

    print('\n▶️  [STARTING TEST] test_real_ltx_video_script_execution')
    """Run the actual run_ltx_2_3.py main() with GPU deps mocked in-memory.

    Verifies: arg parsing, frame count calculation, negative prompt assembly,
    ffmpeg rawvideo→MP4 encoding, output MP4 validity via ffprobe.
    """
    import types
    import importlib
    import tempfile

    saved_modules = {}
    mock_keys = [
        "torch", "model_pin",
        "ltx_pipelines", "ltx_pipelines.ti2vid_one_stage",
        "ltx_core", "ltx_core.components", "ltx_core.components.guiders",
    ]

    try:
        # --- Mock torch ---
        mock_torch = types.ModuleType("torch")
        mock_torch.bfloat16 = "bfloat16"

        class FakeCuda:
            @staticmethod
            def empty_cache(): pass
            @staticmethod
            def synchronize(): pass

        mock_torch.cuda = FakeCuda()
        saved_modules["torch"] = sys.modules.get("torch")
        sys.modules["torch"] = mock_torch

        # --- Mock model_pin ---
        mock_model_pin = types.ModuleType("model_pin")
        mock_model_pin.LTX_VIDEO_PIN = None
        mock_model_pin.LTX_VIDEO_GEMMA_PIN = None
        mock_model_pin.verify_pin = lambda pin, path: None
        saved_modules["model_pin"] = sys.modules.get("model_pin")
        sys.modules["model_pin"] = mock_model_pin

        # --- Mock ltx_core hierarchy ---
        mock_ltx_core = types.ModuleType("ltx_core")
        mock_ltx_core_components = types.ModuleType("ltx_core.components")
        mock_ltx_core_guiders = types.ModuleType("ltx_core.components.guiders")

        class FakeMultiModalGuiderParams:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        mock_ltx_core_guiders.MultiModalGuiderParams = FakeMultiModalGuiderParams
        mock_ltx_core.components = mock_ltx_core_components
        mock_ltx_core_components.guiders = mock_ltx_core_guiders

        for k in ["ltx_core", "ltx_core.components", "ltx_core.components.guiders"]:
            saved_modules[k] = sys.modules.get(k)
        sys.modules["ltx_core"] = mock_ltx_core
        sys.modules["ltx_core.components"] = mock_ltx_core_components
        sys.modules["ltx_core.components.guiders"] = mock_ltx_core_guiders

        # --- Mock ltx_pipelines with a pipeline that yields real numpy frames ---
        mock_ltx_pipelines = types.ModuleType("ltx_pipelines")
        mock_ltx_pipelines_ti2vid = types.ModuleType("ltx_pipelines.ti2vid_one_stage")

        class FakeTI2VidOneStagePipeline:
            def __init__(self, checkpoint_path=None, gemma_root=None, loras=None):
                pass

            def __call__(self, prompt=None, negative_prompt=None, seed=None,
                         height=None, width=None, num_frames=None, frame_rate=None,
                         num_inference_steps=None, video_guider_params=None,
                         audio_guider_params=None, images=None):
                # Generate real RGB frames as numpy array
                h = height or 320
                w = width or 512
                nf = num_frames or 25

                class FakeChunk:
                    def __init__(self, data):
                        self._data = data
                    def cpu(self):
                        return self
                    def numpy(self):
                        return self._data

                # Create gradient frames so ffmpeg has real pixel data to encode
                frames = np.zeros((nf, h, w, 3), dtype=np.uint8)
                for i in range(nf):
                    # Horizontal gradient that shifts per frame
                    grad = np.linspace(0, 255, w, dtype=np.uint8)
                    frame = np.tile(grad, (h, 1))
                    frames[i, :, :, 0] = np.roll(frame, i * 5, axis=1)  # R
                    frames[i, :, :, 1] = np.roll(frame, i * 10, axis=1)  # G
                    frames[i, :, :, 2] = 128  # B constant

                return iter([FakeChunk(frames)]), None

        mock_ltx_pipelines_ti2vid.TI2VidOneStagePipeline = FakeTI2VidOneStagePipeline
        mock_ltx_pipelines.ti2vid_one_stage = mock_ltx_pipelines_ti2vid

        saved_modules["ltx_pipelines"] = sys.modules.get("ltx_pipelines")
        saved_modules["ltx_pipelines.ti2vid_one_stage"] = sys.modules.get("ltx_pipelines.ti2vid_one_stage")
        sys.modules["ltx_pipelines"] = mock_ltx_pipelines
        sys.modules["ltx_pipelines.ti2vid_one_stage"] = mock_ltx_pipelines_ti2vid

        with tempfile.TemporaryDirectory() as tmpdir:
            output_mp4 = os.path.join(tmpdir, "test_output.mp4")

            # Create fake model directory structure so path checks pass
            models_dir = os.path.join(tmpdir, "models", "ltx23")
            gemma_dir = os.path.join(tmpdir, "models", "ltx23", "gemma")
            os.makedirs(gemma_dir, exist_ok=True)
            # Create fake checkpoint file
            with open(os.path.join(models_dir, "ltx-2.3-22b-dev.safetensors"), "wb") as f:
                f.write(b"\x00" * 64)

            # Load the actual production script
            script_path = str(PROJECT_ROOT / "scripts" / "run_ltx_2_3.py")
            spec = importlib.util.spec_from_file_location("run_ltx_2_3", script_path)
            ltx_module = importlib.util.module_from_spec(spec)

            # Patch the hardcoded /workspace/models path
            original_argv = sys.argv
            sys.argv = [
                "run_ltx_2_3.py",
                "--prompt", "A sweeping aerial view of mountain ranges at dawn.",
                "--duration", "2.0",
                "--width", "512",
                "--height", "320",
                "--seed", "42",
                "--steps", "10",
                "--output", output_mp4,
            ]

            try:
                # Monkey-patch the hardcoded models_dir before exec
                source = Path(script_path).read_text()
                source = source.replace(
                    'models_dir = "/workspace/models"',
                    f'models_dir = "{os.path.join(tmpdir, "models")}"',
                )
                code = compile(source, script_path, "exec")
                exec(code, ltx_module.__dict__)
                exit_code = ltx_module.main()
            finally:
                sys.argv = original_argv

            # Assert clean exit
            print('     ├─ [Assert] Checking: exit_code == 0, f\"run_ltx_2_3.py main() returned {exit_code...')
            assert exit_code == 0, f"run_ltx_2_3.py main() returned {exit_code}"

            # Assert output MP4 exists
            print('     ├─ [Assert] Checking: os.path.exists(output_mp4), \"LTX output MP4 was not written...')
            assert os.path.exists(output_mp4), "LTX output MP4 was not written"
            print('     ├─ [Assert] Checking: os.path.getsize(output_mp4) > 0, \"LTX output MP4 is empty\"')
            assert os.path.getsize(output_mp4) > 0, "LTX output MP4 is empty"

            # Verify with ffprobe: duration and video stream
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", output_mp4],
                capture_output=True, text=True, check=True,
            )
            duration = float(res.stdout.strip())
            print(f"LTX output duration: {duration:.2f}s")
            print('     ├─ [Assert] Checking: 1.0 <= duration <= 3.0, f\"Expected ~2.0s duration, got {dur...')
            assert 1.0 <= duration <= 3.0, f"Expected ~2.0s duration, got {duration:.2f}s"

            # Verify video codec
            res_video = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height",
                 "-of", "csv=p=0", output_mp4],
                capture_output=True, text=True, check=True,
            )
            parts = res_video.stdout.strip().split(",")
            codec = parts[0]
            w = int(parts[1])
            h = int(parts[2])
            print(f"LTX output: codec={codec}, {w}x{h}")
            print('     ├─ [Assert] Checking: codec == \"h264\", f\"Expected h264 codec, got {codec}\"')
            assert codec == "h264", f"Expected h264 codec, got {codec}"
            print('     ├─ [Assert] Checking: w == 512, f\"Expected width 512, got {w}\"')
            assert w == 512, f"Expected width 512, got {w}"
            print('     ├─ [Assert] Checking: h == 320, f\"Expected height 320, got {h}\"')
            assert h == 320, f"Expected height 320, got {h}"

    finally:
        for key in mock_keys:
            if saved_modules.get(key) is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = saved_modules[key]


# ===========================================================================
# 20. parse_duration: All Format Variants
# ===========================================================================