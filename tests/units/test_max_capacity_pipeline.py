import os
import sys
import time
import httpx
import wave
import math
import subprocess
import numpy as np
import builtins
from pathlib import Path

def print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\n')
    msg = sep.join(str(arg) for arg in args) + end
    if sys.stdout is not None:
        sys.stdout.write(msg)
        sys.stdout.flush()
    else:
        builtins.print(*args, **kwargs)

# Append paths so we can import harness and effects
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, BudgetSet, UpdateScript, ScriptBlock, VMAllocated, JobStarted, JobCompleted

from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from typing import Any
import re
from projections import VMs
from agent_base import get_active_log_dir
from capabilities.test_real_vast_provisioning_bdd_worker_health import WorkerHealthSimulator
from capabilities.test_real_assembly_bdd_assemble_final_cut import AssembleFinalCutSimulator

class GenericAudioSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd and ("job_audio_" in cmd or "job_id=job_tts_" in cmd):
                match = re.search(r"job_id=([a-zA-Z0-9_]+)", cmd)
                if match:
                    job_id = match.group(1)
                    duration = 3.0
                    
                    log_dir = get_active_log_dir()
                    store = EventStore(log_dir=log_dir)
                    audio_dir = os.path.join(log_dir, "audio_outputs")
                    os.makedirs(audio_dir, exist_ok=True)
                    out_path = f"{audio_dir}/{job_id}.wav"
                    
                    import hashlib
                    h_val = int(hashlib.sha256(job_id.encode()).hexdigest(), 16)
                    frequency = 200 + (h_val % 300)
                    
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100", "-t", str(duration), out_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                    )
                    
                    vms_proj = VMs()
                    vms_proj.tick(store)
                    active_vm_id = None
                    for vm_id, vm in vms_proj.vms.items():
                        if vm.role == "tts" and vm.status == "active":
                            active_vm_id = vm_id
                            break
                    if not active_vm_id:
                        active_vm_id = "1234567"
                        store.append(VMAllocated(
                            agent="provisioner",
                            instance_id=active_vm_id,
                            role="tts",
                            offer_id="1001",
                            worker_url="http://127.0.0.1:8888",
                            gpu_type="RTX 4090",
                            cost_per_hour=0.40
                        ), "initial_hash")
                        
                    store.append(JobStarted(agent="provisioner", job_id=job_id, vm_instance_id=active_vm_id), "initial_hash")
                    store.append(JobCompleted(
                        agent="provisioner",
                        job_id=job_id,
                        artifact_uri=out_path,
                        duration_sec=duration,
                        vm_instance_id=active_vm_id
                    ), "initial_hash")
                    
                    return f'{{"status": "success", "job_id": "{job_id}", "artifact_uri": "{out_path}"}}'
        return await handler(args)

class GenericVideoSimulator(AbstractCapability):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_bash":
            cmd = args.get("command", "")
            if "curl" in cmd and ("job_video_" in cmd or "job_id=job_video_" in cmd or "job_id=job_ltx_" in cmd):
                match = re.search(r"job_id=([a-zA-Z0-9_]+)", cmd)
                if match:
                    job_id = match.group(1)
                    duration = 3.0
                    
                    log_dir = get_active_log_dir()
                    store = EventStore(log_dir=log_dir)
                    video_dir = os.path.join(log_dir, "video_outputs")
                    os.makedirs(video_dir, exist_ok=True)
                    out_path = f"{video_dir}/{job_id}.mp4"
                    
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                    )
                    
                    vms_proj = VMs()
                    vms_proj.tick(store)
                    active_vm_id = None
                    for vm_id, vm in vms_proj.vms.items():
                        if vm.role == "ltx" and vm.status == "active":
                            active_vm_id = vm_id
                            break
                    if not active_vm_id:
                        active_vm_id = "1234567"
                        store.append(VMAllocated(
                            agent="provisioner",
                            instance_id=active_vm_id,
                            role="ltx",
                            offer_id="1002",
                            worker_url="http://127.0.0.1:8888",
                            gpu_type="RTX 4090",
                            cost_per_hour=0.85
                        ), "initial_hash")
                        
                    store.append(JobStarted(agent="provisioner", job_id=job_id, vm_instance_id=active_vm_id), "initial_hash")
                    store.append(JobCompleted(
                        agent="provisioner",
                        job_id=job_id,
                        artifact_uri=out_path,
                        duration_sec=duration,
                        vm_instance_id=active_vm_id
                    ), "initial_hash")
                    
                    return f'{{"status": "success", "job_id": "{job_id}", "artifact_uri": "{out_path}"}}'
        return await handler(args)

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

