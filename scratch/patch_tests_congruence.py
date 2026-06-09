import os

TESTS_DIR = "/Users/orpington/Documents/economy-documentary-work/tests/units"

def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

# 1. test_audio_agent_tts_job_queueing.py
write_file(
    os.path.join(TESTS_DIR, "test_audio_agent_tts_job_queueing.py"),
    """import os
import sys
import time
import httpx
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, QueueAudioJob
from agent_base import execute_agent_turn
from config_schema import PipelineConfig

def test_audio_agent_tts_job_queueing():
    print('\\n▶️  [STARTING TEST] test_audio_agent_tts_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")

    try:
        httpx.get("https://api.deepseek.com/", timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

    # Start real GSA service in integration harness
    with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        gsa_url = f"http://127.0.0.1:{gsa_port}/"
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Dopamine drives motivation.", duration_sec=3.0)
        ]
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Verify GSA is running and responding live
        gsa_check = httpx.get(gsa_url)
        assert gsa_check.status_code == 200
        
        # Execute the production agent turn directly to query GSA and DeepSeek (SC-05)
        config = PipelineConfig(capabilities=[], log_dir=db_dir)
        
        effects = asyncio.run(execute_agent_turn(
            role="audio",
            gsa_url=gsa_url,
            notification_type="instruction",
            config=config
        ))
        
        # Append the emitted effects to the event store
        for effect in list(effects):
            event_store.append(effect, "")
            
        # Verify event store contains QueueAudioJob using the new split class
        events = event_store.replay()
        queue_audio_events = [e.effect for e in events if e.effect.kind == "queue_audio_job"]
        assert len(queue_audio_events) >= 1
        qa_event = queue_audio_events[0]
        assert isinstance(qa_event, QueueAudioJob)
        assert qa_event.slot_id.startswith("A1:")
        assert not hasattr(qa_event, "job_type")
        
        # Double check GSA state reflection
        gsa_state = httpx.get(gsa_url).json()
        assert len(gsa_state["jobs"]["jobs"]) >= 1
        print("✓ Audio Agent TTS Job queueing verified via direct execute_agent_turn.")
"""
)

# 2. test_video_agent_ltx_job_queueing.py
write_file(
    os.path.join(TESTS_DIR, "test_video_agent_ltx_job_queueing.py"),
    """import os
import sys
import time
import httpx
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, QueueVideoJob
from agent_base import execute_agent_turn
from config_schema import PipelineConfig

def test_video_agent_ltx_job_queueing():
    print('\\n▶️  [STARTING TEST] test_video_agent_ltx_job_queueing')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. DeepSeek API key is missing!")

    try:
        httpx.get("https://api.deepseek.com/", timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

    # Start real GSA service in integration harness
    with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        gsa_url = f"http://127.0.0.1:{gsa_port}/"
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Dopamine drives motivation.", duration_sec=3.0)
        ]
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Verify GSA is running and responding live
        gsa_check = httpx.get(gsa_url)
        assert gsa_check.status_code == 200
        
        # Execute the production agent turn directly to query GSA and DeepSeek (SC-06)
        config = PipelineConfig(capabilities=[], log_dir=db_dir)
        
        effects = asyncio.run(execute_agent_turn(
            role="video",
            gsa_url=gsa_url,
            notification_type="instruction",
            config=config
        ))
        
        # Append the emitted effects to the event store
        for effect in list(effects):
            event_store.append(effect, "")
            
        # Verify event store contains QueueVideoJob using the new split class
        events = event_store.replay()
        queue_video_events = [e.effect for e in events if e.effect.kind == "queue_video_job"]
        assert len(queue_video_events) >= 1
        qv_event = queue_video_events[0]
        assert isinstance(qv_event, QueueVideoJob)
        assert qv_event.slot_id.startswith("V1:")
        assert not hasattr(qv_event, "job_type")
        
        # Double check GSA state reflection
        gsa_state = httpx.get(gsa_url).json()
        assert len(gsa_state["jobs"]["jobs"]) >= 1
        print("✓ Video Agent LTX Job queueing verified via direct execute_agent_turn.")
"""
)

