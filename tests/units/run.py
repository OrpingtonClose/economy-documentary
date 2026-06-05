import os
import sys
import time
import socket
import threading
import queue
import webbrowser
import httpx
import uvicorn
import pathlib
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Setup Python paths
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "server"))

from tests.units.test_gsa_wal_concurrency_isolation import test_gsa_wal_concurrency_isolation
from tests.units.test_scenario_agent_live_prompt_turn import test_scenario_agent_live_prompt_turn
from tests.units.test_audio_agent_tts_job_queueing import test_audio_agent_tts_job_queueing
from tests.units.test_video_agent_ltx_job_queueing import test_video_agent_ltx_job_queueing
from tests.units.test_provisioner_vast_offers_search import test_provisioner_vast_offers_search
from tests.units.test_vast_create_and_destroy_lifecycle import test_vast_create_and_destroy_lifecycle
from tests.units.test_ssh_handshake_and_docker_health import test_ssh_handshake_and_docker_health
from tests.units.test_audio_loudness_normalizer_compilation import test_audio_loudness_normalizer_compilation
from tests.units.test_coordinate_timeline_dynamic_drift import test_coordinate_timeline_dynamic_drift
from tests.units.test_budget_limit_aborted_gate import test_budget_limit_aborted_gate
from tests.units.test_agent_chooses_vm_size_and_provisioner_allocates import test_agent_chooses_vm_size_and_provisioner_allocates
from tests.units.test_provisioner_escalation_policy import test_provisioner_escalation_policy
from tests.units.test_preemption_and_recovery import test_preemption_and_recovery
from tests.units.test_localized_recovery_and_retry import test_localized_recovery_and_retry
from tests.units.test_accumulative_drift_correction import test_accumulative_drift_correction
from tests.units.test_provisioner_cli_command_invocation import test_provisioner_cli_command_invocation
from tests.units.test_assemble_final_cut_execution import test_assemble_final_cut_execution
from tests.units.test_real_qwen3_tts_script_execution import test_real_qwen3_tts_script_execution
from tests.units.test_real_ltx_video_script_execution import test_real_ltx_video_script_execution
from tests.units.test_parse_duration_all_formats import test_parse_duration_all_formats
from tests.units.test_effect_pydantic_round_trip import test_effect_pydantic_round_trip
from tests.units.test_event_store_append_replay_ordering import test_event_store_append_replay_ordering
from tests.units.test_event_store_idempotent_dedup import test_event_store_idempotent_dedup
from tests.units.test_event_store_read_since_window import test_event_store_read_since_window
from tests.units.test_timeline_projection_script_to_slots import test_timeline_projection_script_to_slots
from tests.units.test_timeline_projection_merge_and_delivered import test_timeline_projection_merge_and_delivered
from tests.units.test_timeline_projection_delete_scene import test_timeline_projection_delete_scene
from tests.units.test_timeline_projection_reorder_scenes import test_timeline_projection_reorder_scenes
from tests.units.test_timeline_validation_suite import test_timeline_validation_suite
from tests.units.test_jobs_projection_full_lifecycle import test_jobs_projection_full_lifecycle
from tests.units.test_jobs_projection_dirty_clean_tracking import test_jobs_projection_dirty_clean_tracking
from tests.units.test_vm_projection_multi_role_fleet import test_vm_projection_multi_role_fleet
from tests.units.test_budget_projection_exceeded_detection import test_budget_projection_exceeded_detection
from tests.units.test_state_projection_full_phase_machine import test_state_projection_full_phase_machine
from tests.units.test_coordinate_timeline_cascade_and_overlap import test_coordinate_timeline_cascade_and_overlap
from tests.units.test_bdd_tts_fleet_cold_start import test_bdd_tts_fleet_cold_start
from tests.units.test_bdd_single_block_tts_inference import test_bdd_single_block_tts_inference
from tests.units.test_bdd_multi_block_tts_reconciliation import test_bdd_multi_block_tts_reconciliation
from tests.units.test_bdd_voice_continuity_across_scenes import test_bdd_voice_continuity_across_scenes
from tests.units.test_bdd_ltx_fleet_scale_up import test_bdd_ltx_fleet_scale_up
from tests.units.test_bdd_single_clip_video_generation import test_bdd_single_clip_video_generation
from tests.units.test_bdd_multi_scene_video_otio_assembly import test_bdd_multi_scene_video_otio_assembly
from tests.units.test_bdd_audio_video_duration_alignment import test_bdd_audio_video_duration_alignment
from tests.units.test_bdd_tts_retry_after_failure import test_bdd_tts_retry_after_failure
from tests.units.test_bdd_vm_preemption_recovery import test_bdd_vm_preemption_recovery
from tests.units.test_bdd_budget_gated_provisioning import test_bdd_budget_gated_provisioning
from tests.units.test_bdd_script_revision_selective_requeue import test_bdd_script_revision_selective_requeue
from tests.units.test_bdd_final_assembly_real_media import test_bdd_final_assembly_real_media
from tests.units.test_bdd_partial_failure_isolated_recovery import test_bdd_partial_failure_isolated_recovery
from tests.units.test_bdd_full_fleet_teardown_cost_accounting import test_bdd_full_fleet_teardown_cost_accounting
from tests.units.test_perplexity_verify_live import test_perplexity_verify_live
from tests.units import test_max_capacity_pipeline


