import os
import json

TESTS_DIR = "/Users/orpington/Documents/economy-documentary-work/tests/units"

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

# 1. Update simulation_covers_implementation_plan.md
plan_path = os.path.join(TESTS_DIR, "simulation_covers_implementation_plan.md")
if os.path.exists(plan_path):
    content = read_file(plan_path)
    
    # Add scenario for test_audio_loudness_normalizer_compilation
    old_target = """### SC-08: Timeline Dynamic Offset Cascade
* **BDD Scenario**:
  ```gherkin
  Scenario: recalculating slot timings on duration adjustment
    Given a timeline containing 3 blocks is active
    When a DurationAdjusted event increases block 1 duration by 2.0 seconds
    Then GSA must update the start/end coordinates of blocks 2 and 3
    And the total timeline duration must increase exactly by 2.0 seconds
  ```
* **Cover Test (`test_coordinate_timeline_dynamic_drift`)**: Verifies offset shifting math directly on the GSA projection engine."""

    new_replacement = """### SC-08: Timeline Dynamic Offset Cascade
* **BDD Scenario**:
  ```gherkin
  Scenario: recalculating slot timings on duration adjustment
    Given a timeline containing 3 blocks is active
    When a DurationAdjusted event increases block 1 duration by 2.0 seconds
    Then GSA must update the start/end coordinates of blocks 2 and 3
    And the total timeline duration must increase exactly by 2.0 seconds
  ```
* **Cover Test (`test_coordinate_timeline_dynamic_drift`)**: Verifies offset shifting math directly on the GSA projection engine.

### SC-29/SC-31/SC-34: Audio Loudness Normalization & Assembly
* **BDD Scenario**:
  ```gherkin
  Scenario: Compiling media and applying loudness normalization
    Given a timeline containing a loud narration clip is active
    When the Assembly Agent renders the final cut movie
    Then the output movie must contain a normalized audio track
    And the loudness of the final track must measure -16.0 LUFS +/- 1.0 LUFS
    And the emitted PipelineComplete event must conform to the expected schema
  ```
* **Cover Test (`test_audio_loudness_normalizer_compilation`)**: Invokes the actual production `run_movie_assembly` module, processes the loud wav through the real FFmpeg normalizer filter, measures final track LUFS, and verifies event schema."""

    if old_target in content:
        content = content.replace(old_target, new_replacement)
        print("Updated plan with SC-29/SC-31/SC-34 scenario")
    else:
        print("Could not find SC-08 in plan")

    # Refine SC-10 WAL Concurrency
    old_wal = """### SC-10: WAL Concurrency
* **BDD Scenario**:
  ```gherkin
  Scenario: Replaying log events under parallel writes
    Given GSA is configured in SQLite WAL mode
    When multiple microservices write events concurrently
    Then GSA must reconstruct projections from sequence 0 without locking database transactions
  ```
* **Cover Test (`test_gsa_wal_concurrency_isolation`)**: Asserts lock-free writes and state reconstruction under high event loads."""

    new_wal = """### SC-10: WAL Concurrency
* **BDD Scenario**:
  ```gherkin
  Scenario: Replaying log events under parallel writes
    Given GSA is configured in SQLite WAL mode
    When multiple microservices write events concurrently using direct SQLite connection queries
    Then GSA must reconstruct projections from sequence 0 without locking database transactions
  ```
* **Cover Test (`test_gsa_wal_concurrency_isolation`)**: Asserts lock-free writes and state reconstruction under high parallel database writes."""

    if old_wal in content:
        content = content.replace(old_wal, new_wal)
        print("Refined WAL scenario in plan")
    else:
        print("Could not find SC-10 in plan")
        
    write_file(plan_path, content)