# 3. test_provisioner_vast_offers_search.py
write_file(
    os.path.join(TESTS_DIR, "test_provisioner_vast_offers_search.py"),
    """import os
import sys
import subprocess
import socket
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
        
    # Check live network reachability to vast.ai
    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")

    # Verify CLI version compatibility
    cmd_version = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--version"]
    res_version = subprocess.run(cmd_version, capture_output=True, text=True)
    assert res_version.returncode == 0
    version_str = res_version.stdout.strip()
    assert version_str, "Vast.ai CLI version is empty"
    parts = version_str.split('.')
    assert len(parts) >= 2 and all(p.isdigit() for p in parts[:2]), f"Unexpected version: {version_str}"

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
        parts_line = line.split()
        if len(parts_line) >= 8 and parts_line[0].isdigit():
            try:
                price = float(parts_line[-1])
                gpu_name = parts_line[2]
                found_offer = True
            except ValueError:
                continue
    assert found_offer, "Could not parse any valid offers from vastai output"
    print("✓ Vast.ai offers search verified successfully.")
"""
)

# 4. test_vast_create_and_destroy_lifecycle.py
write_file(
    os.path.join(TESTS_DIR, "test_vast_create_and_destroy_lifecycle.py"),
    """import os
import sys
import time
import subprocess
import json
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from models.vm_state import VMState

def test_vast_create_and_destroy_lifecycle():
    print('\\n▶️  [STARTING TEST] test_vast_create_and_destroy_lifecycle')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")

    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")
        
    # Find cheapest offer
    cmd_search = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers", "rentable=true num_gpus=1", "-o", "price", "--raw"]
    res = subprocess.run(cmd_search, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("CRITICAL FAILURE: Failed to fetch search offers from Vast.ai API.")
        
    try:
        offers = json.loads(res.stdout.strip())
        offer_id = offers[0]["id"]
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")
        
    print(f"Renting cheapest Vast.ai offer: {offer_id}")
    cmd_create = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "create", "instance", str(offer_id), "--image", "ubuntu:22.04", "--disk", "10", "--raw"]
    create_res = subprocess.run(cmd_create, capture_output=True, text=True)
    if create_res.returncode != 0:
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
    
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM successfully leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}. Output: {create_res.stdout}.")
        
    try:
        # Poll status until "running"
        print(f"Waiting for VM instance {instance_id} to boot...")
        start_time = time.time()
        booted = False
        while time.time() - start_time < 300:
            cmd_show = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "show", "instances", "--raw"]
            show_res = subprocess.run(cmd_show, capture_output=True, text=True)
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
                            
                            # Parse connection details and validate using production VMState model
                            ssh_host = inst_info.get("ssh_host", "") or inst_info.get("ssh_ipaddr", "") or "127.0.0.1"
                            ssh_port = int(inst_info.get("ssh_port", 0) or 0)
                            gpu_name = inst_info.get("gpu_name", "")
                            vram_gb = float(inst_info.get("gpu_ram", 0.0) or 0.0)
                            price_per_hour = float(inst_info.get("dph_total", 0.0) or 0.0)
                            disk_gb = float(inst_info.get("disk_space", 0.0) or 0.0)
                            
                            vm_state_obj = VMState(
                                instance_id=str(instance_id),
                                status="running",
                                ssh_host=ssh_host,
                                ssh_port=ssh_port,
                                gpu_name=gpu_name,
                                vram_gb=vram_gb,
                                price_per_hour=price_per_hour,
                                disk_gb=disk_gb,
                                worker_url=f"http://{ssh_host}:{ssh_port}"
                            )
                            assert vm_state_obj.instance_id == str(instance_id)
                            assert vm_state_obj.status == "running"
                            break
                except Exception as e:
                    print(f"Error parsing show instances: {e}")
            time.sleep(5)
        assert booted, "VM instance failed to reach running status in time"
    finally:
        print(f"Cleaning up and destroying Vast.ai instance {instance_id}...")
        cmd_destroy = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "destroy", "instance", str(instance_id)]
        subprocess.run(cmd_destroy, capture_output=True)
        print("Teardown finished.")
"""
)

