import re
import os
from pathlib import Path

tests_dir = Path("/Users/orpington/Documents/economy-documentary-work/tests/units")

files_to_clean = [
    "test_accumulative_drift_correction.py",
    "test_agent_chooses_vm_size_and_provisioner_allocates.py",
    "test_assemble_final_cut_execution.py",
    "test_bdd_final_assembly_real_media.py",
    "test_budget_limit_aborted_gate.py",
    "test_effect_pydantic_round_trip.py",
    "test_gsa_wal_concurrency_isolation.py",
    "test_localized_recovery_and_retry.py",
    "test_max_capacity_pipeline.py",
    "test_perplexity_verify_live.py",
    "test_preemption_and_recovery.py",
    "test_provisioner_cli_command_invocation.py",
    "test_provisioner_escalation_policy.py",
    "test_provisioner_vast_offers_search.py",
    "test_timeline_validation_suite.py",
    "test_vast_create_and_destroy_lifecycle.py",
]

# Unused simulator imports to remove from non-simulation tests
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
]

path_replacements = {
    '"/Users/orpington/api_keys/vast_ai_key.txt"': 'os.path.expanduser("~/api_keys/vast_ai_key.txt")',
    '"/Users/orpington/.letta-cli-venv/bin/vastai"': 'os.path.expanduser("~/.letta-cli-venv/bin/vastai")',
    '"/Users/orpington/.gemini/antigravity/brain/2396d2a7-2d70-42f0-8498-e40c70b10fa0"': 'os.path.expanduser("~/.gemini/antigravity/brain/2396d2a7-2d70-42f0-8498-e40c70b10fa0")',
    '"/Users/orpington/.gemini/antigravity/brain"': 'os.path.expanduser("~/.gemini/antigravity/brain")',
    '"/Users/orpington/api_keys/LLMS/perplexity_api_key.txt"': 'os.path.expanduser("~/api_keys/LLMS/perplexity_api_key.txt")',
    '"/Users/orpington/api_keys/LLMS/deepseek_api_key.txt"': 'os.path.expanduser("~/api_keys/LLMS/deepseek_api_key.txt")',
}

def clean_file(filename):
    file_path = tests_dir / filename
    if not file_path.exists():
        print(f"Skipping {filename}: not found")
        return
        
    print(f"Cleaning {filename}...")
    content = file_path.read_text(encoding="utf-8")
    
    # 1. Rename used simulator imports in BDD / Simulation Tests
    if filename in ("test_bdd_final_assembly_real_media.py", "test_max_capacity_pipeline.py"):
        content = content.replace("capabilities.test_real_vast_provisioning_bdd_worker_health", "capabilities.sim_vast_provisioning_bdd_worker_health")
        content = content.replace("capabilities.test_real_assembly_bdd_assemble_final_cut", "capabilities.sim_assembly_bdd_assemble_final_cut")
        content = content.replace("capabilities.test_real_vast_provisioning_bdd_search_offers", "capabilities.sim_vast_provisioning_bdd_search_offers")
        content = content.replace("capabilities.test_real_vast_provisioning_bdd_create_instance", "capabilities.sim_vast_provisioning_bdd_create_instance")
    else:
        # Otherwise remove them
        for pattern in simulator_imports_to_remove:
            content = re.sub(pattern, "", content)

    # 2. Remove custom print override block
    print_override_pattern = r"def print\(\*args, \*\*kwargs\):[\s\S]*?builtins\.print\(\*args, \*\*kwargs\)\n?"
    content = re.sub(print_override_pattern, "", content)
    content = re.sub(r"import builtins\n?", "", content)
    
    # 3. Remove unused measure_lufs_integrated helper (except for designated assembly tests)
    if filename not in ("test_max_capacity_pipeline.py", "test_audio_loudness_normalizer_compilation.py"):
        lufs_pattern = r"def measure_lufs_integrated\([\s\S]*?return 20\.0 \* math\.log10\(rms\) \+ 0\.0\n?"
        content = re.sub(lufs_pattern, "", content)
        
    # 4. Remove timeout arguments from HTTP and socket connections
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

for f in files_to_clean:
    clean_file(f)
