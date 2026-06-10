import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path('/Users/orpington/Documents/economy-documentary-work')
TESTS_UNITS = PROJECT_ROOT / 'tests' / 'units'

covering_stems = {
    'test_gsa_wal_concurrency_isolation',
    'test_scenario_agent_live_prompt_turn',
    'test_ssh_handshake_and_docker_health',
    'test_audio_agent_tts_job_queueing',
    'test_coordinate_timeline_dynamic_drift',
    'test_vast_create_and_destroy_lifecycle',
    'test_provisioner_vast_offers_search',
    'test_budget_limit_aborted_gate',
    'test_audio_loudness_normalizer_compilation',
    'test_perplexity_verify_live'
}

# Collect files and determine new names
files = [f for f in TESTS_UNITS.glob('*.py') if f.name not in ('__init__.py', 'harness.py')]
mapping = {} # old_stem -> new_stem

for f in files:
    stem = f.stem
    if stem in covering_stems:
        new_stem = stem.replace('test_', 'test_covering_', 1)
    else:
        if stem.startswith('test_sim_'):
            new_stem = stem.replace('test_sim_', 'test_simulation_', 1)
        elif stem.startswith('test_'):
            new_stem = stem.replace('test_', 'test_simulation_', 1)
        else:
            new_stem = 'test_simulation_' + stem
    mapping[stem] = new_stem

print('Determined mapping for', len(mapping), 'files.')

# 1. Execute git mv
for old_stem, new_stem in mapping.items():
    old_path = TESTS_UNITS / f'{old_stem}.py'
    new_path = TESTS_UNITS / f'{new_stem}.py'
    if old_path.exists():
        print(f'git mv {old_stem}.py -> {new_stem}.py')
        subprocess.run(['git', 'mv', str(old_path), str(new_path)], check=True, cwd=str(PROJECT_ROOT))

# 2. Add test_architecture_audit.py
arch_test_content = """import pytest
from tests.runner.architecture_checker import run_agentic_architecture_test

def test_architecture_compliance():
    # Run the agentic architecture auditor to enforce paranoid test invariants.
    run_agentic_architecture_test()
"""
arch_path = TESTS_UNITS / 'test_architecture_audit.py'
with open(arch_path, 'w') as f:
    f.write(arch_test_content)
subprocess.run(['git', 'add', str(arch_path)], check=True, cwd=str(PROJECT_ROOT))
print('Created test_architecture_audit.py')

# Add test_architecture_audit to mapping
mapping['test_architecture_audit'] = 'test_architecture_audit'

# 3. Refactor function definitions and internal self-references in all test files
for old_stem, new_stem in mapping.items():
    new_path = TESTS_UNITS / f'{new_stem}.py'
    if not new_path.exists():
        continue
    content = new_path.read_text(encoding='utf-8')
    
    # Prefix function definitions def test_...
    # For covering tests: def test_xyz -> def test_covering_xyz
    # For simulation tests: def test_xyz -> def test_simulation_xyz (or test_sim_xyz -> test_simulation_xyz)
    def repl_func(m):
        func_name = m.group(1)
        if new_stem.startswith('test_covering_'):
            if not func_name.startswith('covering_'):
                return f'def test_covering_{func_name}'
        elif new_stem.startswith('test_simulation_'):
            if func_name.startswith('sim_'):
                return f'def test_simulation_{func_name[4:]}'
            elif func_name.startswith('bdd_'):
                return f'def test_simulation_{func_name}'
            elif not func_name.startswith('simulation_'):
                return f'def test_simulation_{func_name}'
        return m.group(0)

    updated_content = re.sub(r'def test_([a-zA-Z0-9_]+)', repl_func, content)
    
    # Also replace old_stem references inside the file content (like print('[STARTING TEST] test_gsa...'))
    updated_content = updated_content.replace(old_stem, new_stem)
    
    # Write back
    new_path.write_text(updated_content, encoding='utf-8')
    print(f'Updated function names and self-references in {new_stem}.py')

# 4. Update runner scripts and documentation
files_to_update = [
    PROJECT_ROOT / 'tests' / 'runner' / 'run.py',
    PROJECT_ROOT / 'tests' / 'runner' / 'run_cli.py',
    PROJECT_ROOT / 'tests' / 'runner' / 'architecture_checker.py',
    PROJECT_ROOT / 'scratch' / 'runner_copy' / 'run.py',
    PROJECT_ROOT / 'scratch' / 'runner_copy' / 'run_cli.py',
    PROJECT_ROOT / 'scratch' / 'runner_copy' / 'architecture_checker.py',
    PROJECT_ROOT / 'obsidian-vault' / '10 - Simulation Covers.md',
    PROJECT_ROOT / 'obsidian-vault' / '08 - Testing, Concurrency, and Rollout.md',
    PROJECT_ROOT / 'server' / 'capabilities' / 'test_judge_capability.py',
]

for p in files_to_update:
    if p.exists():
        content = p.read_text(encoding='utf-8')
        orig = content
        
        # Sort keys by length descending to prevent substring matching issues
        for old_stem in sorted(mapping.keys(), key=len, reverse=True):
            new_name = mapping[old_stem]
            # Match old_stem as a module/filename token or exact word
            content = content.replace(f'import {old_stem}', f'import {new_name}')
            content = content.replace(f'from tests.units.{old_stem}', f'from tests.units.{new_name}')
            content = content.replace(f'"{old_stem}.py"', f'"{new_name}.py"')
            content = content.replace(f'\'{old_stem}.py\'', f'\'{new_name}.py\'')
            content = content.replace(f'`{old_stem}.py`', f'`{new_name}.py`')
            content = content.replace(f'`{old_stem}`', f'`{new_name}`')
            content = content.replace(f'"{old_stem}"', f'"{new_name}"')
            content = content.replace(f'\'{old_stem}\'', f'\'{new_name}\'')
            
            # Replace function name references
            content = re.sub(r'\b' + old_stem + r'\b', new_name, content)
            
        if content != orig:
            p.write_text(content, encoding='utf-8')
            print(f'Updated references in {p.name}')
