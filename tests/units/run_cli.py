#!/usr/bin/env python3
import os
import sys
import time
import argparse
import json
import httpx
import pathlib
import traceback

# Setup Python paths
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "server"))

# Import all tests
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

# Load DeepSeek API key
DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
api_key = None
if os.path.exists(DEEPSEEK_KEY_PATH):
    try:
        with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
    except Exception:
        pass

def run_subagent_audit(test_name: str) -> dict:
    if not api_key:
        return {"verdict": "FAIL", "reasoning": "DeepSeek API key not found. Congruence audit disabled."}

    # 1. Read documentation
    docs_content = ""
    brain_dir = None
    try:
        brain_root = pathlib.Path("/Users/orpington/.gemini/antigravity/brain")
        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        if conv_id and (brain_root / conv_id).exists():
            brain_dir = brain_root / conv_id
        else:
            newest_mtime = 0
            for subdir in brain_root.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    state_file = subdir / ".lock_state"
                    mtime = state_file.stat().st_mtime if state_file.exists() else subdir.stat().st_mtime
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        brain_dir = subdir
    except Exception:
        pass

    if brain_dir:
        cov_def_path = brain_dir / "simulation_coverage_definition.md"
        if cov_def_path.exists():
            try:
                docs_content += "=== DOCUMENTATION: simulation_coverage_definition.md ===\n"
                docs_content += cov_def_path.read_text(encoding="utf-8") + "\n\n"
            except Exception:
                pass

    impl_plan_path = PROJECT_ROOT / "tests" / "units" / "simulation_covers_implementation_plan.md"
    if impl_plan_path.exists():
        try:
            docs_content += "=== DOCUMENTATION: simulation_covers_implementation_plan.md ===\n"
            docs_content += impl_plan_path.read_text(encoding="utf-8") + "\n\n"
        except Exception:
            pass

    if not docs_content:
        docs_content = "No specific documentation file found."

    # 2. Read test code file
    test_file_path = PROJECT_ROOT / "tests" / "units" / f"{test_name}.py"
    if test_name == "Maximum Capacity Test":
        test_file_path = PROJECT_ROOT / "tests" / "units" / "test_max_capacity_pipeline.py"

    if test_file_path.exists():
        try:
            test_code = test_file_path.read_text(encoding="utf-8")
        except Exception as e:
            test_code = f"Error reading test file: {e}"
    else:
        test_code = f"Test file {test_file_path} not found on disk."

    # 3. Call DeepSeek
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    system_prompt = (
        "You are a fresh QA subagent auditor verifying test congruence.\n"
        "Your task is to compare the implementation of a specific test case (Python code) "
        "with the official documentation/specification (like Given/When/Then requirements, BDD scenarios, and coverage plans).\n"
        "Verify if the test code is congruent with the documentation. Check for:\n"
        "1. Does the test code actually implement the scenario described in the docs?\n"
        "2. Are the assertions, events, and logic in the test code matching the requirements in the docs?\n"
        "3. Is there any mismatch, mocking where real behavior is required, or missing verification steps?\n"
        "Note: The tests are integration/unit tests. If they use mocks or simulators where the docs specify real behavior, point that out.\n\n"
        "Respond with EXACTLY a JSON object containing the keys 'verdict' (must be either 'PASS' or 'FAIL') "
        "and 'reasoning' (a detailed explanation of the verdict). "
        "Do not include any explanation or markdown backticks outside the JSON."
    )
    
    user_prompt = (
        f"Test Case: {test_name}\n\n"
        f"--- DOCUMENTATION ---\n{docs_content}\n\n"
        f"--- TEST CODE ---\n{test_code}\n\n"
        f"Evaluate the congruence of the test code with the documentation and output JSON."
    )
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 1000
    }
    
    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=60.0)
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            res = json.loads(raw)
            if "verdict" in res and "reasoning" in res:
                return res
            return {"verdict": "FAIL", "reasoning": f"Invalid response format from LLM: {raw}"}
        else:
            return {"verdict": "FAIL", "reasoning": f"DeepSeek API request failed (HTTP {resp.status_code})"}
    except Exception as e:
        return {"verdict": "FAIL", "reasoning": f"Error during subagent audit execution: {e}"}

