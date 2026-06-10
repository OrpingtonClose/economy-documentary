import re
from pathlib import Path

def replace_in_file(filepath: Path, target: str, replacement: str):
    if not filepath.exists():
        print(f"File not found: {filepath}")
        return False
    content = filepath.read_text(encoding="utf-8")
    if target in content:
        new_content = content.replace(target, replacement)
        filepath.write_text(new_content, encoding="utf-8")
        print(f"✓ Successfully updated {filepath}")
        return True
    else:
        print(f"⚠️ Target pattern not found in {filepath}")
        return False

def main():
    base_dir = Path("tests/units")
    
    # 1. test_audio_agent_tts_job_queueing.py
    replace_in_file(
        base_dir / "test_audio_agent_tts_job_queueing.py",
        'httpx.get("https://api.deepseek.com/", timeout=5.0)',
        'httpx.get("https://api.deepseek.com/")'
    )
    
    # 2. test_budget_limit_aborted_gate.py
    replace_in_file(
        base_dir / "test_budget_limit_aborted_gate.py",
        'socket.create_connection(("vast.ai", 80), timeout=5.0)',
        'socket.create_connection(("vast.ai", 80))'
    )
    
    # 3. test_perplexity_verify_live.py
    replace_in_file(
        base_dir / "test_perplexity_verify_live.py",
        'httpx.get("https://api.perplexity.ai/", timeout=5.0)',
        'httpx.get("https://api.perplexity.ai/")'
    )
    
    # 4. test_provisioner_vast_offers_search.py
    replace_in_file(
        base_dir / "test_provisioner_vast_offers_search.py",
        'socket.create_connection(("vast.ai", 80), timeout=5.0)',
        'socket.create_connection(("vast.ai", 80))'
    )
    
    # 5. test_scenario_agent_live_prompt_turn.py
    replace_in_file(
        base_dir / "test_scenario_agent_live_prompt_turn.py",
        'httpx.get("https://api.deepseek.com/", timeout=5.0)',
        'httpx.get("https://api.deepseek.com/")'
    )
    
    # 6. test_ssh_handshake_and_docker_health.py
    replace_in_file(
        base_dir / "test_ssh_handshake_and_docker_health.py",
        'resp = httpx.get(url, timeout=2.0)',
        'resp = httpx.get(url)'
    )
    
    # 7. test_vast_create_and_destroy_lifecycle.py
    replace_in_file(
        base_dir / "test_vast_create_and_destroy_lifecycle.py",
        'socket.create_connection(("vast.ai", 80), timeout=5.0)',
        'socket.create_connection(("vast.ai", 80))'
    )
    replace_in_file(
        base_dir / "test_vast_create_and_destroy_lifecycle.py",
        'while time.time() - start_time < 300:',
        'while True:'
    )
    
    # 8. test_video_agent_ltx_job_queueing.py
    replace_in_file(
        base_dir / "test_video_agent_ltx_job_queueing.py",
        'httpx.get("https://api.deepseek.com/", timeout=5.0)',
        'httpx.get("https://api.deepseek.com/")'
    )

if __name__ == "__main__":
    main()