# Thread-safe log collection
log_queue = queue.Queue()
logs_list = []

# List of all test cases: (name, function, category)

TEST_CASES = [
    # 1. Simulation Covers (Consequential Claims subset)
    ("test_gsa_wal_concurrency_isolation", test_gsa_wal_concurrency_isolation, "Simulation Cover"),
    ("test_scenario_agent_live_prompt_turn", test_scenario_agent_live_prompt_turn, "Simulation Cover"),
    ("test_audio_agent_tts_job_queueing", test_audio_agent_tts_job_queueing, "Simulation Cover"),
    ("test_video_agent_ltx_job_queueing", test_video_agent_ltx_job_queueing, "Simulation Cover"),
    ("test_provisioner_vast_offers_search", test_provisioner_vast_offers_search, "Simulation Cover"),
    ("test_vast_create_and_destroy_lifecycle", test_vast_create_and_destroy_lifecycle, "Simulation Cover"),
    ("test_ssh_handshake_and_docker_health", test_ssh_handshake_and_docker_health, "Simulation Cover"),
    ("test_audio_loudness_normalizer_compilation", test_audio_loudness_normalizer_compilation, "Simulation Cover"),
    ("test_coordinate_timeline_dynamic_drift", test_coordinate_timeline_dynamic_drift, "Simulation Cover"),
    ("test_budget_limit_aborted_gate", test_budget_limit_aborted_gate, "Simulation Cover"),

    # 1b. Consequential Claims (non-covering subset)
    ("test_agent_chooses_vm_size_and_provisioner_allocates", test_agent_chooses_vm_size_and_provisioner_allocates, "Consequential Claims"),
    ("test_provisioner_escalation_policy", test_provisioner_escalation_policy, "Consequential Claims"),
    ("test_preemption_and_recovery", test_preemption_and_recovery, "Consequential Claims"),
    ("test_localized_recovery_and_retry", test_localized_recovery_and_retry, "Consequential Claims"),
    ("test_accumulative_drift_correction", test_accumulative_drift_correction, "Consequential Claims"),
    ("test_provisioner_cli_command_invocation", test_provisioner_cli_command_invocation, "Consequential Claims"),
    ("test_assemble_final_cut_execution", test_assemble_final_cut_execution, "Consequential Claims"),
    ("test_real_qwen3_tts_script_execution", test_real_qwen3_tts_script_execution, "Consequential Claims"),
    ("test_real_ltx_video_script_execution", test_real_ltx_video_script_execution, "Consequential Claims"),

    # 2. Process Tests
    ("test_parse_duration_all_formats", test_parse_duration_all_formats, "Process Tests"),
    ("test_effect_pydantic_round_trip", test_effect_pydantic_round_trip, "Process Tests"),
    ("test_event_store_append_replay_ordering", test_event_store_append_replay_ordering, "Process Tests"),
    ("test_event_store_idempotent_dedup", test_event_store_idempotent_dedup, "Process Tests"),
    ("test_event_store_read_since_window", test_event_store_read_since_window, "Process Tests"),
    ("test_timeline_projection_script_to_slots", test_timeline_projection_script_to_slots, "Process Tests"),
    ("test_timeline_projection_merge_and_delivered", test_timeline_projection_merge_and_delivered, "Process Tests"),
    ("test_timeline_projection_delete_scene", test_timeline_projection_delete_scene, "Process Tests"),
    ("test_timeline_projection_reorder_scenes", test_timeline_projection_reorder_scenes, "Process Tests"),
    ("test_timeline_validation_suite", test_timeline_validation_suite, "Process Tests"),
    ("test_jobs_projection_full_lifecycle", test_jobs_projection_full_lifecycle, "Process Tests"),
    ("test_jobs_projection_dirty_clean_tracking", test_jobs_projection_dirty_clean_tracking, "Process Tests"),
    ("test_vm_projection_multi_role_fleet", test_vm_projection_multi_role_fleet, "Process Tests"),
    ("test_budget_projection_exceeded_detection", test_budget_projection_exceeded_detection, "Process Tests"),
    ("test_state_projection_full_phase_machine", test_state_projection_full_phase_machine, "Process Tests"),
    ("test_coordinate_timeline_cascade_and_overlap", test_coordinate_timeline_cascade_and_overlap, "Process Tests"),

    # 3. Maximum Capacity
    ("Maximum Capacity Test", test_max_capacity_pipeline.run_test, "Maximum Capacity"),

    # 4. BDD Integration Tests
    ("test_bdd_tts_fleet_cold_start", test_bdd_tts_fleet_cold_start, "BDD Integration"),
    ("test_bdd_single_block_tts_inference", test_bdd_single_block_tts_inference, "BDD Integration"),
    ("test_bdd_multi_block_tts_reconciliation", test_bdd_multi_block_tts_reconciliation, "BDD Integration"),
    ("test_bdd_voice_continuity_across_scenes", test_bdd_voice_continuity_across_scenes, "BDD Integration"),
    ("test_bdd_ltx_fleet_scale_up", test_bdd_ltx_fleet_scale_up, "BDD Integration"),
    ("test_bdd_single_clip_video_generation", test_bdd_single_clip_video_generation, "BDD Integration"),
    ("test_bdd_multi_scene_video_otio_assembly", test_bdd_multi_scene_video_otio_assembly, "BDD Integration"),
    ("test_bdd_audio_video_duration_alignment", test_bdd_audio_video_duration_alignment, "BDD Integration"),
    ("test_bdd_tts_retry_after_failure", test_bdd_tts_retry_after_failure, "BDD Integration"),
    ("test_bdd_vm_preemption_recovery", test_bdd_vm_preemption_recovery, "BDD Integration"),
    ("test_bdd_budget_gated_provisioning", test_bdd_budget_gated_provisioning, "BDD Integration"),
    ("test_bdd_script_revision_selective_requeue", test_bdd_script_revision_selective_requeue, "BDD Integration"),
    ("test_bdd_final_assembly_real_media", test_bdd_final_assembly_real_media, "BDD Integration"),
    ("test_bdd_partial_failure_isolated_recovery", test_bdd_partial_failure_isolated_recovery, "BDD Integration"),
    ("test_bdd_full_fleet_teardown_cost_accounting", test_bdd_full_fleet_teardown_cost_accounting, "BDD Integration"),
    ("test_perplexity_verify_live", test_perplexity_verify_live, "BDD Integration"),
]