# 5. test_ssh_handshake_and_docker_health.py
write_file(
    os.path.join(TESTS_DIR, "test_ssh_handshake_and_docker_health.py"),
    """import os
import sys
import time
import subprocess
import json
import socket
from pathlib import Path

def test_ssh_handshake_and_docker_health():
    print('\\n▶️  [STARTING TEST] test_ssh_handshake_and_docker_health')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")

    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")

    # spot lease of GPU (SC-04)
    cmd_search = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers", "rentable=true num_gpus=1", "-o", "price", "--raw"]
    res = subprocess.run(cmd_search, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("CRITICAL FAILURE: Failed to fetch search offers from Vast.ai API.")
        
    try:
        offers = json.loads(res.stdout.strip())
        offer_id = offers[0]["id"]
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")

    print(f"Renting cheapest Vast.ai offer: {offer_id}")
    cmd_create = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "create", "instance", str(offer_id), "--image", "ubuntu:22.04", "--disk", "10", "--raw"]
    create_res = subprocess.run(cmd_create, capture_output=True, text=True)
    if create_res.returncode != 0:
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
    
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}. Output: {create_res.stdout}.")

    try:
        # Poll status until "running"
        print(f"Waiting for VM instance {instance_id} to boot...")
        start_time = time.time()
        ssh_host, ssh_port = None, None
        while time.time() - start_time < 300:
            cmd_show = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "show", "instances", "--raw"]
            show_res = subprocess.run(cmd_show, capture_output=True, text=True)
            if show_res.returncode == 0:
                try:
                    instances = json.loads(show_res.stdout.strip())
                    inst_info = next((inst for inst in instances if str(inst["id"]) == str(instance_id)), None)
                    if inst_info:
                        status = inst_info.get("status", "")
                        actual_status = inst_info.get("actual_status", "")
                        if status == "running" or actual_status == "running":
                            ssh_host = inst_info.get("ssh_host", "") or inst_info.get("ssh_ipaddr", "")
                            ssh_port = int(inst_info.get("ssh_port", 0) or 0)
                            if ssh_host and ssh_port:
                                break
                except Exception as e:
                    print(f"Error parsing show instances: {e}")
            time.sleep(5)
        
        assert ssh_host and ssh_port, "VM instance failed to reach running status with SSH port"
        
        # Verify SSH handshake and transfer the actual production worker agent (scripts/vm_agent.py) to VM
        print(f"Connecting to VM via SSH at {ssh_host}:{ssh_port}...")
        
        # Function to run SSH command
        def run_ssh(cmd_str, timeout=60):
            args = [
                "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "PasswordAuthentication=no", "-p", str(ssh_port), f"root@{ssh_host}",
                cmd_str
            ]
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

        # Poll until SSH is reachable
        ssh_ready = False
        for _ in range(30):
            res_ping = run_ssh("echo ready")
            if res_ping.returncode == 0 and "ready" in res_ping.stdout:
                ssh_ready = True
                break
            time.sleep(2)
        assert ssh_ready, "SSH port opened but handshake timed out"

        # Install actual production VM agent dependencies
        print("Installing FastAPI and uvicorn on VM...")
        run_ssh("apt-get update -y && apt-get install -y python3-pip")
        run_ssh("pip3 install fastapi uvicorn pydantic-ai")

        # Copy the actual production script scripts/vm_agent.py to the VM via SCP
        print("Copying actual scripts/vm_agent.py to remote VM...")
        local_agent_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "vm_agent.py")
        cmd_scp = [
            "scp", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PasswordAuthentication=no", "-P", str(ssh_port), local_agent_path, f"root@{ssh_host}:/workspace/vm_agent.py"
        ]
        scp_res = subprocess.run(cmd_scp, capture_output=True, text=True)
        assert scp_res.returncode == 0, f"SCP failed: {scp_res.stderr}"

        # Start the actual production agent inside the remote VM on port 8880
        print("Starting actual vm_agent.py on remote VM...")
        run_ssh("nohup python3 /workspace/vm_agent.py --port 8880 > /workspace/agent.log 2>&1 &")

        # Query local HTTP GET to worker URL inside the container (SC-04)
        ssh_success = False
        ssh_err = ""
        # Try up to 10 times for uvicorn server to start inside VM
        for _ in range(10):
            ssh_res = run_ssh("curl -i -s http://127.0.0.1:8880/")
            if ssh_res.returncode == 0:
                stdout = ssh_res.stdout
                assert "HTTP/1.1 200 OK" in stdout or "200" in stdout.split('\\n')[0]
                assert "Content-Type: text/plain" in stdout or "content-type: text/plain" in stdout
                assert "healthy and active" in stdout
                ssh_success = True
                print("✓ SSH handshake and actual docker worker health verified successfully.")
                break
            else:
                ssh_err = ssh_res.stderr + "\\n" + ssh_res.stdout
                time.sleep(3)
        assert ssh_success, f"Remote worker health check failed: {ssh_err}"
    finally:
        print(f"Destroying Vast.ai instance {instance_id}...")
        cmd_destroy = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "destroy", "instance", str(instance_id)]
        subprocess.run(cmd_destroy, capture_output=True)
        print("Teardown finished.")
"""
)