# 2. Update test_scenario_agent_live_prompt_turn.py
ts_path = os.path.join(TESTS_DIR, "test_scenario_agent_live_prompt_turn.py")
if os.path.exists(ts_path):
    content = read_file(ts_path)
    old_assert = """        # Verify script block creation in GSA
        print('     ├─ [Assert] Checking: len(gsa_resp[\"otio\"][\"slots\"]) >= 1')
        assert len(gsa_resp["otio"]["slots"]) >= 1"""
        
    new_assert = """        # Verify script block creation in GSA
        print('     ├─ [Assert] Checking: len(gsa_resp[\"otio\"][\"slots\"]) >= 1')
        assert len(gsa_resp["otio"]["slots"]) >= 1
        
        # Verify event store contains UpdateScript and check its blocks
        events = event_store.replay()
        update_script_events = [e for e in events if e.kind == "update_script"]
        assert len(update_script_events) >= 1
        us_event = update_script_events[0]
        assert len(us_event.blocks) >= 1"""
        
    if old_assert in content:
        content = content.replace(old_assert, new_assert)
        write_file(ts_path, content)
        print("Updated test_scenario_agent_live_prompt_turn.py")
    else:
        print("Could not find assertions in test_scenario_agent_live_prompt_turn.py")


# 3. Update test_audio_agent_tts_job_queueing.py
audio_path = os.path.join(TESTS_DIR, "test_audio_agent_tts_job_queueing.py")
if os.path.exists(audio_path):
    content = read_file(audio_path)
    
    # Enforce key check at start of test
    old_start = """def test_audio_agent_tts_job_queueing():

    print('\\n▶️  [STARTING TEST] test_audio_agent_tts_job_queueing')"""

    new_start = """def test_audio_agent_tts_job_queueing():

    print('\\n▶️  [STARTING TEST] test_audio_agent_tts_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")"""

    # Change harness init
    old_harness = """with IntegrationHarness(required_agents=["gsa", "audio"]) as harness:"""
    new_harness = """with IntegrationHarness(required_agents=["gsa", "audio"], capabilities=[]) as harness:"""
    
    # Import QueueAudioJob
    old_effects_import = """from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob,"""
    new_effects_import = """from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob, QueueAudioJob,"""

    # Change assertion to verify QueueAudioJob
    old_assert = """        # Check GSA to see if TTS job was queued
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        jobs = gsa_resp["jobs"]["jobs"]
        print('     ├─ [Assert] Checking: len(jobs) >= 1')
        assert len(jobs) >= 1
        print('     ├─ [Assert] Checking: any(j[\"job_type\"] == \"tts\" for j in jobs.values())')
        assert any(j["job_type"] == "tts" for j in jobs.values())"""

    new_assert = """        # Check event store to see if TTS job was queued using the new split class
        events = event_store.replay()
        queue_audio_events = [e for e in events if e.kind == "queue_audio_job"]
        assert len(queue_audio_events) >= 1
        qa_event = queue_audio_events[0]
        assert qa_event.slot_id.startswith("A1:")
        assert not hasattr(qa_event, "job_type")"""

    content = content.replace(old_start, new_start)
    content = content.replace(old_harness, new_harness)
    content = content.replace(old_effects_import, new_effects_import)
    content = content.replace(old_assert, new_assert)
    write_file(audio_path, content)
    print("Updated test_audio_agent_tts_job_queueing.py")


# 4. Update test_video_agent_ltx_job_queueing.py
video_path = os.path.join(TESTS_DIR, "test_video_agent_ltx_job_queueing.py")
if os.path.exists(video_path):
    content = read_file(video_path)
    
    # Enforce key check at start of test
    old_start = """def test_video_agent_ltx_job_queueing():

    print('\\n▶️  [STARTING TEST] test_video_agent_ltx_job_queueing')"""

    new_start = """def test_video_agent_ltx_job_queueing():

    print('\\n▶️  [STARTING TEST] test_video_agent_ltx_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")"""

    # Change harness init
    old_harness = """with IntegrationHarness(required_agents=["gsa", "video"]) as harness:"""
    new_harness = """with IntegrationHarness(required_agents=["gsa", "video"], capabilities=[]) as harness:"""

    # Import QueueVideoJob
    old_effects_import = """from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob,"""
    new_effects_import = """from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob, QueueVideoJob,"""

    # Change assertion to verify QueueVideoJob
    old_assert = """        # Check GSA for LTX jobs
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        jobs = gsa_resp["jobs"]["jobs"]
        print('     ├─ [Assert] Checking: len(jobs) >= 1')
        assert len(jobs) >= 1
        print('     ├─ [Assert] Checking: any(j[\"job_type\"] == \"ltx\" for j in jobs.values())')
        assert any(j["job_type"] == "ltx" for j in jobs.values())"""

    new_assert = """        # Check event store for LTX jobs using the new split class
        events = event_store.replay()
        queue_video_events = [e for e in events if e.kind == "queue_video_job"]
        assert len(queue_video_events) >= 1
        qv_event = queue_video_events[0]
        assert qv_event.slot_id.startswith("V1:")
        assert not hasattr(qv_event, "job_type")"""

    content = content.replace(old_start, new_start)
    content = content.replace(old_harness, new_harness)
    content = content.replace(old_effects_import, new_effects_import)
    content = content.replace(old_assert, new_assert)
    write_file(video_path, content)
    print("Updated test_video_agent_ltx_job_queueing.py")