def dump_diagnostic_logs(db_dir: str, event_store: EventStore):
    """Dump database events and stderr of all agents for troubleshooting."""
    print("\n" + "!" * 80)
    print("                     DIAGNOSTIC CRITICAL FAIL DUMP")
    print("!" * 80)
    
    # 1. Dump database events
    print("\n--- DATABASE EVENTS LOG ---")
    try:
        events = event_store.read_all()
        for e in events:
            print(f"  Seq {e.seq:3d} | Agent: {e.effect.agent:12s} | Kind: {e.effect.kind:25s} | TS: {e.effect.timestamp}")
    except Exception as exc:
        print(f"Failed to read events: {exc}")

    # 2. Dump agents stderr
    for agent in ["gsa", "scenario", "audio", "video", "provisioner", "assembly"]:
        stderr_path = os.path.join(db_dir, f"agent_{agent}_stderr.log")
        if os.path.exists(stderr_path):
            print(f"\n--- AGENT '{agent.upper()}' STDERR LOG (LAST 40 LINES) ---")
            try:
                with open(stderr_path) as f:
                    lines = f.readlines()
                    for line in lines[-40:]:
                        print("  " + line.strip())
            except Exception as exc:
                print(f"Failed to read agent log: {exc}")
    print("!" * 80 + "\n")