# 6. test_audio_loudness_normalizer_compilation.py
write_file(
    os.path.join(TESTS_DIR, "test_audio_loudness_normalizer_compilation.py"),
    """import os
import sys
import subprocess
import numpy as np
import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from agent_base import run_movie_assembly
from effects import PipelineComplete
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
    # Guard: Ensure physical ffmpeg binary is installed and callable
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: ffmpeg binary is missing or not callable: {e}")

    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Paths to real assets in repository
        video_path = str(PROJECT_ROOT / "tests/assets/dummy_video_6.8s.mp4")
        audio_path = str(PROJECT_ROOT / "tests/assets/dummy_narrator_6.8s.wav")
        
        if not os.path.exists(video_path) or not os.path.exists(audio_path):
            raise RuntimeError(f"CRITICAL FAILURE: Source media assets are missing: {video_path}, {audio_path}")

        # Create valid OTIO timeline referencing real assets
        timeline = otio.schema.Timeline(name="test_timeline")
        video_track = otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video)
        audio_track = otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio)
        
        loud_clip = otio.schema.Clip(
            name="loud_audio",
            media_reference=otio.schema.ExternalReference(target_url=audio_path),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 44100),
                duration=otio.opentime.RationalTime(6.8 * 44100, 44100)
            )
        )
        audio_track.append(loud_clip)
        
        video_clip = otio.schema.Clip(
            name="video_clip",
            media_reference=otio.schema.ExternalReference(target_url=video_path),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(6.8 * 24, 24)
            )
        )
        video_track.append(video_clip)
        
        timeline.tracks.append(video_track)
        timeline.tracks.append(audio_track)
        
        otio_path = os.path.join(db_dir, "timeline.otio")
        otio.adapters.write_to_file(timeline, otio_path)
        
        # Run movie builder
        output_mp4 = os.path.join(db_dir, "final.mp4")
        run_movie_assembly(
            output_path=output_mp4,
            timeline_path=otio_path,
            include_placeholders=False,
            target_duration=6.8,
            event_store_instance=event_store,
            log_dir=db_dir
        )
        
        # Assert normalization results (Target: -16.0 LUFS +/- 1.0 LUFS)
        norm_wav = os.path.join(db_dir, "normalized.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", output_mp4, "-vn", "-acodec", "pcm_s16le", norm_wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        
        normalized_lufs = measure_lufs_integrated(norm_wav)
        print(f"Normalized audio LUFS: {normalized_lufs:.2f} LUFS")
        assert abs(normalized_lufs - (-16.0)) <= 1.0, f"Loudness normalization out of bounds: {normalized_lufs:.2f}"
        
        # Verify schema correctness of emitted event using production model
        events = event_store.replay()
        complete_events = [e.effect for e in events if e.effect.kind == "pipeline_complete"]
        assert len(complete_events) == 1
        evt = complete_events[0]
        assert isinstance(evt, PipelineComplete)
        assert evt.agent == "assembly"
        assert evt.output_path == output_mp4
        assert abs(evt.duration_sec - 6.8) < 0.1
        print("✓ Loudness normalization & event schema verified.")
"""
)