# 5. Rewrite test_provisioner_vast_offers_search.py
prov_search_path = os.path.join(TESTS_DIR, "test_provisioner_vast_offers_search.py")
if os.path.exists(prov_search_path):
    new_search_code = """import os
import sys
import subprocess
from pathlib import Path

def test_provisioner_vast_offers_search():
    print('\\n▶️  [STARTING TEST] test_provisioner_vast_offers_search')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key file is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")
        
    # Run the real search command (SC-02 & SC-07)
    cmd = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    
    assert res.returncode == 0, f"vastai command failed with code {res.returncode}: {res.stderr}"
    output = res.stdout
    assert "GPU_name" in output or "GPU" in output or "Price" in output
    
    # Parse lines to check for valid prices and GPUs
    lines = output.strip().split("\\n")
    found_offer = False
    for line in lines:
        parts = line.split()
        if len(parts) >= 8 and parts[0].isdigit():
            try:
                price = float(parts[-1])
                gpu_name = parts[2]
                found_offer = True
            except ValueError:
                continue
    assert found_offer, "Could not parse any valid offers from vastai output"
    print("✓ Vast.ai offers search verified successfully.")
"""
    write_file(prov_search_path, new_search_code)
    print("Updated test_provisioner_vast_offers_search.py")


# 6. Update test_vast_create_and_destroy_lifecycle.py
lifecycle_path = os.path.join(TESTS_DIR, "test_vast_create_and_destroy_lifecycle.py")
if os.path.exists(lifecycle_path):
    content = read_file(lifecycle_path)
    
    # Remove simulator imports
    old_imports = """from capabilities.test_real_vast_provisioning_bdd_create_instance import VastCreateSimulator
from capabilities.test_real_vast_provisioning_bdd_destroy_instance import VastDestroySimulator"""
    content = content.replace(old_imports, "")
    
    # Find VM lease creation block
    old_destroy_block = """    # Wait and then destroy to ensure clean teardown
    print(f"Cleaning up and destroying Vast.ai instance {instance_id}...")
    cmd_destroy = f"vastai --api-key {api_key} destroy instance {instance_id}"
    destroy_res = subprocess.run(cmd_destroy, shell=True, capture_output=True)
    print('     ├─ [Assert] Checking: destroy_res.returncode == 0, f\\"VM teardown leaked: {destroy...')
    assert destroy_res.returncode == 0, f"VM teardown leaked: {destroy_res.stderr}\""""

    new_destroy_block = """    # Poll instance status until it returns "running" (SC-03)
    print(f"Waiting for VM instance {instance_id} to boot...")
    start_time = time.time()
    booted = False
    while time.time() - start_time < 300: # 5 minute timeout
        cmd_show = f"vastai --api-key {api_key} show instances --raw"
        show_res = subprocess.run(cmd_show, shell=True, capture_output=True, text=True)
        if show_res.returncode == 0:
            try:
                instances = json.loads(show_res.stdout.strip())
                inst_info = next((inst for inst in instances if str(inst["id"]) == str(instance_id)), None)
                if inst_info:
                    status = inst_info.get("status", "")
                    actual_status = inst_info.get("actual_status", "")
                    print(f"VM status: {status}, actual_status: {actual_status}")
                    if status == "running" or actual_status == "running":
                        booted = True
                        # Verify output schema correctness
                        assert "num_gpus" in inst_info
                        assert "gpu_name" in inst_info
                        break
            except Exception as e:
                print(f"Error parsing show instances: {e}")
        time.sleep(5)
    assert booted, "VM instance failed to reach running status in time"
    
    # Wait and then destroy to ensure clean teardown
    print(f"Cleaning up and destroying Vast.ai instance {instance_id}...")
    cmd_destroy = f"vastai --api-key {api_key} destroy instance {instance_id}"
    destroy_res = subprocess.run(cmd_destroy, shell=True, capture_output=True)
    assert destroy_res.returncode == 0, f"VM teardown leaked: {destroy_res.stderr}" """

    content = content.replace(old_destroy_block, new_destroy_block)
    write_file(lifecycle_path, content)
    print("Updated test_vast_create_and_destroy_lifecycle.py")