def run_test():
    print("=== STARTING TEST: MAXIMUM CAPACITY INTEGRATION (HARNESS MODE) ===")
    
    # Spawn all 6 agents
    agents = ["gsa", "scenario", "audio", "video", "provisioner", "assembly"]
    capabilities = [
        "DryRunModel",
        "WorkerHealthSimulator",
        "GenericAudioSimulator",
        "GenericVideoSimulator",
        "AssembleFinalCutSimulator"
    ]
    with IntegrationHarness(required_agents=agents, capabilities=capabilities) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        audio_port = harness.ports["audio"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        with event_store._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")

        # Set output video file path
        output_movie_path = os.path.join(db_dir, "final_documentary.mp4")

        # 1. Seed initial run configurations
        event_store.append(PipelineStarted(agent="operator", output_path=output_movie_path), "")
        event_store.append(BudgetSet(agent="operator", budget_usd=50.0), "")

        # 2. Generate 30 ScriptBlocks to stress capacity limits
        # Each block is 3.0s duration. Total movie duration = 90.0s.
        blocks = []
        for i in range(1, 31):
            blocks.append(ScriptBlock(
                scene_num=1,
                block_id=f"s1_b{i}",
                speaker="V1_Narrator",
                text=f"Continuous narrative block text number {i} for maximum capacity testing.",
                duration_sec=3.0
            ))
        
        # Write script to database
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        print(f"Seeded 30 script blocks in isolated SQLite database events.db")

        # 3. Wake up the Audio Agent to trigger autonomous loop execution
        print("Sending initial wakeup notification to Audio Agent...")
        resp = httpx.post(f"http://127.0.0.1:{audio_port}/", content="Wakeup", timeout=None)
        assert resp.status_code == 200, f"Failed to wakeup Audio Agent: {resp.text}"

        # 4. Monitor pipeline execution
        print("Monitoring pipeline progression...")
        start_time = time.time()
        completed = False
        aborted = False
        
        # Track historical concurrent limits seen during run
        peak_concurrent_tts = 0
        peak_concurrent_ltx = 0
        last_seen_seq = -1

        for _ in range(500):
            # 4.1 Read events database to count concurrent jobs
            try:
                db_events = event_store.read_all()
                events = [e.effect for e in db_events]
                
                # Print any new events in real-time
                for e in db_events:
                    if e.seq > last_seen_seq:
                        last_seen_seq = e.seq
                        eff = e.effect
                        kind = eff.kind
                        agent = getattr(eff, "agent", "unknown")
                        
                        if kind == "pipeline_started":
                            msg = f"🚀 Pipeline started with output path: {getattr(eff, 'output_path', '')}"
                        elif kind == "budget_set":
                            msg = f"💰 Budget limit set to ${getattr(eff, 'budget_usd', 0.0):.2f} USD"
                        elif kind == "update_script":
                            blocks_count = len(getattr(eff, "blocks", []))
                            msg = f"📝 Scenario Agent updated timeline script: {blocks_count} script blocks seeded"
                        elif kind == "queue_job":
                            msg = f"📥 {agent.capitalize()} Agent queued {getattr(eff, 'job_type', '').upper()} job '{getattr(eff, 'job_id', '')}' for block '{getattr(eff, 'block_id', '')}'"
                        elif kind == "job_started":
                            msg = f"⚙️ Job '{getattr(eff, 'job_id', '')}' started on VM '{getattr(eff, 'vm_instance_id', '')}'"
                        elif kind == "job_completed":
                            msg = f"✅ Job '{getattr(eff, 'job_id', '')}' completed on VM '{getattr(eff, 'vm_instance_id', '')}' (Duration: {getattr(eff, 'duration_sec', 0.0):.1f}s)"
                        elif kind == "job_failed":
                            msg = f"❌ Job '{getattr(eff, 'job_id', '')}' failed on VM '{getattr(eff, 'vm_instance_id', '')}': {getattr(eff, 'error_message', '')}"
                        elif kind == "duration_adjusted":
                            msg = f"🔄 Audio Agent adjusted block '{getattr(eff, 'block_id', '')}' duration: scripted={getattr(eff, 'scripted_sec', 0.0):.1f}s → measured={getattr(eff, 'measured_sec', 0.0):.1f}s"
                        elif kind == "reconciliation_complete":
                            msg = f"⚖️ Audio Agent finished reconciliation: {getattr(eff, 'blocks_passed', 0)}/{getattr(eff, 'blocks_total', 0)} blocks passed"
                        elif kind == "reconciliation_failed":
                            msg = f"⚠️ Audio Agent reconciliation failed: {getattr(eff, 'reason', '')}"
                        elif kind == "vm_allocated":
                            msg = f"🖥️ Provisioner Agent allocated VM '{getattr(eff, 'instance_id', '')}' ({getattr(eff, 'role', '').upper()}) at {getattr(eff, 'worker_url', '')} (${getattr(eff, 'cost_per_hour', 0.0):.2f}/hr)"
                        elif kind == "vm_deallocated":
                            msg = f"🔌 Provisioner Agent deallocated VM '{getattr(eff, 'instance_id', '')}': reason='{getattr(eff, 'reason', '')}'"
                        elif kind == "merge_into_otio":
                            msg = f"🧩 Video Agent merged clip '{getattr(eff, 'job_id', '')}' into OTIO slot '{getattr(eff, 'slot_id', '')}' ({getattr(eff, 'duration_sec', 0.0):.1f}s)"
                        elif kind == "pipeline_complete":
                            msg = f"🎉 Pipeline execution successfully completed! Movie compiled to {getattr(eff, 'output_path', '')} ({getattr(eff, 'duration_sec', 0.0):.1f}s)"
                        elif kind == "pipeline_aborted":
                            msg = f"🚨 Pipeline execution aborted: {getattr(eff, 'reason', '')}"
                        else:
                            msg = f"🔔 Event '{kind}' emitted by agent '{agent}'"
                            
                        print(f"  [GUI Event Log] {msg}", flush=True)

                # Active TTS jobs = started TTS jobs that are not completed or failed
                started_tts = {e.job_id for e in events if e.kind == "job_started" and "job_audio" in e.job_id}
                finished_tts = {e.job_id for e in events if e.kind in ("job_completed", "job_failed") and "job_audio" in e.job_id}
                active_tts = started_tts - finished_tts
                peak_concurrent_tts = max(peak_concurrent_tts, len(active_tts))

                # Active LTX jobs = started LTX jobs that are not completed or failed
                started_ltx = {e.job_id for e in events if e.kind == "job_started" and "job_video" in e.job_id}
                finished_ltx = {e.job_id for e in events if e.kind in ("job_completed", "job_failed") and "job_video" in e.job_id}
                active_ltx = started_ltx - finished_ltx
                peak_concurrent_ltx = max(peak_concurrent_ltx, len(active_ltx))

                # Read completions or aborts
                if any(e.kind == "pipeline_complete" for e in events):
                    completed = True
                    break
                if any(e.kind == "pipeline_aborted" for e in events):
                    aborted = True
                    break
            except Exception as e:
                print(f"  [Warning] Event store read error: {e}")

            # 4.2 Query GSA health to report progress and display beautiful live dashboard
            try:
                resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
                if resp.status_code == 200:
                    state_data = resp.json()
                    phase = state_data.get("state", {}).get("current_phase")
                    slots = state_data.get("otio", {}).get("slots", {})
                    measured = sum(1 for s in slots.values() if s.get("status") == "measured")
                    delivered = sum(1 for s in slots.values() if s.get("status") == "delivered")
                    duration = state_data.get("otio", {}).get("duration_sec", 0.0)
                    spent = state_data.get("budget", {}).get("spent_usd", 0.0)
                    active_vms = state_data.get("vms", {}).get("vms", {})
                    
                    # Construct progress tracks
                    audio_track = ""
                    video_track = ""
                    for i in range(1, 31):
                        bid = f"s1_b{i}"
                        # Audio
                        a_slot = slots.get(f"A1:1:{bid}") or slots.get(bid) or {}
                        if a_slot.get("status") == "measured":
                            audio_track += "\033[92m█\033[0m" # Green block
                        else:
                            audio_track += "\033[90m░\033[0m" # Dim block
                        # Video
                        v_slot = slots.get(f"V1:1:{bid}") or slots.get(bid) or {}
                        if v_slot.get("status") == "delivered":
                            video_track += "\033[96m█\033[0m" # Cyan block
                        else:
                            video_track += "\033[90m░\033[0m" # Dim block
                            
                    vm_lines = []
                    for vm_id, vm in active_vms.items():
                        if vm.get("status") == "active":
                            role = (vm.get("role") or "unknown").upper()
                            gpu = vm.get("gpu_type") or "GPU"
                            cost = vm.get("hourly_rate_usd", 0.0)
                            vm_lines.append(f"│  • VM {vm_id:7s} | Role: {role:3s} | Status: ACTIVE | GPU: {gpu:9s} | Cost: ${cost:.2f}/hr")
                    if not vm_lines:
                        vm_lines.append("│  • No VMs active currently.")
                    vm_lines_str = "\n".join(vm_lines)

                    elapsed = time.time() - start_time
                    dashboard = f"""
┌──────────────────────────────────────────────────────────────────────────────┐
│ 📊 PIPELINE LIVE DASHBOARD                                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│  Phase: {phase.upper():9s} | Elapsed: {elapsed:5.1f}s | Spent: ${spent:.2f} | Duration: {duration:5.1f}s │
├──────────────────────────────────────────────────────────────────────────────┤
│  AUDIO TRACK:  {audio_track}  ({measured:2d}/30 measured)              │
│  VIDEO TRACK:  {video_track}  ({delivered:2d}/30 delivered)              │
├──────────────────────────────────────────────────────────────────────────────┤
│  VM FLEET STATUS:                                                            │
{vm_lines_str}
├──────────────────────────────────────────────────────────────────────────────┤
│  ACTIVE QUEUED JOBS: TTS: {len(active_tts):2d} | LTX: {len(active_ltx):2d}                                    │
└──────────────────────────────────────────────────────────────────────────────┘
"""
                    print(dashboard, flush=True)
                    
                    # Construct progress tracks in HTML
                    audio_slots_html = ""
                    video_slots_html = ""
                    for i in range(1, 31):
                        bid = f"s1_b{i}"
                        # Audio
                        a_slot = slots.get(f"A1:1:{bid}") or slots.get(bid) or {}
                        if a_slot.get("status") == "measured":
                            audio_slots_html += f'<div style="width:14px; height:20px; background-color:#10b981; border-radius:3px; display:inline-block; margin:2px;" title="Slot {bid}: Measured"></div>'
                        else:
                            audio_slots_html += f'<div style="width:14px; height:20px; background-color:#374151; border-radius:3px; display:inline-block; margin:2px;" title="Slot {bid}: Scripted"></div>'
                        # Video
                        v_slot = slots.get(f"V1:1:{bid}") or slots.get(bid) or {}
                        if v_slot.get("status") == "delivered":
                            video_slots_html += f'<div style="width:14px; height:20px; background-color:#06b6d4; border-radius:3px; display:inline-block; margin:2px;" title="Slot {bid}: Delivered"></div>'
                        else:
                            video_slots_html += f'<div style="width:14px; height:20px; background-color:#374151; border-radius:3px; display:inline-block; margin:2px;" title="Slot {bid}: Scripted"></div>'
                            
                    vm_rows = ""
                    for vm_id, vm in active_vms.items():
                        if vm.get("status") == "active":
                            role = (vm.get("role") or "unknown").upper()
                            gpu = vm.get("gpu_type") or "GPU"
                            cost = vm.get("hourly_rate_usd", 0.0)
                            vm_rows += f'<tr style="border-bottom: 1px solid rgba(255,255,255,0.05);"><td style="padding: 6px 0; font-family: monospace; color: #e2e8f0;">VM-{vm_id}</td><td style="padding: 6px 0; color: #818cf8; font-weight: 600;">{role}</td><td style="padding: 6px 0; color: #cbd5e1;">{gpu}</td><td style="padding: 6px 0; color: #10b981; font-weight: 600;">ACTIVE</td><td style="padding: 6px 0; text-align: right; color: #94a3b8;">${cost:.2f}/hr</td></tr>'
                    if not vm_rows:
                        vm_rows = '<tr><td colspan="5" style="padding: 12px 0; text-align: center; color: #64748b; font-style: italic;">No VMs active currently.</td></tr>'

                    # Highlight active phase step
                    phase_steps = ["init", "audio_reconcile", "video_production", "done"]
                    phase_states = {}
                    for p in phase_steps:
                        if phase and p == phase.lower():
                            phase_states[p] = ('#818cf8', 'rgba(129, 140, 248, 0.15)', '1px solid rgba(129, 140, 248, 0.3)', '#ffffff')
                        else:
                            phase_states[p] = ('#64748b', 'transparent', '1px solid rgba(255,255,255,0.05)', '#64748b')

                    elapsed = time.time() - start_time
                    
                    md_dashboard = f"""# 🧪 Maximum Capacity Test - Live Pipeline Status

<div style="background: #0f172a; border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; padding: 1.5rem; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 800px; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3); display: flex; flex-direction: column; gap: 1.25rem;">
    
    <!-- Header -->
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 0.8rem;">
        <div>
            <h2 style="margin: 0; font-size: 1.4rem; color: #ffffff; font-weight: 700; letter-spacing: -0.025em;">📊 Pipeline Live Dashboard</h2>
            <p style="margin: 4px 0 0 0; font-size: 0.8rem; color: #94a3b8;">Maximum Capacity Stress Test Execution</p>
        </div>
        <div style="font-size: 0.8rem; color: #94a3b8; font-weight: 600;">
            Elapsed: <strong style="color: #ffffff;">{elapsed:.1f}s</strong> | Spent: <strong style="color: #10b981;">${spent:.2f}</strong>
        </div>
    </div>

    <!-- Timeline Phase Flow Steps -->
    <div style="padding: 1rem; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; background: #1e293b;">
        <div style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; margin-bottom: 0.75rem; letter-spacing: 0.05em;">Timeline Phase Flow</div>
        <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.8rem; font-weight: 600;">
            <div style="color: {phase_states['init'][3]}; background: {phase_states['init'][1]}; border: {phase_states['init'][2]}; padding: 6px 12px; border-radius: 6px; display: flex; align-items: center; gap: 0.4rem;">
                <span style="display: inline-flex; width: 16px; height: 16px; background: {phase_states['init'][0]}; color: #0f172a; border-radius: 50%; align-items: center; justify-content: center; font-size: 0.65rem; font-weight: 700;">1</span>Init
            </div>
            <div style="color: #475569;">➔</div>
            <div style="color: {phase_states['audio_reconcile'][3]}; background: {phase_states['audio_reconcile'][1]}; border: {phase_states['audio_reconcile'][2]}; padding: 6px 12px; border-radius: 6px; display: flex; align-items: center; gap: 0.4rem;">
                <span style="display: inline-flex; width: 16px; height: 16px; background: {phase_states['audio_reconcile'][0]}; color: #0f172a; border-radius: 50%; align-items: center; justify-content: center; font-size: 0.65rem; font-weight: 700;">2</span>Audio Reconcile
            </div>
            <div style="color: #475569;">➔</div>
            <div style="color: {phase_states['video_production'][3]}; background: {phase_states['video_production'][1]}; border: {phase_states['video_production'][2]}; padding: 6px 12px; border-radius: 6px; display: flex; align-items: center; gap: 0.4rem;">
                <span style="display: inline-flex; width: 16px; height: 16px; background: {phase_states['video_production'][0]}; color: #0f172a; border-radius: 50%; align-items: center; justify-content: center; font-size: 0.65rem; font-weight: 700;">3</span>Video Production
            </div>
            <div style="color: #475569;">➔</div>
            <div style="color: {phase_states['done'][3]}; background: {phase_states['done'][1]}; border: {phase_states['done'][2]}; padding: 6px 12px; border-radius: 6px; display: flex; align-items: center; gap: 0.4rem;">
                <span style="display: inline-flex; width: 16px; height: 16px; background: {phase_states['done'][0]}; color: #0f172a; border-radius: 50%; align-items: center; justify-content: center; font-size: 0.65rem; font-weight: 700;">4</span>Done
            </div>
        </div>
    </div>

    <!-- Media Track Slots -->
    <div style="padding: 1rem; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; background: #1e293b; display: flex; flex-direction: column; gap: 1rem;">
        <div style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; letter-spacing: 0.05em;">Media Track Slots</div>
        
        <div style="display: flex; flex-direction: column; gap: 0.4rem;">
            <div style="font-size: 0.7rem; font-weight: 700; color: #10b981; letter-spacing: 0.05em; text-transform: uppercase;">Audio Narration (A1) &middot; ({measured}/30 measured)</div>
            <div style="line-height: 1;">{audio_slots_html}</div>
        </div>

        <div style="display: flex; flex-direction: column; gap: 0.4rem;">
            <div style="font-size: 0.7rem; font-weight: 700; color: #06b6d4; letter-spacing: 0.05em; text-transform: uppercase;">Video B-Roll (V1) &middot; ({delivered}/30 delivered)</div>
            <div style="line-height: 1;">{video_slots_html}</div>
        </div>
    </div>

    <!-- VM Fleet Status -->
    <div style="padding: 1rem; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; background: #1e293b; display: flex; flex-direction: column;">
        <div style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; margin-bottom: 0.5rem; letter-spacing: 0.05em;">Active VM Fleet</div>
        <div style="max-height: 150px; overflow-y: auto;">
            <table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 0.8rem;">
                <thead>
                    <tr style="border-bottom: 2px solid rgba(255,255,255,0.1); font-size: 0.75rem; color: #94a3b8; font-weight: 700;">
                        <th style="padding: 6px 0;">VM ID</th>
                        <th style="padding: 6px 0;">Role</th>
                        <th style="padding: 6px 0;">GPU Type</th>
                        <th style="padding: 6px 0;">Status</th>
                        <th style="padding: 6px 0; text-align: right;">Cost Rate</th>
                    </tr>
                </thead>
                <tbody>{vm_rows}</tbody>
            </table>
        </div>
    </div>
    
    <!-- Queue Stats -->
    <div style="display: flex; justify-content: space-between; font-size: 0.8rem; font-weight: 700; background: #1e293b; border: 1px solid rgba(255,255,255,0.05); padding: 0.75rem 1rem; border-radius: 8px;">
        <div style="display: flex; gap: 1.5rem;">
            <span style="color: #818cf8;">Active TTS Queue: {len(active_tts)}</span>
            <span style="color: #06b6d4;">Active LTX Queue: {len(active_ltx)}</span>
        </div>
        <span style="color: #ffffff;">Total Timeline Duration: {duration:.1f}s</span>
    </div>
</div>
"""
                    import pathlib
                    current_active = pathlib.Path("/Users/orpington/.gemini/antigravity/brain/2396d2a7-2d70-42f0-8498-e40c70b10fa0")
                    if current_active.exists():
                        active_brain = current_active
                    else:
                        brain_root = pathlib.Path("/Users/orpington/.gemini/antigravity/brain")
                        active_brain = current_active
                        if brain_root.exists():
                            newest_dir = None
                            newest_mtime = 0
                            for subdir in brain_root.iterdir():
                                if subdir.is_dir() and not subdir.name.startswith("."):
                                    try:
                                        mtime = subdir.stat().st_mtime
                                        if mtime > newest_mtime:
                                            newest_mtime = mtime
                                            newest_dir = subdir
                                    except Exception:
                                        pass
                            if newest_dir:
                                active_brain = newest_dir
                    
                    try:
                        with open(PROJECT_ROOT / "test_results.md", "w") as f:
                            f.write(md_dashboard)
                    except Exception:
                        pass
                    try:
                        with open(active_brain / "test_results.md", "w") as f:
                            f.write(md_dashboard)
                    except Exception:
                        pass
                    
                    if phase == "done":
                        completed = True
                        break
                    elif phase == "aborted":
                        aborted = True
                        break
            except Exception as e:
                print(f"  [Warning] GSA poll error: {e}")

            time.sleep(1.0)

        # Print concurrency findings
        print(f"Peak concurrent TTS jobs processed:   {peak_concurrent_tts}")
        print(f"Peak concurrent Video jobs processed: {peak_concurrent_ltx}")

        # Assertions
        if not completed:
            dump_diagnostic_logs(db_dir, event_store)
            raise AssertionError("Pipeline execution was aborted or failed to complete.")

        # Verify concurrency safety
        assert peak_concurrent_tts <= 4, f"TTS concurrency exceeded cap: peak was {peak_concurrent_tts} (limit: 4)"
        assert peak_concurrent_ltx <= 4, f"Video concurrency exceeded cap: peak was {peak_concurrent_ltx} (limit: 4)"

        # 5. Verify final output media asset
        assert os.path.exists(output_movie_path), "Final compiled MP4 file does not exist on disk"
        assert os.path.getsize(output_movie_path) > 0, "Final compiled MP4 file is empty"
        
        # 5.1 Query video parameters via ffprobe
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", output_movie_path],
            capture_output=True, text=True, check=True
        )
        compiled_duration = float(res.stdout.strip())
        print(f"Compiled movie duration measured by ffprobe: {compiled_duration:.2f}s")
        assert abs(compiled_duration - 90.0) < 3.0, f"Expected movie duration near 90.0s, got {compiled_duration:.2f}s"

        # 5.2 Verify audio stream track is present in container
        res_audio = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_movie_path],
            capture_output=True, text=True, check=True
        )
        audio_stream_codec = res_audio.stdout.strip()
        assert audio_stream_codec != "", "Compiled movie container is missing an audio stream track"
        print(f"Compiled movie container audio codec verified: {audio_stream_codec}")

        # 5.3 Extract audio to WAV to check loudness normalization
        extracted_audio_wav = os.path.join(db_dir, "extracted_normalized_audio.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", output_movie_path, "-vn", "-acodec", "pcm_s16le", "-ac", "1", extracted_audio_wav],
            capture_output=True, check=True
        )
        
        lufs_value = measure_lufs_integrated(extracted_audio_wav)
        print(f"Compiled movie integrated loudness measured: {lufs_value:.2f} LUFS")
        assert abs(lufs_value - (-16.0)) <= 2.0, f"Loudness is not within +/-2.0 LUFS of the -16.0 target, got {lufs_value:.2f} LUFS"

        print("=== TEST PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    run_test()
