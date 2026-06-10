import os
import re
import subprocess
from pathlib import Path

tests_dir = Path("/Users/orpington/Documents/economy-documentary-work/tests/units")

# 1. Non-SC files that should be Category 3 logic tests (prefixed with test_sim_)
files_to_rename = {
    "test_accumulative_drift_correction.py": "test_sim_accumulative_drift_correction.py",
    "test_agent_chooses_vm_size_and_provisioner_allocates.py": "test_sim_agent_chooses_vm_size_and_provisioner_allocates.py",
    "test_assemble_final_cut_execution.py": "test_sim_assemble_final_cut_execution.py",
    "test_localized_recovery_and_retry.py": "test_sim_localized_recovery_and_retry.py",
    "test_preemption_and_recovery.py": "test_sim_preemption_and_recovery.py",
    "test_provisioner_escalation_policy.py": "test_sim_provisioner_escalation_policy.py",
    "test_timeline_validation_suite.py": "test_sim_timeline_validation_suite.py",
    "test_provisioner_cli_command_invocation.py": "test_sim_provisioner_cli_command_invocation.py",
}

# Perform git renames
for src, dst in files_to_rename.items():
    src_path = tests_dir / src
    dst_path = tests_dir / dst
    if src_path.exists():
        if dst_path.exists():
            print(f"Destination {dst} already exists, deleting source...")
            subprocess.run(["git", "rm", "-f", str(src_path)])
        else:
            print(f"Renaming {src} -> {dst}...")
            subprocess.run(["git", "mv", str(src_path), str(dst_path)])

# 2. Complete list of files to sanitize (both SCs and Sim tests)
all_files = [
    # 10 Simulation Covers (Category 1)
    "test_gsa_wal_concurrency_isolation.py",
    "test_scenario_agent_live_prompt_turn.py",
    "test_ssh_handshake_and_docker_health.py",
    "test_audio_agent_tts_job_queueing.py",
    "test_coordinate_timeline_dynamic_drift.py",
    "test_vast_create_and_destroy_lifecycle.py",
    "test_provisioner_vast_offers_search.py",
    "test_budget_limit_aborted_gate.py",
    "test_audio_loudness_normalizer_compilation.py",
    "test_perplexity_verify_live.py",
    
    # Renamed local logic simulation tests (Category 3)
    "test_sim_accumulative_drift_correction.py",
    "test_sim_agent_chooses_vm_size_and_provisioner_allocates.py",
    "test_sim_assemble_final_cut_execution.py",
    "test_sim_localized_recovery_and_retry.py",
    "test_sim_preemption_and_recovery.py",
    "test_sim_provisioner_escalation_policy.py",
    "test_sim_timeline_validation_suite.py",
    "test_sim_provisioner_cli_command_invocation.py",
    
    # Other simulation/capacity tests (Category 2/3)
    "test_max_capacity_pipeline.py",
    "test_bdd_final_assembly_real_media.py",
    "test_effect_pydantic_round_trip.py",
]

simulator_imports_to_remove = [
    r"from capabilities\.test_real_vast_provisioning_bdd_search_offers import VastSearchSimulator\n?",
    r"from capabilities\.test_real_vast_provisioning_bdd_create_instance import VastCreateSimulator\n?",
    r"from capabilities\.test_real_assembly_bdd_assemble_final_cut import AssembleFinalCutSimulator\n?",
    r"from capabilities\.test_real_vast_provisioning_bdd_worker_health import WorkerHealthSimulator\n?",
    r"from capabilities\.test_real_vast_provisioning_bdd_destroy_instance import VastDestroySimulator\n?",
    r"from capabilities\.test_real_perplexity_verify import PerplexityVerifySimulator\n?",
    r"from capabilities\.test_single_purpose_tts_simulators import TtsPreemptSimulator\n?",
    r"from capabilities\.test_single_purpose_tts_simulators import TtsMultiBlockSimulator\n?",
    r"from capabilities\.test_single_purpose_tts_simulators import TtsFailSimulator\n?",
    r"from capabilities\.test_single_purpose_tts_simulators import TtsJob1Simulator\n?",
    r"from capabilities\.test_single_purpose_ltx_simulators import LtxScaleSimulator\n?",
    r"from capabilities\.test_single_purpose_ltx_simulators import LtxSingleSimulator\n?",
]