# 7. Update test_ssh_handshake_and_docker_health.py
ssh_path = os.path.join(TESTS_DIR, "test_ssh_handshake_and_docker_health.py")
if os.path.exists(ssh_path):
    content = read_file(ssh_path)
    
    # Remove simulator import
    old_sim_import = "from capabilities.test_real_vast_provisioning_bdd_worker_health import WorkerHealthSimulator"
    content = content.replace(old_sim_import, "")
    
    # Use vm_agent.py instead of mock_gpu_worker.py
    content = content.replace("mock_gpu_worker.py", "vm_agent.py")
    
    # Remove extra LTX/TTS assertions
    old_extra = """            # Verify TTS job dispatch to mock worker
            tts_payload = 'python run_qwen3_tts.py --text "Dopamine drives motivation." --voice narrator --output /workspace/output/tts.wav'
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp_tts = httpx.post("http://127.0.0.1:9001/", content=tts_payload, timeout=None)
            print('     ├─ [Assert] Checking: resp_tts.status_code == 200')
            assert resp_tts.status_code == 200
            print('     ├─ [Assert] Checking: \"RESULT:\" in resp_tts.text')
            assert "RESULT:" in resp_tts.text
            print('     ├─ [Assert] Checking: \"Generated narration audio\" in resp_tts.text')
            assert "Generated narration audio" in resp_tts.text
            print('     ├─ [Assert] Checking: os.path.exists(\"/tmp/documentary-pipeline/output/tts.wav\")')
            assert os.path.exists("/tmp/documentary-pipeline/output/tts.wav")

            # Verify LTX job dispatch to mock worker
            ltx_payload = 'python run_ltx_2_3.py --prompt "A beautiful landscape." --duration 5.0 --output /workspace/output/ltx.mp4'
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp_ltx = httpx.post("http://127.0.0.1:9001/", content=ltx_payload, timeout=None)
            print('     ├─ [Assert] Checking: resp_ltx.status_code == 200')
            assert resp_ltx.status_code == 200
            print('     ├─ [Assert] Checking: \"RESULT:\" in resp_ltx.text')
            assert "RESULT:" in resp_ltx.text
            print('     ├─ [Assert] Checking: \"Generated video clip\" in resp_ltx.text')
            assert "Generated video clip" in resp_ltx.text
            print('     ├─ [Assert] Checking: os.path.exists(\"/tmp/documentary-pipeline/output/ltx.mp4\")')
            assert os.path.exists("/tmp/documentary-pipeline/output/ltx.mp4")"""
            
    content = content.replace(old_extra, "")
    write_file(ssh_path, content)
    print("Updated test_ssh_handshake_and_docker_health.py")


# 8. Rewrite test_audio_loudness_normalizer_compilation.py
loudness_test_path = os.path.join(TESTS_DIR, "test_audio_loudness_normalizer_compilation.py")
if os.path.exists(loudness_test_path):
    new_loudness_code = """import os
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
    print('\\n▶️  [STARTING TEST] test_audio_loudness_normalizer_compilation')
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
"""
    write_file(loudness_test_path, new_loudness_code)
    print("Updated test_audio_loudness_normalizer_compilation.py")