def main():
    parser = argparse.ArgumentParser(description="Documentary Pipeline CLI Test Runner")
    parser.add_argument("tests", nargs="*", help="Filter test cases by name (exact or substring).")
    parser.add_argument("--category", help="Only run tests in a specific category.")
    parser.add_argument("--list", action="store_true", help="List all available test cases and exit.")
    parser.add_argument("--no-audit", action="store_true", help="Skip the subagent congruence audit.")
    args = parser.parse_args()

    # 1. Handle --list
    if args.list:
        print("\n=== Available Test Cases ===")
        current_cat = None
        for name, _, category in TEST_CASES:
            if category != current_cat:
                current_cat = category
                print(f"\n[{category}]")
            print(f"  - {name}")
        print()
        sys.exit(0)

    # 2. Filter test cases
    selected_tests = []
    for name, func, category in TEST_CASES:
        # Filter by category
        if args.category and args.category.lower() not in category.lower():
            continue
        # Filter by test name
        if args.tests:
            matched = False
            for filter_term in args.tests:
                if filter_term.lower() in name.lower():
                    matched = True
                    break
            if not matched:
                continue
        selected_tests.append((name, func, category))

    if not selected_tests:
        print("\n❌ No test cases matched the filters.")
        sys.exit(1)

    print(f"\n🚀 Running {len(selected_tests)} test case(s)...")

    results = []
    passed_count = 0
    failed_count = 0
    skipped_count = 0

    for idx, (name, func, category) in enumerate(selected_tests, 1):
        print("\n" + "=" * 80)
        print(f"[{idx}/{len(selected_tests)}] RUNNING: {name} ({category})")
        print("=" * 80)

        # Run Congruence Audit first unless skipped
        audit_verdict = "N/A"
        audit_reasoning = ""
        if not args.no_audit:
            print(f"🔍 Running fresh subagent congruence audit for '{name}'...")
            audit_res = run_subagent_audit(name)
            audit_verdict = audit_res.get("verdict", "FAIL")
            audit_reasoning = audit_res.get("reasoning", "Audit failed or returned no explanation.")
            print(f"🔍 Audit Verdict: {audit_verdict}")
            if audit_verdict == "FAIL":
                print(f"⚠️  Audit Reasoning: {audit_reasoning}")

        start_time = time.time()
        status = "PASSED"
        err_msg = ""
        
        try:
            func()
        except BaseException as e:
            if e.__class__.__name__ == "Skipped":
                status = "SKIPPED"
                err_msg = str(e)
            else:
                status = "FAILED"
                err_msg = traceback.format_exc()

        elapsed = time.time() - start_time
        print("-" * 80)
        
        if status == "PASSED":
            print(f"🟢 PASSED: '{name}' in {elapsed:.2f} seconds.")
            passed_count += 1
        elif status == "SKIPPED":
            print(f"🟡 SKIPPED: '{name}' in {elapsed:.2f} seconds. Reason: {err_msg}")
            skipped_count += 1
        else:
            print(f"🔴 FAILED: '{name}' in {elapsed:.2f} seconds.")
            print(f"\n{err_msg}")
            failed_count += 1

        results.append({
            "name": name,
            "status": status,
            "elapsed": elapsed,
            "audit_verdict": audit_verdict,
            "audit_reasoning": audit_reasoning
        })

    # Render final report table
    print("\n" + "=" * 100)
    print("                                   FINAL CLI RUN REPORT")
    print("=" * 100)
    print(f"{'Test Name':<50} | {'Status':<8} | {'Time (s)':<8} | {'Audit':<8}")
    print("-" * 100)
    for res in results:
        status_color = "🟢 " if res["status"] == "PASSED" else "🔴 " if res["status"] == "FAILED" else "🟡 "
        audit_color = "🟢 " if res["audit_verdict"] == "PASS" else "🔴 " if res["audit_verdict"] == "FAIL" else "⚪ "
        
        print(f"{res['name']:<50} | {status_color + res['status']:<8} | {res['elapsed']:<8.2f} | {audit_color + res['audit_verdict']:<8}")
    print("=" * 100)
    print(f"SUMMARY: {passed_count} Passed, {failed_count} Failed, {skipped_count} Skipped.")
    print("=" * 100 + "\n")

    sys.exit(0 if failed_count == 0 else 1)

if __name__ == "__main__":
    main()
