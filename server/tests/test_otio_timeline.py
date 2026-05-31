"import os
import shutil
import tempfile
import pytest
import opentimelineio as otio
from pathlib import Path
import sys

# Setup python path to import server/pipeline modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "pipeline") not in sys.path:
    sys.path.append(str(PROJECT_ROOT / "pipeline"))

from otio_timeline import (
    create_timeline,
    load_timeline,
    save_timeline,
    add_clip,
    get_timeline_summary,
    validate_timeline,
)

@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="otio_test_")
    yield d
    shutil.rmtree(d)

def test_create_timeline(temp_dir):
    filepath = create_timeline("Lacan Objet Petit A", 3, output_dir=temp_dir)
    assert os.path.exists(filepath)
    assert filepath.endswith("Lacan_Objet_Petit_A.otio")

    timeline = load_timeline(filepath)
    assert timeline.name == "documentary_Lacan Objet Petit A"
    assert timeline.metadata["topic"] == "Lacan Objet Petit A"
    assert timeline.metadata["num_scenes"] == 3

    track_names = [t.name for t in timeline.tracks]
    assert "V1_Video" in track_names
    assert "A1_Narration" in track_names
    assert "A2_Music" in track_names

def test_add_clip(temp_dir):
    filepath = create_timeline("Desire", 1, output_dir=temp_dir)
    
    # Add a narration clip
    media_path = os.path.join(temp_dir, "test_audio.wav")
    # Touch the file so it exists for validation
    Path(media_path).touch()

    added = add_clip(
        filepath=filepath,
        track_name="A1_Narration",
        clip_name="scene1_narrator",
        media_path=media_path,
        duration=6.5,
        metadata={"scene_num": 1, "block_id": "intro"}
    )
    assert added is True

    # Try adding again (idempotency check)
    added_again = add_clip(
        filepath=filepath,
        track_name="A1_Narration",
        clip_name="scene1_narrator",
        media_path=media_path,
        duration=6.
<truncated 3045 bytes>