path_replacements = {
    '"/Users/orpington/api_keys/vast_ai_key.txt"': 'os.path.expanduser("~/api_keys/vast_ai_key.txt")',
    '"/Users/orpington/.letta-cli-venv/bin/vastai"': 'os.path.expanduser("~/.letta-cli-venv/bin/vastai")',
    '"/Users/orpington/.gemini/antigravity/brain/2396d2a7-2d70-42f0-8498-e40c70b10fa0"': 'os.path.expanduser("~/.gemini/antigravity/brain/2396d2a7-2d70-42f0-8498-e40c70b10fa0")',
    '"/Users/orpington/.gemini/antigravity/brain"': 'os.path.expanduser("~/.gemini/antigravity/brain")',
    '"/Users/orpington/api_keys/LLMS/perplexity_api_key.txt"': 'os.path.expanduser("~/api_keys/LLMS/perplexity_api_key.txt")',
    '"/Users/orpington/api_keys/LLMS/deepseek_api_key.txt"': 'os.path.expanduser("~/api_keys/LLMS/deepseek_api_key.txt")',
    '"/Users/orpington/api_keys/LLMS/deepseek_api.txt"': 'os.path.expanduser("~/api_keys/LLMS/deepseek_api.txt")',
}

def clean_file(filename):
    file_path = tests_dir / filename
    if not file_path.exists():
        print(f"Skipping {filename}: not found")
        return
        
    print(f"Cleaning {filename}...")
    content = file_path.read_text(encoding="utf-8")
    
    # 1. Clean up imports based on category
    is_bdd_or_capacity = filename.startswith("test_bdd_") or filename == "test_max_capacity_pipeline.py"
    
    if is_bdd_or_capacity:
        content = content.replace("capabilities.test_real_vast_provisioning_bdd_worker_health", "capabilities.sim_vast_provisioning_bdd_worker_health")
        content = content.replace("capabilities.test_real_assembly_bdd_assemble_final_cut", "capabilities.sim_assembly_bdd_assemble_final_cut")
        content = content.replace("capabilities.test_real_vast_provisioning_bdd_search_offers", "capabilities.sim_vast_provisioning_bdd_search_offers")
        content = content.replace("capabilities.test_real_vast_provisioning_bdd_create_instance", "capabilities.sim_vast_provisioning_bdd_create_instance")
    else:
        # For non-simulation tests, remove all simulator/mock capabilities
        for pattern in simulator_imports_to_remove:
            content = re.sub(pattern, "", content)
        # Also remove unused BDD judge capability imports
        content = re.sub(r"from test_judge_capability import BddScenario, run_bdd_judge, collect_evidence_from_store\n?", "", content)

    # 2. Remove print overrides
    print_override_pattern = r"def print\(\*args, \*\*kwargs\):[\s\S]*?builtins\.print\(\*args, \*\*kwargs\)\n?"
    content = re.sub(print_override_pattern, "", content)
    content = re.sub(r"import builtins\n?", "", content)
    
    # 3. Remove unused measure_lufs_integrated (except assembly compilation and max capacity)
    if filename not in ("test_max_capacity_pipeline.py", "test_audio_loudness_normalizer_compilation.py"):
        lufs_pattern = r"def measure_lufs_integrated\([\s\S]*?return 20\.0 \* math\.log10\(rms\) \+ 0\.0\n?"
        content = re.sub(lufs_pattern, "", content)
        
    # 4. Remove timeout arguments
    content = content.replace(", timeout=None", "")
    content = content.replace("timeout=None", "")
    content = content.replace(", timeout=5.0", "")
    content = content.replace("timeout=5.0", "")
    content = content.replace(", timeout=2.0", "")
    content = content.replace("timeout=2.0", "")
    
    # 5. Make paths portable
    for orig, repl in path_replacements.items():
        content = content.replace(orig, repl)

    # 6. Replace soft RuntimeError with hard failures using pytest.fail
    if "pytest.fail" in content or "raise RuntimeError(" in content:
        if "import pytest" not in content:
            content = "import pytest\n" + content
            
        content = re.sub(r"raise RuntimeError\((['\"])CRITICAL FAILURE:[\s\S]*?\1\)", r"pytest.fail(\1CRITICAL FAILURE: live dependencies missing\1)", content)
        content = re.sub(r"raise RuntimeError\((['\"])Vast\.ai account lacks credit[\s\S]*?\1\)", r"pytest.fail(\1Vast.ai account lacks credit; hard failure\1)", content)
        content = re.sub(r"raise RuntimeError\((['\"])Simulation Cover requires live execution[\s\S]*?\1\)", r"pytest.fail(\1Simulation Cover requires live execution\1)", content)

    # 7. Specific fixes for test_max_capacity_pipeline.py
    if filename == "test_max_capacity_pipeline.py":
        content = content.replace("while True:", "for iteration in range(300):")
        
    file_path.write_text(content, encoding="utf-8")
    print(f"Cleaned {filename} successfully.")

for f in all_files:
    clean_file(f)