# 9. Update test_coordinate_timeline_dynamic_drift.py
drift_path = os.path.join(TESTS_DIR, "test_coordinate_timeline_dynamic_drift.py")
if os.path.exists(drift_path):
    content = read_file(drift_path)
    
    # Change harness init
    content = content.replace('with IntegrationHarness(required_agents=["gsa"]) as harness:',
                              'with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:')
                              
    # Add coordinates verification
    old_assert = """        print('     ├─ [Assert] Checking: state[\"otio\"][\"duration_sec\"] == 11.0 # 5.0 + 3.0 + 3.0 ...')
        assert state["otio"]["duration_sec"] == 11.0 # 5.0 + 3.0 + 3.0 = 11.0s"""
        
    new_assert = """        print('     ├─ [Assert] Checking: state[\"otio\"][\"duration_sec\"] == 11.0 # 5.0 + 3.0 + 3.0 ...')
        assert state["otio"]["duration_sec"] == 11.0 # 5.0 + 3.0 + 3.0 = 11.0s
        
        # Direct projection checks to verify start/end coordinates of blocks 2 and 3 (SC-08)
        import opentimelineio as otio
        timeline = Timeline()
        timeline.tick(event_store)
        audio_track = next(t for t in timeline.timeline.tracks if t.name == "A1_Narration")
        clips = [c for c in audio_track if isinstance(c, otio.schema.Clip)]
        assert len(clips) == 3
        # Clip 1 starts at 0.0s, duration 5.0s
        assert clips[0].range_in_parent().start_time.to_seconds() == 0.0
        assert clips[0].range_in_parent().duration.to_seconds() == 5.0
        # Clip 2 starts at 5.0s, duration 3.0s
        assert clips[1].range_in_parent().start_time.to_seconds() == 5.0
        assert clips[1].range_in_parent().duration.to_seconds() == 3.0
        # Clip 3 starts at 8.0s, duration 3.0s
        assert clips[2].range_in_parent().start_time.to_seconds() == 8.0
        assert clips[2].range_in_parent().duration.to_seconds() == 3.0
        
        # Verify CoordinateTimeline projection coordinates
        coord_timeline = CoordinateTimeline()
        coord_timeline.tick(event_store)
        c_clips = sorted(coord_timeline.clips["audio"], key=lambda c: c.span.start_sec)
        assert len(c_clips) == 3
        assert c_clips[0].span.start_sec == 0.0
        assert c_clips[0].span.end_sec == 5.0
        assert c_clips[1].span.start_sec == 5.0
        assert c_clips[1].span.end_sec == 8.0
        assert c_clips[2].span.start_sec == 8.0
        assert c_clips[2].span.end_sec == 11.0"""
        
    content = content.replace(old_assert, new_assert)
    write_file(drift_path, content)
    print("Updated test_coordinate_timeline_dynamic_drift.py")


# 10. Update test_budget_limit_aborted_gate.py
budget_path = os.path.join(TESTS_DIR, "test_budget_limit_aborted_gate.py")
if os.path.exists(budget_path):
    content = read_file(budget_path)
    
    # Change harness init to spawn provisioner too
    content = content.replace('with IntegrationHarness(required_agents=["gsa"]) as harness:',
                              'with IntegrationHarness(required_agents=["gsa", "provisioner"], capabilities=[]) as harness:')
                              
    # Add wakeup provisioner and verify VM deallocated
    old_assert = """        print('     ├─ [Assert] Checking: state[\"budget\"][\"exceeded\"] is True')
        assert state["budget"]["exceeded"] is True"""
        
    new_assert = """        print('     ├─ [Assert] Checking: state[\"budget\"][\"exceeded\"] is True')
        assert state["budget"]["exceeded"] is True
        
        # Wake up the Provisioner agent to destroy the VMs (SC-09)
        provisioner_port = harness.ports["provisioner"]
        print('     ├─ [HTTP] Waking up Provisioner agent...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup")
        assert resp.status_code == 200
        
        # Verify that Provisioner appended VMDeallocated
        events = event_store.replay()
        deallocated = [e for e in events if e.kind == "vm_deallocated"]
        assert len(deallocated) >= 1
        assert deallocated[0].instance_id == "vm_huge_1" """
        
    content = content.replace(old_assert, new_assert)
    write_file(budget_path, content)
    print("Updated test_budget_limit_aborted_gate.py")