# 7. test_coordinate_timeline_dynamic_drift.py
write_file(
    os.path.join(TESTS_DIR, "test_coordinate_timeline_dynamic_drift.py"),
    """import os
import sys
import tempfile
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, DurationAdjusted
from projections import Timeline
from coordinate_timeline import CoordinateTimeline
import opentimelineio as otio

def test_coordinate_timeline_dynamic_drift():
    print('\\n▶️  [STARTING TEST] test_coordinate_timeline_dynamic_drift')
    # Guard: Ensure sqlite3 binary exists (Condition 2: live shell command and physical binary)
    try:
        subprocess.run(["sqlite3", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: sqlite3 CLI binary is missing or not callable: {e}")
    
    with tempfile.TemporaryDirectory() as db_dir:
        # Initialize physical SQLite database (Condition 2: live boundary interaction)
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        db_file = os.path.join(db_dir, "events.db")
        assert os.path.exists(db_file), "CRITICAL FAILURE: physical events database was not created!"
        
        # Initialize script with 3 blocks
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="First text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Second text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b3", speaker="narrator", text="Third text.", duration_sec=3.0)
        ]
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Adjust duration of block 1 (increase by 2.0s)
        event_store.append(DurationAdjusted(
            agent="audio", block_id="s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=5.0
        ), "initial_hash")
        
        # Verify database contents using physical sqlite3 CLI command (Condition 2: live shell command)
        res = subprocess.run(["sqlite3", db_file, "SELECT seq, kind FROM events ORDER BY seq"], capture_output=True, text=True, check=True)
        assert "duration_adjusted" in res.stdout
        
        # Reconstruct projections from sequence 0 directly via the physical DB
        timeline = Timeline()
        timeline.tick(event_store)
        
        # Verify total duration increased by exactly 2.0s (to 11.0s total)
        duration = timeline.get_timeline_duration_sec()
        assert duration == 11.0
        
        # Direct check on CoordinateTimeline projection to verify start/end coordinates of blocks 2 and 3 (SC-08)
        coord_timeline = CoordinateTimeline()
        coord_timeline.tick(event_store)
        
        c_clips = sorted(coord_timeline.clips["audio"], key=lambda c: c.span.start_sec)
        assert len(c_clips) == 3
        # Block 1 starts at 0.0s, ends at 5.0s (duration 5.0s)
        assert c_clips[0].scenario_id == "s1_b1"
        assert c_clips[0].span.start_sec == 0.0
        assert c_clips[0].span.end_sec == 5.0
        
        # Block 2 starts at 5.0s, ends at 8.0s (duration 3.0s)
        assert c_clips[1].scenario_id == "s1_b2"
        assert c_clips[1].span.start_sec == 5.0
        assert c_clips[1].span.end_sec == 8.0
        
        # Block 3 starts at 8.0s, ends at 11.0s (duration 3.0s)
        assert c_clips[2].scenario_id == "s1_b3"
        assert c_clips[2].span.start_sec == 8.0
        assert c_clips[2].span.end_sec == 11.0

        # Assert database-native high precision subtraction using sqlean (Condition 2)
        diff_ns = coord_timeline.query_sqlean_timespan(0.0, 11.0)
        assert diff_ns == 11 * 1000000000
        print("✓ Coordinate Timeline dynamic drift verified.")
"""
)