# State tracking
suite_status = "running"
suite_completed = False
stats = {"passed": 0, "failed": 0, "skipped": 0, "pending": len(TEST_CASES)}
history = []
active_test_name = "Initializing..."
active_category = "Suite"
start_time = time.time()
end_time = None
auto_close_countdown = 10
ai_summary = "AI Copilot analysis pending logs..."

# Load DeepSeek API key
DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
api_key = None
if os.path.exists(DEEPSEEK_KEY_PATH):
    try:
        with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
    except Exception:
        pass

def get_llm_summary(recent_logs: str) -> str:
    if not api_key:
        return "DeepSeek API key not found. AI Copilot summary disabled."
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    system_prompt = (
        "You are an assistant describing a running test suite. "
        "Review the recent log chunk and summarize what the system is currently doing in one short, clear, friendly sentence. "
        "Focus on the active operation (e.g. provisioning a VM, normalising audio loudness, running BDD assertions). "
        "Be extremely concise (max 15 words) and do not explain the code itself."
    )
    # Strip HTML tags from logs chunk before sending
    import re
    clean_logs = re.sub(r'<[^>]+>', '', recent_logs)
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Logs:\n{clean_logs[-4000:]}"}
        ],
        "temperature": 0.3,
        "max_tokens": 60
    }
    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            return f"AI Copilot: Busy monitoring runs (HTTP {resp.status_code})"
    except Exception:
        return "AI Copilot: Analyzing execution pipeline..."

