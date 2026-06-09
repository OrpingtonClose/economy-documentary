#!/usr/bin/env python3
import sys
import os
import json
import tempfile
from pathlib import Path
import subprocess

plugin_path = Path("/Users/orpington/Documents/economy-documentary-work/.agents/plugins/uppercase-enforcer/uppercase_enforcer.py")

def create_mock_transcript(user_prompts, proposed_tool, proposed_args):
    temp_dir = tempfile.mkdtemp()
    conv_dir = Path(temp_dir) / "mock-conversation-id"
    log_dir = conv_dir / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"
    
    with open(transcript_file, "w", encoding="utf-8") as f:
        for i, prompt in enumerate(user_prompts):
            step = {
                "step_index": i * 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nLocal time\n</ADDITIONAL_METADATA>"
            }
            f.write(json.dumps(step) + "\n")
            
        planner_step = {
            "step_index": len(user_prompts) * 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": proposed_tool,
                    "args": proposed_args
                }
            ]
        }
        f.write(json.dumps(planner_step) + "\n")
        
    return temp_dir, transcript_file

def run_test_case(test_name, user_prompts, proposed_tool, proposed_args, cli_args, initial_caps_lock=None, expected_exit_code=0):
    print(f"--- Running Test: {test_name} ---")
    temp_dir, transcript_file = create_mock_transcript(user_prompts, proposed_tool, proposed_args)
    
    script_content = plugin_path.read_text(encoding="utf-8")
    patched_content = script_content.replace(
        'BRAIN_DIR = Path("/Users/orpington/.gemini/antigravity/brain")',
        f'BRAIN_DIR = Path("{temp_dir}")'
    )
    
    temp_script_path = Path(temp_dir) / "test_enforcer.py"
    temp_script_path.write_text(patched_content, encoding="utf-8")
    
    # Write initial state of caps lock if provided
    caps_lock_path = Path(temp_dir) / "caps_lock_state.json"
    if initial_caps_lock is not None:
        with open(caps_lock_path, "w") as f:
            json.dump(initial_caps_lock, f)
            
    cmd = ["python3", str(temp_script_path)] + cli_args
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    print("STDOUT:")
    print(result.stdout)
    print("STDERR:")
    print(result.stderr)
    
    # Read final state of caps lock if it exists
    final_state = None
    if caps_lock_path.exists():
        with open(caps_lock_path, "r") as f:
            final_state = json.load(f)
        print("Final Caps Lock State:", final_state)
        
    print(f"Exit Code: {result.returncode} (Expected: {expected_exit_code})")
    
    # Clean up
    import shutil
    shutil.rmtree(temp_dir)
    
    if result.returncode == expected_exit_code:
        print(f"✅ {test_name}: SUCCESS\n")
        return True, final_state
    else:
        print(f"❌ {test_name}: FAILED\n")
        return False, final_state

def main():
    success = True
    
    # Test 1: Standard lowercase request (no caps lock trigger, no state exists)
    ok, state = run_test_case(
        "Lowercase Request Passes",
        ["please view config files"],
        "view_file",
        {"AbsolutePath": "server/config.py"},
        ["--tool", "view_file", "--file", "server/config.py"],
        expected_exit_code=0
    )
    success &= ok
    
    # Test 2: Caps lock trigger automatically sets active status and blocks non-compliant call
    # Prompt is "DONT TOUCH THE LICENSE FILE"
    ok, state = run_test_case(
        "Caps Lock Trigger Blocks Violation",
        ["DONT TOUCH THE LICENSE FILE"],
        "write_to_file",
        {"TargetFile": "LICENSE", "CodeContent": "corrupted license"},
        ["--tool", "write_to_file", "--file", "LICENSE"],
        expected_exit_code=1
    )
    success &= ok
    assert state and state.get("active") is True
    assert state.get("superprompt") == "DONT TOUCH THE LICENSE FILE"
    
    # Test 3: Caps lock is active on subsequent non-pertinent tools
    # Let's say user request is "DONT TOUCH THE LICENSE FILE" but tool call is "run_command" to delete LICENSE.
    # The enforcer should audit it and block it because ALL subsequent things are harshly checked!
    ok, state = run_test_case(
        "Caps Lock Active Audits Non-Pertinent Tool",
        ["DONT TOUCH THE LICENSE FILE"],
        "run_command",
        {"CommandLine": "rm LICENSE"},
        ["--tool", "run_command", "--command", "rm LICENSE"],
        initial_caps_lock={"active": True, "superprompt": "DONT TOUCH THE LICENSE FILE"},
        expected_exit_code=1
    )
    success &= ok

    # Test 4: Canceled Caps Lock does not audit/block
    ok, state = run_test_case(
        "Canceled Caps Lock Bypasses Checks",
        ["DONT TOUCH THE LICENSE FILE"],
        "run_command",
        {"CommandLine": "rm LICENSE"},
        ["--tool", "run_command", "--command", "rm LICENSE"],
        initial_caps_lock={"active": False, "superprompt": "DONT TOUCH THE LICENSE FILE"},
        expected_exit_code=0
    )
    success &= ok

    if success:
        print("🎉 ALL CAPS LOCK PROTECTION TESTS PASSED!")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED!")
        sys.exit(1)

if __name__ == "__main__":
    main()