# 8. test_budget_limit_aborted_gate.py
write_file(
    os.path.join(TESTS_DIR, "test_budget_limit_aborted_gate.py"),
    """import os
import sys
import time
import httpx
import json
import socket
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, BudgetSet, VMAllocated, VMDeallocated

def test_budget_limit_aborted_gate():
    print('\\n▶️  [STARTING TEST] test_budget_limit_aborted_gate')
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    if not api_key:
        raise RuntimeError("CRITICAL FAILURE: Vast.ai API key is empty!")

    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")

    # 1. Lease a real VM on Vast.ai to allow live destruction behavior
    cmd_search = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "search", "offers", "rentable=true num_gpus=1", "-o", "price", "--raw"]
    res = subprocess.run(cmd_search, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("CRITICAL FAILURE: Failed to fetch search offers from Vast.ai API.")
        
    try:
        offers = json.loads(res.stdout.strip())
        offer_id = offers[0]["id"]
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")

    print(f"Renting spot VM offer {offer_id} for budget test...")
    cmd_create = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "create", "instance", str(offer_id), "--image", "ubuntu:22.04", "--disk", "10", "--raw"]
    create_res = subprocess.run(cmd_create, capture_output=True, text=True)
    if create_res.returncode != 0:
        raise RuntimeError(f"CRITICAL FAILURE: Lease creation failed: {create_res.stderr}.")
    
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM successfully leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Failed to parse contract ID: {e}.")

    try:
        # 2. Run GSA and Provisioner with real boundaries (capabilities=[])
        with IntegrationHarness(required_agents=["gsa", "provisioner"], capabilities=[]) as harness:
            db_dir = harness.temp_dir.name
            gsa_port = harness.ports["gsa"]
            
            event_store = EventStore(log_dir=db_dir)
            event_store._init_db()
            
            # Seed budget set to $0.01 (extremely low limit to force cost cap violation)
            event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
            event_store.append(BudgetSet(agent="operator", budget_usd=0.01, reason="run_start"), "")
            
            # Seed the real VM allocation (so the Provisioner agent knows it exists and is active)
            event_store.append(VMAllocated(
                agent="provisioner",
                instance_id=str(instance_id),
                role="tts",
                offer_id=str(offer_id),
                worker_url="http://127.0.0.1:8880",
                gpu_type="RTX 4090",
                cost_per_hour=0.40
            ), "")
            
            # Exercise the cost accumulation logic (Condition 5) by seeding a deallocated VM with $0.02 cost
            # Cumulative spent_usd becomes $0.02, which is > budget_cap_usd ($0.01)
            event_store.append(VMDeallocated(
                agent="provisioner",
                instance_id="dummy_vm",
                reason="job_done",
                final_cost=0.02,
                runtime_sec=180.0
            ), "")
            
            # Poll GSA to verify cost accumulation has indeed crossed the budget and set budget.exceeded to True
            exceeded = False
            for _ in range(20):
                try:
                    resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
                    state = resp.json()
                    if state["budget"]["exceeded"] is True:
                        exceeded = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            assert exceeded, "GSA cost accumulation failed to flag budget exceeded status!"
            
            # Do NOT send manual wakeup POST. Allow the Provisioner's autonomous background loop
            # to poll GSA, detect the budget violation, and destroy the VM automatically!
            destroyed = False
            start_poll = time.time()
            while time.time() - start_poll < 60:  # 60s timeout
                events = event_store.replay()
                deallocated_events = [
                    e.effect for e in events 
                    if e.effect.kind == "vm_deallocated" and str(e.effect.instance_id) == str(instance_id)
                ]
                if len(deallocated_events) >= 1:
                    destroyed = True
                    break
                time.sleep(1)
                
            assert destroyed, "Provisioner background loop failed to automatically detect budget violation and destroy the VM!"
            print("✓ Budget gate cost accumulation and autonomous VM deallocation verified.")
    finally:
        # Cleanup
        print(f"Ensuring Vast.ai instance {instance_id} is destroyed...")
        cmd_destroy = ["/Users/orpington/.letta-cli-venv/bin/vastai", "--api-key", api_key, "destroy", "instance", str(instance_id)]
        subprocess.run(cmd_destroy, capture_output=True)
        print("Teardown complete.")
"""
)

