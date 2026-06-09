import os
import sys
import subprocess
import numpy as np
import math
from pathlib import Path

# Setup Python paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from agent_base import run_movie_assembly
import opentimelineio as otio

def measure_lufs_integrated(audio_path: str) -> float:
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
    return 20.0 * math.log10(rms)

def test_audio_loudness_normalizer_compilation():
    print('\n▶️  [STARTING TEST] test_audio_loudness_normalizer_compilation')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # 1. Generate a loud PCM wav using ffmpeg
        loud_wav = os.path.join(db_dir, "loud.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100", "-t", "5.0", loud_wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        
        # Create a valid OTIO timeline referencing the loud wav
        timeline = otio.schema.Timeline(name="test_timeline")
        video_track = otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video)
        audio_track = otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio)
        
        loud_clip = otio.schema.Clip(
            name="loud_audio",
            media_reference=otio.schema.ExternalReference(target_url=loud_wav),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 44100),
                duration=otio.opentime.RationalTime(5 * 44100, 44100)
            )
        )
        audio_track.append(loud_clip)
        
        video_clip = otio.schema.Clip(
            name="video_placeholder",
            media_reference=otio.schema.MissingReference(),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(5 * 24, 24)
            )
        )
        video_track.append(video_clip)
        
        timeline.tracks.append(video_track)
        timeline.tracks.append(audio_track)
        
        otio_path = os.path.join(db_dir, "timeline.otio")
        otio.adapters.write_to_file(timeline, otio_path)
        
        # 2. Run the production assembly movie builder
        output_mp4 = os.path.join(db_dir, "final.mp4")
        run_movie_assembly(
            output_path=output_mp4,
            timeline_path=otio_path,
            include_placeholders=True,
            target_duration=5.0,
            event_store_instance=event_store,
            log_dir=db_dir
        )
        
        # 3. Assert normalization results (Target: -16.0 LUFS +/- 1.0 LUFS)
        norm_wav = os.path.join(db_dir, "normalized.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", output_mp4, "-vn", "-acodec", "pcm_s16le", norm_wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        
        normalized_lufs = measure_lufs_integrated(norm_wav)
        print(f"Normalized sine wave LUFS: {normalized_lufs:.2f} LUFS")
        assert abs(normalized_lufs - (-16.0)) <= 1.0, f"Loudness normalization out of bounds: {normalized_lufs:.2f}"
        
        # Verify schema correctness of emitted event
        events = event_store.replay()
        complete_events = [e for e in events if e.kind == "pipeline_complete"]
        assert len(complete_events) == 1
        evt = complete_events[0]
        assert evt.agent == "assembly"
        assert evt.output_path == output_mp4
        assert isinstance(evt.duration_sec, float)
        print("✓ Loudness normalization & event schema verified.")