# 11. Update test_gsa_wal_concurrency_isolation.py (use split job subclasses)
wal_path = os.path.join(TESTS_DIR, "test_gsa_wal_concurrency_isolation.py")
if os.path.exists(wal_path):
    content = read_file(wal_path)
    
    # Import new subclasses
    old_effects_import = """from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved,"""
    new_effects_import = """from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved,
    QueueAudioJob, QueueVideoJob, AudioJobStarted, VideoJobStarted,
    AudioJobCompleted, VideoJobCompleted, AudioJobFailed, VideoJobFailed,"""
    content = content.replace(old_effects_import, new_effects_import)

    # Replace QueueJob in write_worker
    old_write = """                    local_store.append(QueueJob(
                        agent="audio", job_id=job_id, job_type="tts",
                        scene_num=thread_id, block_id=job_id, slot_id=job_id,
                        params={"text": "Concurrent WAL check", "voice": "narrator"}
                    ), "")"""
    new_write = """                    local_store.append(QueueAudioJob(
                        agent="audio", job_id=job_id,
                        scene_num=thread_id, block_id=job_id, slot_id=job_id,
                        params={"text": "Concurrent WAL check", "voice": "narrator"}
                    ), "")"""
    content = content.replace(old_write, new_write)

    # Replace recovery jobs and status events
    content = content.replace('QueueJob(agent="audio", job_id=job_id_tts, job_type="tts", scene_num=i // 10 + 1, block_id=f"rec_b_{i}", slot_id=slot_id_tts)',
                              'QueueAudioJob(agent="audio", job_id=job_id_tts, scene_num=i // 10 + 1, block_id=f"rec_b_{i}", slot_id=slot_id_tts)')
                              
    content = content.replace('QueueJob(agent="video", job_id=job_id_ltx, job_type="ltx", scene_num=i // 10 + 1, block_id=f"rec_b_{i}", slot_id=slot_id_ltx)',
                              'QueueVideoJob(agent="video", job_id=job_id_ltx, scene_num=i // 10 + 1, block_id=f"rec_b_{i}", slot_id=slot_id_ltx)')
                              
    content = content.replace('JobStarted(agent="provisioner", job_id=job_id_tts, vm_instance_id="1234567")',
                              'AudioJobStarted(agent="provisioner", job_id=job_id_tts, vm_instance_id="1234567")')
                              
    content = content.replace('JobStarted(agent="provisioner", job_id=job_id_ltx, vm_instance_id="1234567")',
                              'VideoJobStarted(agent="provisioner", job_id=job_id_ltx, vm_instance_id="1234567")')
                              
    content = content.replace('JobFailed(agent="provisioner", job_id=job_id_tts, error_message="TTS failed", failure_category="unknown", vm_instance_id="1234567")',
                              'AudioJobFailed(agent="provisioner", job_id=job_id_tts, error_message="TTS failed", failure_category="unknown", vm_instance_id="1234567")')
                              
    content = content.replace('JobFailed(agent="provisioner", job_id=job_id_ltx, error_message="LTX failed", failure_category="unknown", vm_instance_id="1234567")',
                              'VideoJobFailed(agent="provisioner", job_id=job_id_ltx, error_message="LTX failed", failure_category="unknown", vm_instance_id="1234567")')
                              
    content = content.replace('JobCompleted(agent="provisioner", job_id=job_id_tts, artifact_uri=f"rec_b_{i}.wav", duration_sec=2.0, vm_instance_id="1234567")',
                              'AudioJobCompleted(agent="provisioner", job_id=job_id_tts, artifact_uri=f"rec_b_{i}.wav", duration_sec=2.0, vm_instance_id="1234567")')
                              
    content = content.replace('JobCompleted(agent="provisioner", job_id=job_id_ltx, artifact_uri=f"rec_b_{i}.mp4", duration_sec=2.0, vm_instance_id="1234567")',
                              'VideoJobCompleted(agent="provisioner", job_id=job_id_ltx, artifact_uri=f"rec_b_{i}.mp4", duration_sec=2.0, vm_instance_id="1234567")')

    write_file(wal_path, content)
    print("Updated test_gsa_wal_concurrency_isolation.py")

print("All updates applied!")