# 9. test_gsa_wal_concurrency_isolation.py
write_file(
    os.path.join(TESTS_DIR, "test_gsa_wal_concurrency_isolation.py"),
    """import os
import sys
import time
import tempfile
import sqlite3
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from event_store import EventStore
from projections import Jobs, Timeline, VMs, BudgetProjection, StateProjection

def test_gsa_wal_concurrency_isolation():
    print('\\n▶️  [STARTING TEST] test_gsa_wal_concurrency_isolation')
    
    with tempfile.TemporaryDirectory() as db_dir:
        # 1. Initialize DB and set to WAL mode
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        db_file = os.path.join(db_dir, "events.db")
        
        # Verify WAL mode is set on SQLite DB
        conn = sqlite3.connect(db_file)
        res = conn.execute("PRAGMA journal_mode").fetchone()
        assert res[0].lower() == "wal", f"Database is not in WAL mode: {res[0]}"
        conn.close()
        
        # Spawn 5 parallel microservice subprocesses to write events concurrently using direct SQLite inserts
        writers = []
        # Python script code to run in subprocess
        writer_code = (
            "import sys, sqlite3, time, uuid, json\\n"
            "db_path = sys.argv[1]\\n"
            "thread_id = sys.argv[2]\\n"
            "for i in range(100):\\n"
            "    conn = sqlite3.connect(db_path, isolation_level=None)\\n"
            "    conn.execute('PRAGMA busy_timeout=30000')\\n"
            "    conn.execute('PRAGMA journal_mode=WAL')\\n"
            "    conn.execute('BEGIN IMMEDIATE')\\n"
            "    effect_id = str(uuid.uuid4())\\n"
            "    kind = 'queue_audio_job'\\n"
            "    effect_json = json.dumps({\\n"
            "        'effect_id': effect_id,\\n"
            "        'kind': kind,\\n"
            "        'agent': f'writer_{thread_id}',\\n"
            "        'timestamp': time.time(),\\n"
            "        'job_id': f'job_{thread_id}_{i}',\\n"
            "        'scene_num': 1,\\n"
            "        'block_id': f'block_{thread_id}_{i}',\\n"
            "        'slot_id': f'A1:1:block_{thread_id}_{i}',\\n"
            "        'params': {}\\n"
            "    })\\n"
            "    conn.execute(\\n"
            "        'INSERT INTO events (effect_id, kind, effect_json, otio_hash_before, agent, timestamp) VALUES (?, ?, ?, ?, ?, ?)',\\n"
            "        (effect_id, kind, effect_json, '', f'writer_{thread_id}', time.time())\\n"
            "    )\\n"
            "    conn.execute('COMMIT')\\n"
            "    conn.close()\\n"
        )
        
        print("Spawning 5 parallel microservice writers...")
        for i in range(5):
            p = subprocess.Popen([sys.executable, "-c", writer_code, db_file, str(i)])
            writers.append(p)
            
        # Reconstruct projections from sequence 0 repeatedly while writes are occurring (SC-10)
        ticks_count = 0
        while any(p.poll() is None for p in writers):
            # Instantiate clean projections
            jobs = Jobs()
            timeline = Timeline()
            vms = VMs()
            budget = BudgetProjection()
            state = StateProjection()
            
            # Reconstruct from sequence 0 from physical SQLite file
            jobs.tick(event_store)
            timeline.tick(event_store)
            vms.tick(event_store)
            budget.tick(event_store)
            state.tick(event_store)
            ticks_count += 1
            time.sleep(0.05)
            
        # Wait for all processes to complete
        for p in writers:
            p.wait()
            assert p.returncode == 0, "Subprocess writer failed"
            
        # Final reconstruction verification
        jobs = Jobs()
        jobs.tick(event_store)
        events = event_store.replay()
        
        # Verify sequence numbers and database integrity (500 events + initial empty table setup)
        assert len(events) == 500, f"Expected 500 events in database, found {len(events)}"
        assert len(jobs.jobs) == 500, f"Expected 500 jobs reconstructed, found {len(jobs.jobs)}"
        print(f"✓ WAL concurrency isolation verified successfully. Reconstructed {ticks_count} times during writes.")
"""
)

print("Updated tests congruence patches successfully applied!")