def trigger_ai_summary_refresh():
    def run():
        global ai_summary
        log_chunk = "".join(logs_list[-50:])
        if log_chunk:
            ai_summary = get_llm_summary(log_chunk)
    threading.Thread(target=run, daemon=True).start()

def ai_summary_worker():
    global ai_summary
    time.sleep(3) # Initial delay
    while not suite_completed:
        log_chunk = "".join(logs_list[-50:])
        if log_chunk:
            ai_summary = get_llm_summary(log_chunk)
        # Sleep 30 seconds
        for _ in range(30):
            if suite_completed:
                break
            time.sleep(1)

# Custom stream wrapper to capture stdout/stderr in real-time
class LiveStreamCapture:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, s):
        if s:
            log_queue.put(s)
            if self.original_stream:
                self.original_stream.write(s)
                self.original_stream.flush()
        return len(s)

    def flush(self):
        if self.original_stream:
            self.original_stream.flush()

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()

original_stdout = sys.stdout
original_stderr = sys.stderr

def log_processor():
    while True:
        try:
            item = log_queue.get()
            # Clean ANSI color codes for HTML representation
            cleaned = (
                item.replace("\033[92m", '<span style="color: #34d399;">')
                    .replace("\033[91m", '<span style="color: #f87171;">')
                    .replace("\033[93m", '<span style="color: #fbbf24;">')
                    .replace("\033[95m", '<span style="color: #c084fc;">')
                    .replace("\033[96m", '<span style="color: #22d3ee;">')
                    .replace("\033[36m", '<span style="color: #22d3ee;">')
                    .replace("\033[34m", '<span style="color: #60a5fa;">')
                    .replace("\033[33m", '<span style="color: #fbbf24;">')
                    .replace("\033[35m", '<span style="color: #f472b6;">')
                    .replace("\033[32m", '<span style="color: #34d399;">')
                    .replace("\033[31m", '<span style="color: #f87171;">')
                    .replace("\033[0m", '</span>')
            )
            logs_list.append(cleaned)
        except queue.Empty:
            if suite_completed and log_queue.empty():
                break

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Master Suite Test Runner GUI</title>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Outfit:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.6);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-running: #6366f1;
            --accent-passed: #10b981;
            --accent-failed: #ef4444;
        }

        body {
            background: radial-gradient(circle at 80% 20%, #1e1b4b 0%, var(--bg-color) 60%);
            color: var(--text-primary);
            font-family: 'Outfit', -apple-system, sans-serif;
            margin: 0;
            padding: 2rem 0;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }

        .card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 2.5rem;
            width: 90%;
            max-width: 950px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            padding-bottom: 1.25rem;
        }

        .header h1 {
            margin: 0;
            font-size: 1.75rem;
            font-weight: 800;
            background: linear-gradient(to right, #a5b4fc, #e0e7ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .status-pill {
            padding: 6px 18px;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            box-shadow: 0 0 15px rgba(99, 102, 241, 0.1);
            transition: all 0.3s ease;
        }

        .status-running {
            background: rgba(99, 102, 241, 0.15);
            color: #818cf8;
            border: 1px solid rgba(99, 102, 241, 0.4);
            box-shadow: 0 0 15px rgba(99, 102, 241, 0.25);
            animation: pulse 2s infinite;
        }

        .status-passed {
            background: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.4);
            box-shadow: 0 0 15px rgba(16, 185, 129, 0.25);
        }

        .status-failed {
            background: rgba(239, 68, 68, 0.15);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.4);
            box-shadow: 0 0 15px rgba(239, 68, 68, 0.25);
        }

        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(99, 102, 241, 0.4); }
            70% { box-shadow: 0 0 0 10px rgba(99, 102, 241, 0); }
            100% { box-shadow: 0 0 0 0 rgba(99, 102, 241, 0); }
        }

        /* Progress track */
        .progress-container {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .progress-label {
            display: flex;
            justify-content: space-between;
            font-size: 0.85rem;
            font-weight: 600;
        }

        .progress-bar {
            height: 12px;
            background: #101524;
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 9999px;
            overflow: hidden;
            display: flex;
        }

        .progress-fill-passed { background: #10b981; height: 100%; transition: width 0.3s ease; }
        .progress-fill-failed { background: #ef4444; height: 100%; transition: width 0.3s ease; }
        .progress-fill-skipped { background: #fbbf24; height: 100%; transition: width 0.3s ease; }

        /* AI Copilot summary card */
        .ai-card {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(168, 85, 247, 0.1) 100%);
            border: 1px solid rgba(99, 102, 241, 0.3);
            border-radius: 16px;
            padding: 1.25rem;
            display: flex;
            align-items: center;
            gap: 1rem;
            box-shadow: 0 8px 20px rgba(99, 102, 241, 0.08);
            position: relative;
            overflow: hidden;
        }

        .ai-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: linear-gradient(to bottom, #818cf8, #c084fc);
        }

        .ai-avatar {
            font-size: 2rem;
            animation: bounce 3s infinite;
        }

        @keyframes bounce {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-5px); }
        }

        .ai-content {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .ai-title {
            font-size: 0.75rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #818cf8;
        }

        .ai-text {
            font-size: 1rem;
            font-weight: 600;
            color: var(--text-primary);
            line-height: 1.4;
        }

        /* Two columns layout for grid & console */
        .main-layout {
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 1.5rem;
            min-height: 380px;
        }

        .test-list-container {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            max-height: 500px;
            overflow-y: auto;
            border-right: 1px solid rgba(255, 255, 255, 0.06);
            padding-right: 1rem;
        }

        .test-list-title {
            font-size: 0.8rem;
            font-weight: 800;
            text-transform: uppercase;
            color: var(--text-secondary);
            margin-bottom: 0.25rem;
        }

        .test-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            border-radius: 8px;
            background: rgba(30, 41, 59, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.03);
            font-size: 0.8rem;
            font-family: monospace;
            transition: all 0.2s ease;
        }

        .test-item.active {
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.3);
            box-shadow: 0 0 10px rgba(99, 102, 241, 0.1);
            font-weight: 700;
        }

        .test-item.covering {
            border-left: 4px solid #c084fc; /* Purple left border for covering tests */
            background: rgba(168, 85, 247, 0.08); /* Transparent purple background */
            box-shadow: inset 0 0 8px rgba(168, 85, 247, 0.15);
        }

        .test-item.covering.active {
            background: rgba(168, 85, 247, 0.15);
            border: 1px solid rgba(168, 85, 247, 0.4);
            border-left: 4px solid #c084fc;
            box-shadow: 0 0 12px rgba(168, 85, 247, 0.3);
        }

        .covering-badge {
            font-family: 'Outfit', sans-serif;
            font-size: 0.6rem;
            font-weight: 800;
            background: linear-gradient(135deg, #a855f7 0%, #7c3aed 100%);
            color: #ffffff;
            padding: 2px 6px;
            border-radius: 4px;
            letter-spacing: 0.05em;
            box-shadow: 0 2px 4px rgba(124, 58, 237, 0.3);
            text-transform: uppercase;
        }

        .test-item-status {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }

        .dot-pending { background: #475569; }
        .dot-running { background: #818cf8; box-shadow: 0 0 8px #818cf8; animation: pulse-dot 1.5s infinite; }
        .dot-passed { background: #10b981; }
        .dot-failed { background: #ef4444; }
        .dot-skipped { background: #fbbf24; }

        @keyframes pulse-dot {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        .console-container {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .console-title {
            font-size: 0.8rem;
            font-weight: 800;
            text-transform: uppercase;
            color: var(--text-secondary);
        }

        .terminal {
            background: #020617;
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 16px;
            padding: 1.25rem;
            font-family: 'Fira Code', monospace;
            font-size: 0.85rem;
            height: 440px;
            overflow-y: auto;
            color: #cbd5e1;
            white-space: pre-wrap;
            word-break: break-all;
            line-height: 1.5;
            box-shadow: inset 0 2px 10px rgba(0, 0, 0, 0.8);
        }

        /* Custom scrollbar */
        .terminal::-webkit-scrollbar, .test-list-container::-webkit-scrollbar {
            width: 6px;
        }
        .terminal::-webkit-scrollbar-track, .test-list-container::-webkit-scrollbar-track {
            background: transparent;
        }
        .terminal::-webkit-scrollbar-thumb, .test-list-container::-webkit-scrollbar-thumb {
            background: #1e293b;
            border-radius: 8px;
        }
        .terminal::-webkit-scrollbar-thumb:hover, .test-list-container::-webkit-scrollbar-thumb:hover {
            background: #334155;
        }

        .footer-message {
            text-align: center;
            font-size: 0.85rem;
            color: #a5b4fc;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="card">
        <!-- Header -->
        <div class="header">
            <div>
                <h1>🧪 Master Test Suite Runner</h1>
                <div class="category" id="active-test-details">Active: Initializing suite...</div>
            </div>
            <div class="status-pill status-running" id="suite-status-badge">RUNNING</div>
        </div>

        <!-- Progress Bar -->
        <div class="progress-container">
            <div class="progress-label">
                <span style="color: var(--text-secondary);">Overall Completion Progress</span>
                <span id="progress-text">0.0% (0/0)</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill-passed" id="bar-passed" style="width: 0%;"></div>
                <div class="progress-fill-failed" id="bar-failed" style="width: 0%;"></div>
                <div class="progress-fill-skipped" id="bar-skipped" style="width: 0%;"></div>
            </div>
        </div>

        <!-- AI Copilot Status Summary -->
        <div class="ai-card">
            <div class="ai-avatar">🤖</div>
            <div class="ai-content">
                <div class="ai-title">AI Status Copilot</div>
                <div class="ai-text" id="ai-summary-text">AI Copilot analyzing suite launch logs...</div>
            </div>
        </div>

        <!-- Main Grid & Console layout -->
        <div class="main-layout">
            <!-- Left: Test List -->
            <div class="test-list-container" id="test-list">
                <div class="test-list-title">Suite Test Cases</div>
                <!-- Populated dynamically -->
            </div>

            <!-- Right: Console logs -->
            <div class="console-container">
                <div class="console-title" id="console-title-el">Live Console Stream</div>
                <div class="terminal" id="console-logs">Initializing log feed...</div>
            </div>
        </div>

        <!-- Auto-close countdown notice -->
        <div class="footer-message" id="countdown-notice" style="display: none;">
            Test suite run complete. Auto-closing GUI in 10 seconds...
        </div>
    </div>

    <script>
        const logsEl = document.getElementById("console-logs");
        const suiteStatusEl = document.getElementById("suite-status-badge");
        const activeDetailsEl = document.getElementById("active-test-details");
        const progressTextEl = document.getElementById("progress-text");
        const barPassedEl = document.getElementById("bar-passed");
        const barFailedEl = document.getElementById("bar-failed");
        const barSkippedEl = document.getElementById("bar-skipped");
        const aiSummaryEl = document.getElementById("ai-summary-text");
        const testListEl = document.getElementById("test-list");
        const consoleTitleEl = document.getElementById("console-title-el");
        const countdownEl = document.getElementById("countdown-notice");

        let lastLogsLength = 0;
        let isScrolledToBottom = true;
        let testListPopulated = false;

        logsEl.addEventListener("scroll", () => {
            isScrolledToBottom = logsEl.scrollHeight - logsEl.clientHeight <= logsEl.scrollTop + 5;
        });

        async function updateState() {
            try {
                const response = await fetch("/api/state");
                const data = await response.json();

                // Update suite status
                suiteStatusEl.className = "status-pill status-" + data.suite_status;
                suiteStatusEl.textContent = data.suite_status;

                // Update active test description
                activeDetailsEl.innerHTML = `Active: <strong style="color: #ffffff;">${data.active_test_name}</strong> &middot; Category: <span style="color: #cbd5e1;">${data.active_category}</span>`;
                consoleTitleEl.textContent = `Live Console Stream: ${data.active_test_name}`;

                // Update progress metrics
                const total = data.total_tests;
                const completed = data.passed + data.failed + data.skipped;
                const pct = total > 0 ? (completed / total * 100) : 0.0;
                progressTextEl.textContent = `${pct.toFixed(1)}% (${completed}/${total}) [Passed: ${data.passed} | Failed: ${data.failed}]`;

                if (total > 0) {
                    barPassedEl.style.width = (data.passed / total * 100) + "%";
                    barFailedEl.style.width = (data.failed / total * 100) + "%";
                    barSkippedEl.style.width = (data.skipped / total * 100) + "%";
                }

                // AI Summary
                aiSummaryEl.textContent = data.ai_summary;

                // Render test list
                renderTestList(data.test_cases, data.active_test_name);

                // Update logs
                if (data.logs.length !== lastLogsLength) {
                    logsEl.innerHTML = data.logs;
                    lastLogsLength = data.logs.length;
                    
                    if (isScrolledToBottom) {
                        logsEl.scrollTop = logsEl.scrollHeight;
                    }
                }

                // Countdown auto-close
                if (data.suite_completed) {
                    countdownEl.style.display = "block";
                    const isSuccess = data.failed === 0;
                    const statusText = isSuccess ? "ALL TESTS PASSED" : `${data.failed} TEST(S) FAILED`;
                    const color = isSuccess ? "#34d399" : "#f87171";
                    countdownEl.innerHTML = `<span style="color: ${color}; font-weight: 800;">${statusText}</span>: Suite execution finished. Auto-closing GUI in ${data.auto_close_countdown} seconds...`;
                }

            } catch (e) {
                console.error("Failed to fetch state:", e);
            }
        }

        function renderTestList(cases, activeName) {
            // Keep title header, clear items
            const items = testListEl.querySelectorAll(".test-item");
            items.forEach(el => el.remove());

            cases.forEach(c => {
                const el = document.createElement("div");
                el.className = "test-item" + (c.name === activeName ? " active" : "");
                
                if (c.category === "Simulation Cover") {
                    el.classList.add("covering");
                }
                
                const nameContainer = document.createElement("div");
                nameContainer.style.display = "flex";
                nameContainer.style.alignItems = "center";
                nameContainer.style.gap = "8px";
                
                if (c.category === "Simulation Cover") {
                    const badge = document.createElement("span");
                    badge.className = "covering-badge";
                    badge.textContent = "COVERING";
                    nameContainer.appendChild(badge);
                }
                
                const nameSpan = document.createElement("span");
                nameSpan.textContent = c.name;
                nameSpan.style.fontFamily = "monospace";
                nameContainer.appendChild(nameSpan);
                
                el.appendChild(nameContainer);

                const indicator = document.createElement("div");
                indicator.className = "test-item-status dot-" + c.status;
                el.appendChild(indicator);

                testListEl.appendChild(el);
            });
        }

        // Poll state
        setInterval(updateState, 250);
    </script>
</body>
</html>
"""

app = FastAPI()

@app.get("/")
def get_index():
    return HTMLResponse(content=INDEX_HTML)

@app.get("/api/state")
def get_state():
    # Construct list of test cases in JSON format for the front-end list
    cases_list = []
    history_map = {name: status for name, status in history}
    for name, _, cat in TEST_CASES:
        status = "pending"
        if name == active_test_name:
            status = "running"
        elif name in history_map:
            status = history_map[name]
        cases_list.append({"name": name, "status": status, "category": cat})

    return {
        "suite_status": suite_status,
        "suite_completed": suite_completed,
        "total_tests": len(TEST_CASES),
        "passed": stats["passed"],
        "failed": stats["failed"],
        "skipped": stats["skipped"],
        "pending": stats["pending"],
        "active_test_name": active_test_name,
        "active_category": active_category,
        "test_cases": cases_list,
        "logs": "".join(logs_list),
        "ai_summary": ai_summary,
        "auto_close_countdown": auto_close_countdown
    }

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def run_suite_in_thread():
    global suite_status, suite_completed, active_test_name, active_category, end_time, auto_close_countdown
    
    # Start AI Summary Worker
    threading.Thread(target=ai_summary_worker, daemon=True).start()
    
    for name, func, category in TEST_CASES:
        active_test_name = name
        active_category = category
        stats["pending"] -= 1
        
        # Trigger an immediate AI Summary refresh on test transition
        trigger_ai_summary_refresh()
        
        start = time.time()
        try:
            func()
            elapsed = time.time() - start
            stats["passed"] += 1
            history.append((name, "passed"))
            
            passed_summary = f"SUMMARY: TEST CASE '{name}' COMPLETED SUCCESSFULLY AND PASSED IN {elapsed:.2f} SECONDS.".upper()
            print(f"\n📢  {passed_summary}\n")
        except BaseException as e:
            elapsed = time.time() - start
            if e.__class__.__name__ == "Skipped":
                stats["skipped"] += 1
                history.append((name, "skipped"))
                skipped_summary = f"SUMMARY: TEST CASE '{name}' WAS SKIPPED AFTER {elapsed:.2f} SECONDS. REASON: {e}".upper()
                print(f"\n📢  {skipped_summary}\n")
            else:
                stats["failed"] += 1
                history.append((name, "failed"))
                failed_summary = f"SUMMARY: TEST CASE '{name}' FAILED AFTER {elapsed:.2f} SECONDS. ERROR: {e}".upper()
                print(f"\n📢  {failed_summary}\n")
                
                # If uvicorn/test failed, keep going or fail suite status
                suite_status = "failed"
                
    # Reached end
    suite_completed = True
    if suite_status != "failed":
        suite_status = "passed"
    end_time = time.time()
    
    # Final AI Summary call
    trigger_ai_summary_refresh()
    
    # Wait countdown
    for i in range(10, 0, -1):
        auto_close_countdown = i
        time.sleep(1)
        
    # Restore stdout/stderr
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    
    # Print final test suite result to stdout
    print("\n" + "=" * 80)
    print("                      DOCUMENTARY PIPELINE TEST RUNNER - FINAL REPORT")
    print("=" * 80)
    print(f"PASSED:  {stats['passed']}")
    print(f"FAILED:  {stats['failed']}")
    print(f"SKIPPED: {stats['skipped']}")
    print("=" * 80 + "\n")
    
    os._exit(0 if suite_status == "passed" else 1)

def main():
    # Set stdout line buffering
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    # Find free port
    port = find_free_port()
    
    # Redirect stdout and stderr to capture log feed
    sys.stdout = LiveStreamCapture(original_stdout)
    sys.stderr = LiveStreamCapture(original_stderr)

    # Start log processor thread
    threading.Thread(target=log_processor, daemon=True).start()

    # Start test suite thread
    suite_thread = threading.Thread(target=run_suite_in_thread, daemon=True)
    suite_thread.start()

    # Start web server in a separate thread
    def start_web_server():
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    threading.Thread(target=start_web_server, daemon=True).start()

    # Automatically open local browser page
    time.sleep(0.8)
    webbrowser.open(f"http://127.0.0.1:{port}")

    # Wait blockingly for suite execution to finish
    suite_thread.join()

if __name__ == "__main__":
    main()
